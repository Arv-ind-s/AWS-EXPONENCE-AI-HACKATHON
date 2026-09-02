"""Case orchestration for warning ownership, SLA tracking and lifecycle."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.forecast import TriageEntry
from covenant_radar.db.models.identity import (
    AppUser,
    Role,
    UserPortfolioScope,
    UserRole,
)
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Case, CaseEvent, Notification
from covenant_radar.db.repositories.case import CaseRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.cases.lifecycle import (
    CaseState,
    transition_result,
)
from covenant_radar.domain.cases.sla import (
    BusinessCalendar,
    SlaThresholds,
    derive_sla,
    is_overdue,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize

_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_CASE_REFERENCE_MAX_LENGTH: Final[int] = 20
_CLOSURE_REASON_MAX_LENGTH: Final[int] = 200
_CLOSURE_NOTE_MAX_LENGTH: Final[int] = 4_000
_WARNING_BANDS: Final[frozenset[str]] = frozenset({"act", "amber"})
_ALL_BANDS: Final[frozenset[str]] = frozenset({"act", "amber", "watch"})
_RELATIONSHIP_MANAGER_ROLE: Final[str] = "relationship_manager"
_ADMINISTRATOR_ROLES: Final[tuple[str, ...]] = ("administrator", "admin")
_CASE_OPENED_EVENT: Final[str] = "opened"
_CASE_REOPENED_EVENT: Final[str] = "reopened"
_CASE_BAND_CHANGED_EVENT: Final[str] = "band_changed"
_CASE_STATE_CHANGED_EVENT: Final[str] = "state_changed"
_CASE_SLA_BREACHED_EVENT: Final[str] = "sla_breached"
_CASE_ASSIGNEE_FALLBACK_EVENT: Final[str] = "assignee_fallback"
_ASSIGNEE_FALLBACK_TEMPLATE: Final[str] = "case_assignee_fallback"
_SLA_BREACH_TEMPLATE: Final[str] = "case_sla_breach"
_IN_APP_CHANNEL: Final[str] = "in_app"


class AuditWriter(Protocol):
    """The append-only audit boundary used by case mutations."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's transaction."""


class CaseNotifier(Protocol):
    """Optional immediate delivery hook for a queued case notification."""

    def notify(
        self,
        template: str,
        recipient_ids: Sequence[UUID],
        payload: Mapping[str, object],
    ) -> object:
        """Notify recipients without changing the durable notification row."""


class AssigneeResolver(Protocol):
    """Optional portfolio-to-assignee mapping supplied by a deployment."""

    def __call__(self, borrower: Borrower, band: str) -> UUID | None:
        """Return the mapped assignee, or ``None`` when the mapping is absent."""


class DefaultOwnerResolver(Protocol):
    """Optional resolver for a portfolio's configured default owner."""

    def __call__(self, borrower: Borrower) -> UUID | None:
        """Return the configured default owner, or ``None`` when absent."""


class CaseService:
    """Authorize, persist and audit case workflow decisions.

    The service never commits.  Each mutation uses a savepoint inside the
    caller's transaction and locks the owning borrower before deciding whether
    an open case exists.  That parent-row lock serializes concurrent warning
    arrivals and preserves the one-open-case-per-borrower invariant on
    PostgreSQL; SQLite still receives the same deterministic checks.
    """

    def __init__(
        self,
        session: Session,
        thresholds: SlaThresholds | Mapping[str, object] | object | None = None,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        calendar: BusinessCalendar | Callable[[datetime, int], datetime] | None = None,
        request_id: str | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        repository: CaseRepository | None = None,
        notifier: CaseNotifier | Callable[..., object] | None = None,
        assignee_resolver: AssigneeResolver | None = None,
        default_owner_id: UUID | None = None,
        default_assignee_id: UUID | None = None,
        default_owner_resolver: DefaultOwnerResolver | None = None,
        administrator_ids: Sequence[UUID] = (),
        threshold_store: SlaThresholds | Mapping[str, object] | object | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("CaseService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("CaseService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("CaseService clock must expose now().")
        if repository is not None and not isinstance(repository, CaseRepository):
            raise TypeError("CaseService repository must be a CaseRepository.")
        if repository is not None and repository.session is not session:
            raise ValueError("CaseService repository and session must be identical.")
        if thresholds is not None and threshold_store is not None:
            raise TypeError("Provide exactly one of thresholds or threshold_store.")
        if default_owner_id is not None and default_assignee_id is not None:
            if default_owner_id != default_assignee_id:
                raise TypeError("default_owner_id and default_assignee_id disagree.")
        configured = thresholds if thresholds is not None else threshold_store
        if configured is None:
            from covenant_radar.config.thresholds import ThresholdStore

            configured = ThresholdStore()
        if (
            not callable(calendar)
            and calendar is not None
            and not callable(getattr(calendar, "add_hours", None))
        ):
            raise TypeError("calendar must expose add_hours(started_at, hours).")
        if assignee_resolver is not None and not callable(assignee_resolver):
            raise TypeError("assignee_resolver must be callable.")
        if default_owner_resolver is not None and not callable(default_owner_resolver):
            raise TypeError("default_owner_resolver must be callable.")
        if not isinstance(administrator_ids, Sequence) or isinstance(
            administrator_ids, str | bytes
        ):
            raise TypeError("administrator_ids must be a sequence of UUIDs.")
        if any(not isinstance(value, UUID) for value in administrator_ids):
            raise TypeError("administrator_ids must contain UUIDs.")

        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.calendar = calendar
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )
        self.repository = repository or CaseRepository(session)
        self.sla_thresholds = SlaThresholds.from_store(configured)
        self.notifier = notifier
        self.assignee_resolver = assignee_resolver
        self.default_owner_id = default_owner_id or default_assignee_id
        self.default_owner_resolver = default_owner_resolver
        self.administrator_ids = tuple(administrator_ids)

    def open_or_update(
        self,
        principal: Principal,
        triage_or_borrower: object | None = None,
        band: str | None = None,
        *,
        triage_entry: object | None = None,
        borrower_id: UUID | None = None,
        run_id: UUID | None = None,
        assignee_id: UUID | None = None,
        scope: Scope | None = None,
        now: datetime | None = None,
    ) -> Case | None:
        """Open a warning case or update its SLA when its band changes.

        ``triage_or_borrower`` may be a persisted :class:`TriageEntry`, its
        UUID, a borrower UUID when ``band`` is supplied, or a mapping carrying
        ``borrower_id``, ``band`` and optional ``run_id``.  A watch-band row is
        deliberately ignored because T11 only creates workflow for act/amber
        warnings.
        """

        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.UPDATE_CASE
        )
        source = _single_source(triage_or_borrower, triage_entry)
        resolved_borrower_id, resolved_band, resolved_run_id = self._triage_details(
            source,
            band=band,
            borrower_id=borrower_id,
            run_id=run_id,
            scope=resolved_scope,
        )
        if resolved_band == "watch":
            return None
        if resolved_band not in _WARNING_BANDS:
            raise ValidationError(
                f"Case warning band must be act or amber, got {resolved_band!r}.",
                field="case.band",
            )
        instant = self._now(now)

        with self.session.begin_nested():
            borrower = self.repository.lock_borrower(resolved_borrower_id, scope=resolved_scope)
            if borrower is None:
                raise NotFound(
                    f"Borrower {resolved_borrower_id} was not found within the current scope."
                )
            open_cases = self.repository.open_cases_for_borrower(
                borrower.id, scope=resolved_scope, for_update=True
            )
            if len(open_cases) > 1:
                raise Conflict(
                    f"Borrower {borrower.id} has {len(open_cases)} open cases; "
                    "exactly one is permitted."
                )
            current = open_cases[0] if open_cases else None
            if current is not None:
                if current.band_at_open == resolved_band:
                    return current
                return self._update_band(
                    current,
                    resolved_band,
                    resolved_run_id,
                    principal.id,
                    instant,
                )

            assignment = self._assignment(
                borrower,
                resolved_band,
                explicit_assignee_id=assignee_id,
            )
            deadline = derive_sla(
                resolved_band,
                instant,
                self.sla_thresholds,
                calendar=self.calendar,
            )
            prior_case = self._latest_closed_case(borrower.id)
            case = Case(
                id=new_id(),
                reference=self._next_reference(borrower),
                borrower_id=borrower.id,
                opened_from_run_id=resolved_run_id,
                state=CaseState.OPEN.value,
                band_at_open=resolved_band,
                assignee_id=assignment.assignee_id,
                due_at=deadline.due_at,
                sla_hours=deadline.hours,
                created_at=instant,
                updated_at=instant,
                request_id=self.request_id,
                created_by_id=principal.id,
                updated_by_id=principal.id,
            )
            self.repository.add(case)
            self.session.flush()
            self._append_event(
                case,
                _CASE_REOPENED_EVENT if prior_case is not None else _CASE_OPENED_EVENT,
                principal.id,
                instant,
                {
                    "band": resolved_band,
                    "run_id": str(resolved_run_id) if resolved_run_id is not None else None,
                    "sla_hours": deadline.hours,
                    "due_at": deadline.due_at.isoformat(),
                    "assignee_id": str(assignment.assignee_id),
                    "prior_case_id": str(prior_case.id) if prior_case is not None else None,
                },
            )
            self._audit(
                case,
                "reopened" if prior_case is not None else "opened",
                principal.id,
                {
                    "case_reference": case.reference,
                    "band": resolved_band,
                    "run_id": str(resolved_run_id) if resolved_run_id is not None else None,
                    "sla_hours": deadline.hours,
                    "due_at": deadline.due_at.isoformat(),
                    "assignee_id": str(assignment.assignee_id),
                    "prior_case_id": str(prior_case.id) if prior_case is not None else None,
                },
            )
            if assignment.used_fallback:
                self._append_event(
                    case,
                    _CASE_ASSIGNEE_FALLBACK_EVENT,
                    principal.id,
                    instant,
                    {
                        "assignee_id": str(assignment.assignee_id),
                        "administrator_ids": [str(value) for value in assignment.administrators],
                        "reason": "portfolio_mapping_missing",
                    },
                )
                self._queue_and_notify(
                    case,
                    _ASSIGNEE_FALLBACK_TEMPLATE,
                    assignment.administrators or (assignment.assignee_id,),
                    {
                        "case_reference": case.reference,
                        "case_id": str(case.id),
                        "borrower_id": str(case.borrower_id),
                        "assignee_id": str(case.assignee_id),
                    },
                    principal.id,
                    instant,
                )
            return case

    open_from_triage = open_or_update
    update_from_triage = open_or_update

    def sync(
        self,
        principal: Principal,
        entries: Sequence[object],
        *,
        scope: Scope | None = None,
        now: datetime | None = None,
    ) -> tuple[Case, ...]:
        """Apply a batch of triage rows in deterministic input order."""

        if not isinstance(entries, Sequence) or isinstance(entries, str | bytes):
            raise ValidationError("Case triage entries must be a sequence.", field="entries")
        cases: list[Case] = []
        for entry in entries:
            case = self.open_or_update(principal, entry, scope=scope, now=now)
            if case is not None:
                cases.append(case)
        return tuple(cases)

    def transition_case(
        self,
        principal: Principal,
        case_or_reference: UUID | str,
        target: CaseState | str,
        *,
        closure_reason: str | None = None,
        closure_note: str | None = None,
        expected_version: int | None = None,
        scope: Scope | None = None,
        now: datetime | None = None,
    ) -> Case:
        """Apply one documented lifecycle transition and append its history."""

        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.UPDATE_CASE
        )
        instant = self._now(now)
        with self.session.begin_nested():
            case = self._locked_case(case_or_reference, resolved_scope)
            if expected_version is not None:
                _expected_version(expected_version)
                if case.version != expected_version:
                    raise Conflict(
                        f"Case {case.reference} changed; expected version {expected_version}, "
                        f"found {case.version}."
                    )
            normalized_reason = _closure_reason(closure_reason)
            normalized_note = _closure_note(closure_note)
            change = transition_result(
                case.state,
                target,
                closure_reason=normalized_reason,
            )
            case.state = change.to_state.value
            case.updated_at = instant
            case.updated_by_id = principal.id
            case.version += 1
            if change.to_state is CaseState.CLOSED:
                case.closed_at = instant
                case.closure_reason = normalized_reason
                case.closure_note = normalized_note
            self.session.flush()
            self._append_event(
                case,
                _CASE_STATE_CHANGED_EVENT,
                principal.id,
                instant,
                {
                    "from_state": change.from_state.value,
                    "to_state": change.to_state.value,
                    "closure_reason_recorded": change.to_state is CaseState.CLOSED,
                },
            )
            self._audit(
                case,
                "state_changed",
                principal.id,
                {
                    "from_state": change.from_state.value,
                    "to_state": change.to_state.value,
                    "closure_reason_recorded": change.to_state is CaseState.CLOSED,
                },
            )
            return case

    transition = transition_case
    change_state = transition_case

    def escalate_overdue(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        now: datetime | None = None,
    ) -> tuple[Case, ...]:
        """Escalate every due non-closed case and queue one digest item each."""

        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.UPDATE_CASE
        )
        instant = self._now(now)
        escalated: list[Case] = []
        with self.session.begin_nested():
            candidates = self.repository.overdue(instant, scope=resolved_scope)
            for candidate in candidates:
                case = self.repository.get_for_update(candidate.id, scope=resolved_scope)
                if case is None or case.state == CaseState.CLOSED.value:
                    continue
                if case.due_at is None or not is_overdue(case.due_at, instant):
                    continue
                if case.state == CaseState.ESCALATED.value:
                    continue
                change = transition_result(case.state, CaseState.ESCALATED)
                case.state = change.to_state.value
                case.updated_at = instant
                case.updated_by_id = principal.id
                case.version += 1
                if case.assignee_id is None:
                    raise Conflict(
                        f"Case {case.reference} has no assignee; overdue escalation is refused."
                    )
                self.session.flush()
                self._append_event(
                    case,
                    _CASE_SLA_BREACHED_EVENT,
                    principal.id,
                    instant,
                    {
                        "due_at": case.due_at.isoformat(),
                        "overdue_at": instant.isoformat(),
                        "assignee_id": str(case.assignee_id),
                    },
                )
                self._audit(
                    case,
                    "sla_breached",
                    principal.id,
                    {
                        "due_at": case.due_at.isoformat(),
                        "overdue_at": instant.isoformat(),
                        "assignee_id": str(case.assignee_id),
                    },
                )
                self._queue_and_notify(
                    case,
                    _SLA_BREACH_TEMPLATE,
                    (case.assignee_id,),
                    {
                        "case_reference": case.reference,
                        "case_id": str(case.id),
                        "borrower_id": str(case.borrower_id),
                        "due_at": case.due_at.isoformat(),
                        "overdue_at": instant.isoformat(),
                    },
                    principal.id,
                    instant,
                )
                escalated.append(case)
        return tuple(escalated)

    escalate = escalate_overdue

    def get_case(
        self,
        principal: Principal,
        case_or_reference: UUID | str,
        *,
        scope: Scope | None = None,
    ) -> Case:
        """Return one case only when it is visible to the caller."""

        _principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_CASE
        )
        return self._load_case(case_or_reference, resolved_scope)

    get = get_case

    def list_cases(
        self,
        principal: Principal,
        *,
        state: str | None = None,
        borrower_id: UUID | None = None,
        scope: Scope | None = None,
    ) -> Sequence[Case]:
        """Return cases through the scope-carrying repository boundary."""

        _principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_CASE
        )
        if state is not None and state not in {item.value for item in CaseState}:
            raise ValidationError(f"Unknown case state {state!r}.", field="case.state")
        return self.repository.list(scope=resolved_scope, state=state, borrower_id=borrower_id)

    list = list_cases

    def history(
        self,
        principal: Principal,
        case_or_reference: UUID | str,
        *,
        scope: Scope | None = None,
    ) -> tuple[CaseEvent, ...]:
        """Return the immutable history of one scoped case."""

        _principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_CASE
        )
        case = self._load_case(case_or_reference, resolved_scope)
        return self.repository.events_for(case.id, scope=resolved_scope)

    history_for = history

    def overdue_cases(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        now: datetime | None = None,
    ) -> tuple[Case, ...]:
        """Read the derived overdue set without changing case state."""

        _principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_CASE
        )
        return self.repository.overdue(self._now(now), scope=resolved_scope)

    def _triage_details(
        self,
        source: object | None,
        *,
        band: str | None,
        borrower_id: UUID | None,
        run_id: UUID | None,
        scope: Scope,
    ) -> tuple[UUID, str, UUID | None]:
        if isinstance(source, Mapping):
            if borrower_id is not None or band is not None or run_id is not None:
                raise ValidationError(
                    "Triage mapping cannot be combined with explicit case inputs.",
                    field="triage_entry",
                )
            borrower_id = _uuid(source.get("borrower_id"), "borrower_id")
            band = source.get("band")
            run_id = _optional_uuid(source.get("run_id"), "run_id")
        elif isinstance(source, TriageEntry):
            source = self._load_triage(source.id, scope)
            borrower_id = source.borrower_id
            band = source.band
            run_id = source.run_id
        elif isinstance(source, UUID):
            if band is None and borrower_id is None:
                try:
                    source_entry = self._load_triage(source, scope)
                except NotFound:
                    raise NotFound(
                        f"Triage entry {source} was not found within the current scope."
                    ) from None
                borrower_id = source_entry.borrower_id
                band = source_entry.band
                run_id = source_entry.run_id
            else:
                borrower_id = borrower_id or source
        elif source is not None:
            raise ValidationError(
                "open_or_update requires a triage entry, borrower UUID, or mapping.",
                field="triage_entry",
            )
        if borrower_id is None:
            raise ValidationError("borrower_id is required.", field="borrower_id")
        if not isinstance(borrower_id, UUID):
            raise ValidationError("borrower_id must be a UUID.", field="borrower_id")
        if band is None:
            raise ValidationError("band is required.", field="band")
        normalized_band = _band(band)
        if run_id is not None and not isinstance(run_id, UUID):
            raise ValidationError("run_id must be a UUID or null.", field="run_id")
        return borrower_id, normalized_band, run_id

    def _load_triage(self, entry_id: UUID, scope: Scope) -> TriageEntry:
        if not isinstance(entry_id, UUID):
            raise ValidationError("triage_entry_id must be a UUID.", field="triage_entry_id")
        ownership = ownership_path_for(TriageEntry)
        statement = ownership.apply(select(TriageEntry)).where(
            scope.predicate(ownership.path_column), TriageEntry.id == entry_id
        )
        entry = self.session.execute(statement).scalars().one_or_none()
        if entry is None:
            raise NotFound(f"Triage entry {entry_id} was not found within the current scope.")
        return cast(TriageEntry, entry)

    def _update_band(
        self,
        case: Case,
        band: str,
        run_id: UUID | None,
        actor_id: UUID,
        instant: datetime,
    ) -> Case:
        previous_band = case.band_at_open
        deadline = derive_sla(band, instant, self.sla_thresholds, calendar=self.calendar)
        case.band_at_open = band
        case.sla_hours = deadline.hours
        case.due_at = deadline.due_at
        case.updated_at = instant
        case.updated_by_id = actor_id
        case.version += 1
        self.session.flush()
        self._append_event(
            case,
            _CASE_BAND_CHANGED_EVENT,
            actor_id,
            instant,
            {
                "from_band": previous_band,
                "to_band": band,
                "run_id": str(run_id) if run_id is not None else None,
                "sla_hours": deadline.hours,
                "due_at": deadline.due_at.isoformat(),
            },
        )
        self._audit(
            case,
            "band_changed",
            actor_id,
            {
                "from_band": previous_band,
                "to_band": band,
                "run_id": str(run_id) if run_id is not None else None,
                "sla_hours": deadline.hours,
                "due_at": deadline.due_at.isoformat(),
            },
        )
        return case

    def _assignment(
        self,
        borrower: Borrower,
        band: str,
        *,
        explicit_assignee_id: UUID | None,
    ) -> _Assignment:
        if explicit_assignee_id is not None:
            user = self.session.scalar(
                select(AppUser).where(
                    AppUser.id == explicit_assignee_id,
                    AppUser.is_active.is_(True),
                )
            )
            if user is None:
                raise ValidationError(
                    f"Assignee {explicit_assignee_id} is not an active user.",
                    field="case.assignee_id",
                )
            return _Assignment(explicit_assignee_id, False, ())

        if self.assignee_resolver is not None:
            mapped = self.assignee_resolver(borrower, band)
            if mapped is not None:
                if not isinstance(mapped, UUID):
                    raise ValidationError(
                        "The portfolio assignee mapping must return a UUID or None.",
                        field="case.assignee_id",
                    )
                if self._active_user(mapped) is None:
                    raise ValidationError(
                        f"Mapped assignee {mapped} is not an active user.",
                        field="case.assignee_id",
                    )
                return _Assignment(mapped, False, ())

        mapped_ids = self._role_mapped_users(borrower, (_RELATIONSHIP_MANAGER_ROLE,))
        if mapped_ids:
            return _Assignment(mapped_ids[0], False, ())

        default_owner = self.default_owner_id
        if self.default_owner_resolver is not None:
            resolved = self.default_owner_resolver(borrower)
            if resolved is not None and not isinstance(resolved, UUID):
                raise ValidationError(
                    "The default owner mapping must return a UUID or None.",
                    field="case.assignee_id",
                )
            default_owner = resolved or default_owner
        if default_owner is None:
            administrators = self._role_mapped_users(borrower, _ADMINISTRATOR_ROLES)
            default_owner = administrators[0] if administrators else None
        if default_owner is None or self._active_user(default_owner) is None:
            raise Conflict(
                f"Borrower {borrower.id} has no portfolio assignee and no active default owner."
            )
        administrators = self._role_mapped_users(borrower, _ADMINISTRATOR_ROLES)
        administrator_ids = tuple(dict.fromkeys((*self.administrator_ids, *administrators)))
        return _Assignment(default_owner, True, administrator_ids)

    def _role_mapped_users(
        self,
        borrower: Borrower,
        role_codes: Sequence[str],
    ) -> tuple[UUID, ...]:
        path = self.session.scalar(
            select(Portfolio.path).where(Portfolio.id == borrower.portfolio_id)
        )
        if not isinstance(path, str):
            raise Conflict(f"Borrower {borrower.id} has no resolvable portfolio mapping.")
        rows = self.session.execute(
            select(
                AppUser.id,
                Portfolio.path,
                UserPortfolioScope.include_descendants,
            )
            .join(UserRole, UserRole.user_id == AppUser.id)
            .join(Role, Role.id == UserRole.role_id)
            .join(UserPortfolioScope, UserPortfolioScope.user_id == AppUser.id)
            .join(Portfolio, Portfolio.id == UserPortfolioScope.portfolio_id)
            .where(
                AppUser.is_active.is_(True),
                Role.code.in_(role_codes),
            )
        ).all()
        exact: list[UUID] = []
        descendants: list[UUID] = []
        for user_id, scope_path, include_descendants in rows:
            if include_descendants and path.startswith(scope_path):
                descendants.append(user_id)
            elif not include_descendants and path == scope_path:
                exact.append(user_id)
        return tuple(sorted((*exact, *descendants), key=lambda value: value.bytes))

    def _active_user(self, user_id: UUID) -> AppUser | None:
        if not isinstance(user_id, UUID):
            return None
        return self.session.scalar(
            select(AppUser).where(AppUser.id == user_id, AppUser.is_active.is_(True))
        )

    def _latest_closed_case(self, borrower_id: UUID) -> Case | None:
        return (
            self.session.execute(
                select(Case)
                .where(Case.borrower_id == borrower_id, Case.state == CaseState.CLOSED.value)
                .order_by(Case.closed_at.desc().nullslast(), Case.created_at.desc(), Case.id.desc())
                .limit(1)
            )
            .scalars()
            .one_or_none()
        )

    def _next_reference(self, borrower: Borrower) -> str:
        base = f"C-{borrower.reference}"
        if len(base) > _CASE_REFERENCE_MAX_LENGTH:
            base = f"C-{borrower.id.hex[:12].upper()}"
        existing = set(self.repository.references_for_borrower(borrower.id))
        if base not in existing:
            return base
        suffix = 2
        while True:
            suffix_text = f"-{suffix}"
            candidate = f"{base[: _CASE_REFERENCE_MAX_LENGTH - len(suffix_text)]}{suffix_text}"
            if candidate not in existing:
                return candidate
            suffix += 1

    def _locked_case(self, case_or_reference: UUID | str, scope: Scope) -> Case:
        case_id = _case_id(case_or_reference)
        case = (
            self.repository.get_for_update(case_id, scope=scope)
            if case_id is not None
            else self.repository.by_reference(cast(str, case_or_reference), scope=scope)
        )
        if case is None:
            label = str(case_or_reference)
            raise NotFound(f"Case {label} was not found within the current scope.")
        return case

    def _load_case(self, case_or_reference: UUID | str, scope: Scope) -> Case:
        case_id = _case_id(case_or_reference)
        case = (
            self.repository.get(case_id, scope=scope)
            if case_id is not None
            else self.repository.by_reference(cast(str, case_or_reference), scope=scope)
        )
        if case is None:
            raise NotFound(f"Case {case_or_reference} was not found within the current scope.")
        return case

    def _append_event(
        self,
        case: Case,
        event_type: str,
        actor_id: UUID,
        instant: datetime,
        payload: Mapping[str, object],
    ) -> CaseEvent:
        event = CaseEvent(
            id=new_id(),
            case_id=case.id,
            event_type=event_type,
            actor_id=actor_id,
            payload=dict(payload),
            occurred_at=instant,
            created_at=instant,
            updated_at=instant,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=self.request_id,
        )
        return self.repository.add_event(event)

    def _audit(
        self,
        case: Case,
        action: str,
        actor_id: UUID,
        payload: Mapping[str, object],
    ) -> None:
        self.audit.record(
            AuditEventType.CASE_LIFECYCLE_CHANGED.value,
            ("case", case.id),
            {"action": action, **dict(payload)},
            actor=actor_id,
            request_id=self.request_id,
        )

    def _queue_and_notify(
        self,
        case: Case,
        template: str,
        recipient_ids: Sequence[UUID],
        payload: Mapping[str, object],
        actor_id: UUID,
        instant: datetime,
    ) -> None:
        recipients = tuple(dict.fromkeys(recipient_ids))
        if not recipients or any(not isinstance(value, UUID) for value in recipients):
            raise Conflict(f"Case {case.reference} has no valid notification recipient.")
        for recipient_id in recipients:
            exists = self.session.scalar(
                select(Notification.id).where(
                    Notification.recipient_id == recipient_id,
                    Notification.template == template,
                    Notification.subject_type == "case",
                    Notification.subject_id == case.id,
                )
            )
            if exists is not None:
                continue
            self.session.add(
                Notification(
                    id=new_id(),
                    recipient_id=recipient_id,
                    channel=_IN_APP_CHANNEL,
                    template=template,
                    subject_type="case",
                    subject_id=case.id,
                    payload=dict(payload),
                    state="pending",
                    attempts=0,
                    created_at=instant,
                    updated_at=instant,
                    created_by_id=actor_id,
                    updated_by_id=actor_id,
                    request_id=self.request_id,
                )
            )
        self.session.flush()
        if self.notifier is not None:
            _invoke_notifier(self.notifier, template, recipients, case, payload)

    def _authorized_context(
        self,
        principal: Principal,
        scope: Scope | None,
        permission: Permission,
    ) -> tuple[Principal, Scope]:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, permission)
        resolved = self.scope_resolver(principal) if scope is None else scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The resolved scope does not belong to the authenticated principal."
            )
        return principal, resolved

    def _now(self, value: datetime | None) -> datetime:
        instant = self.clock.now() if value is None else value
        if (
            not isinstance(instant, datetime)
            or instant.tzinfo is None
            or instant.utcoffset() is None
        ):
            raise ValueError("Case service clock must return an aware datetime.")
        return instant.astimezone(UTC)


class _Assignment:
    __slots__ = ("assignee_id", "used_fallback", "administrators")

    def __init__(
        self,
        assignee_id: UUID,
        used_fallback: bool,
        administrators: Sequence[UUID],
    ) -> None:
        self.assignee_id = assignee_id
        self.used_fallback = used_fallback
        self.administrators = tuple(administrators)


def _single_source(first: object | None, second: object | None) -> object | None:
    if first is not None and second is not None:
        raise ValidationError("Provide one triage source, not two.", field="triage_entry")
    return first if first is not None else second


def _band(value: object) -> str:
    raw = value.value if isinstance(value, str) is False and hasattr(value, "value") else value
    if not isinstance(raw, str) or raw.strip().lower() not in _ALL_BANDS:
        raise ValidationError(
            f"Case band must be one of {', '.join(sorted(_ALL_BANDS))}.", field="case.band"
        )
    return raw.strip().lower()


def _case_id(value: UUID | str) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("case id or reference is required.", field="case")
    try:
        return UUID(value.strip())
    except ValueError:
        return None


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value.strip())
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} must be a UUID.", field=field)


def _optional_uuid(value: object, field: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, field)


def _expected_version(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("expected_version must be a positive integer.", field="version")


def _closure_reason(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("A closure reason is required.", field="case.closure_reason")
    normalized = value.strip()
    if len(normalized) > _CLOSURE_REASON_MAX_LENGTH:
        raise ValidationError(
            f"A closure reason must be at most {_CLOSURE_REASON_MAX_LENGTH} characters.",
            field="case.closure_reason",
        )
    return normalized


def _closure_note(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("A closure note must be text.", field="case.closure_note")
    normalized = value.strip()
    if len(normalized) > _CLOSURE_NOTE_MAX_LENGTH:
        raise ValidationError(
            f"A closure note must be at most {_CLOSURE_NOTE_MAX_LENGTH} characters.",
            field="case.closure_note",
        )
    return normalized or None


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(
            f"Case request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
        )
    return value.strip()


def _invoke_notifier(
    notifier: CaseNotifier | Callable[..., object],
    template: str,
    recipient_ids: Sequence[UUID],
    case: Case,
    payload: Mapping[str, object],
) -> None:
    """Call the documented notifier shape while supporting keyword adapters."""

    method = getattr(notifier, "notify", notifier)
    if not callable(method):
        raise TypeError("notifier must expose notify() or be callable.")
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        method(template, tuple(recipient_ids), payload)
        return
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        method(
            template=template,
            recipient_ids=tuple(recipient_ids),
            case=case,
            payload=payload,
        )
        return
    names = set(parameters)
    if {"template", "recipient_ids", "payload"}.issubset(names):
        method(
            template=template,
            recipient_ids=tuple(recipient_ids),
            payload=payload,
        )
        return
    positional = [template, tuple(recipient_ids), payload]
    required = [
        parameter
        for parameter in parameters.values()
        if parameter.default is parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(required) <= len(positional):
        method(*positional[: len(required)])
        return
    raise TypeError(
        "notifier.notify must accept template, recipient_ids and payload, "
        "or provide a compatible adapter."
    )


__all__ = [
    "AssigneeResolver",
    "AuditWriter",
    "CaseNotifier",
    "CaseService",
    "DefaultOwnerResolver",
]
