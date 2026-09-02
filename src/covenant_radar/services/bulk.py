"""Scoped, auditable bulk case operations.

Bulk actions deliberately run one item at a time inside a savepoint.  A bad
selection therefore cannot roll back successful items, while the enclosing
request transaction still commits the detail events and the summary as one
unit.  The same scoped repository lookup is used for every item; callers
never receive an out-of-scope/missing distinction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, DomainError, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.identity import AppUser, UserPortfolioScope
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Case, CaseEvent
from covenant_radar.db.repositories.case import CaseRepository
from covenant_radar.db.scoping import (
    Scope,
    grant_reaches_path,
    resolve_portfolio_path,
    resolve_scope,
)
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.cases.lifecycle import CaseState, transition_result, validate_state
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_MAX_ITEMS: Final[int] = 5_000
_MAX_FILTERS: Final[int] = 30
_MAX_FILTER_VALUE_LENGTH: Final[int] = 500


class BulkAction(StrEnum):
    """Actions that can be applied to selected cases."""

    ASSIGN = "assign"
    STATE = "state"
    WATCHLIST = "watchlist"


class BulkAuditWriter(Protocol):
    """The append-only audit boundary required by bulk mutations."""

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
        ...


@dataclass(frozen=True, slots=True)
class BulkItemResult:
    """The durable outcome-shaped result for one requested item."""

    item_id: str
    status: str
    reason: str | None = None
    case_id: UUID | None = None
    case_reference: str | None = None
    changed: bool = False

    @property
    def succeeded(self) -> bool:
        """Whether this item was applied, including an idempotent no-op."""

        return self.status == "succeeded"


@dataclass(frozen=True, slots=True)
class BulkOperationReport:
    """A complete, JSON-safe report for a bulk operation."""

    operation_id: UUID
    action: str
    requested_count: int
    successes: tuple[BulkItemResult, ...]
    failures: tuple[BulkItemResult, ...]
    excluded: tuple[BulkItemResult, ...]
    filter: Mapping[str, object]
    started_at: datetime
    finished_at: datetime
    ordered_items: tuple[BulkItemResult, ...] = ()

    @property
    def items(self) -> tuple[BulkItemResult, ...]:
        """Return item outcomes in the original request order."""

        return self.ordered_items or (*self.successes, *self.failures, *self.excluded)

    @property
    def success_count(self) -> int:
        return len(self.successes)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)

    @property
    def outcome_distribution(self) -> Mapping[str, int]:
        return {
            "succeeded": self.success_count,
            "failed": self.failure_count,
            "excluded": self.excluded_count,
        }

    @property
    def partial_success(self) -> bool:
        return bool(self.successes) and bool(self.failures or self.excluded)

    def as_dict(self) -> dict[str, object]:
        """Return the report used by both HTML and JSON presenters."""

        return {
            "operation_id": str(self.operation_id),
            "action": self.action,
            "requested_count": self.requested_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "excluded_count": self.excluded_count,
            "outcome_distribution": dict(self.outcome_distribution),
            "filter": dict(self.filter),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "items": [
                {
                    "item_id": item.item_id,
                    "status": item.status,
                    "reason": item.reason,
                    "case_id": str(item.case_id) if item.case_id is not None else None,
                    "case_reference": item.case_reference,
                    "changed": item.changed,
                }
                for item in self.items
            ],
        }


class BulkService:
    """Apply selected case operations under the caller's portfolio scope."""

    def __init__(
        self,
        session: Session,
        *,
        audit: BulkAuditWriter,
        case_repository: CaseRepository | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
        max_items: int = _MAX_ITEMS,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("BulkService requires a SQLAlchemy Session.")
        if not callable(getattr(audit, "record", None)):
            raise TypeError("BulkService requires an append-only audit writer.")
        if case_repository is not None and not isinstance(case_repository, CaseRepository):
            raise TypeError("case_repository must be a CaseRepository.")
        if case_repository is not None and case_repository.session is not session:
            raise ValueError("case_repository and session must be identical.")
        if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items < 1:
            raise ValueError("max_items must be a positive integer.")
        if max_items > _MAX_ITEMS:
            raise ValueError(f"max_items cannot exceed {_MAX_ITEMS}.")

        self.session = session
        self.audit = audit
        self.repository = case_repository or CaseRepository(session)
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())
        self.max_items = max_items

    def execute(
        self,
        principal: Principal,
        item_ids: Sequence[UUID | str] | None = None,
        action: BulkAction | str | None = None,
        *,
        case_ids: Sequence[UUID | str] | None = None,
        value: object | None = None,
        target: object | None = None,
        filters: Mapping[str, object] | None = None,
        scope: Scope | None = None,
        now: datetime | None = None,
    ) -> BulkOperationReport:
        """Apply one action and return every requested item's outcome.

        ``case_ids`` and ``target`` are compatibility-friendly aliases for
        callers that use domain terminology rather than the HTTP form names.
        Exactly one of each pair may be supplied.
        """

        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        if item_ids is not None and case_ids is not None:
            raise ValidationError("Provide item_ids or case_ids, not both.", field="case_ids")
        selected = tuple(item_ids if item_ids is not None else case_ids or ())
        if len(selected) > self.max_items:
            raise ValidationError(
                f"A bulk operation may contain at most {self.max_items} items.", field="case_ids"
            )
        if value is not None and target is not None:
            raise ValidationError("Provide value or target, not both.", field="value")
        resolved_value = value if value is not None else target
        resolved_action = _action(action)
        filter_values = _filters(filters)
        resolved_scope = self._scope(principal, scope)
        started = self._now(now)
        operation_id = new_id()

        successes: list[BulkItemResult] = []
        failures: list[BulkItemResult] = []
        excluded: list[BulkItemResult] = []
        ordered_items: list[BulkItemResult] = []
        seen: set[str] = set()

        for raw_item in selected:
            label = _item_label(raw_item)
            if label in seen:
                result = BulkItemResult(
                    label, "excluded", "Duplicate selection; no second update was run."
                )
                excluded.append(result)
                ordered_items.append(result)
                continue
            seen.add(label)
            result = self._apply_one(
                principal,
                raw_item,
                label,
                resolved_action,
                resolved_value,
                operation_id,
                resolved_scope,
                started,
            )
            if result.status == "succeeded":
                successes.append(result)
            elif result.status == "excluded":
                excluded.append(result)
            else:
                failures.append(result)
            ordered_items.append(result)

        finished = self._now(now)
        report = BulkOperationReport(
            operation_id=operation_id,
            action=resolved_action.value,
            requested_count=len(selected),
            successes=tuple(successes),
            failures=tuple(failures),
            excluded=tuple(excluded),
            filter=filter_values,
            started_at=started,
            finished_at=finished,
            ordered_items=tuple(ordered_items),
        )
        self.audit.record(
            AuditEventType.CASE_LIFECYCLE_CHANGED.value,
            ("bulk_operation", operation_id),
            {
                "action": resolved_action.value,
                "bulk_operation_id": str(operation_id),
                "summary": True,
                "filter": dict(filter_values),
                "requested_count": report.requested_count,
                "success_count": report.success_count,
                "failure_count": report.failure_count,
                "excluded_count": report.excluded_count,
                "outcome_distribution": dict(report.outcome_distribution),
            },
            actor=principal.id,
            request_id=self.request_id,
        )
        return report

    apply = execute
    run = execute

    def _apply_one(
        self,
        principal: Principal,
        raw_item: UUID | str,
        label: str,
        action: BulkAction,
        value: object | None,
        operation_id: UUID,
        scope: Scope,
        instant: datetime,
    ) -> BulkItemResult:
        if not principal.has(Permission.UPDATE_CASE):
            return BulkItemResult(
                label,
                "excluded",
                f"Missing permission: {Permission.UPDATE_CASE.value}.",
            )

        try:
            with self.session.begin_nested():
                case = self._case(raw_item, scope)
                if case is None:
                    return BulkItemResult(
                        label,
                        "excluded",
                        "Case was not found within the current portfolio scope.",
                    )
                old_state = case.state
                old_assignee = case.assignee_id
                payload, event_type, changed = self._mutate(case, action, value, instant)
                if changed:
                    case.updated_at = instant
                    case.updated_by_id = principal.id
                    case.version += 1
                    self.session.flush()
                    self.repository.add_event(
                        CaseEvent(
                            id=new_id(),
                            case_id=case.id,
                            event_type=event_type,
                            actor_id=principal.id,
                            payload={
                                "bulk_operation_id": str(operation_id),
                                "action": action.value,
                                **payload,
                            },
                            occurred_at=instant,
                            created_at=instant,
                            updated_at=instant,
                            created_by_id=principal.id,
                            updated_by_id=principal.id,
                            request_id=self.request_id,
                        )
                    )
                self.audit.record(
                    AuditEventType.CASE_LIFECYCLE_CHANGED.value,
                    ("case", case.id),
                    {
                        "action": action.value,
                        "bulk_operation_id": str(operation_id),
                        "outcome": "succeeded",
                        "changed": changed,
                        "from_state": old_state,
                        "to_state": case.state,
                        "from_assignee_id": str(old_assignee) if old_assignee else None,
                        "to_assignee_id": str(case.assignee_id) if case.assignee_id else None,
                    },
                    actor=principal.id,
                    request_id=self.request_id,
                )
                return BulkItemResult(
                    label,
                    "succeeded",
                    case_id=case.id,
                    case_reference=case.reference,
                    changed=changed,
                )
        except DomainError as error:
            return BulkItemResult(label, "failed", error.message)
        except Exception:
            # The savepoint has already isolated the item.  Do not expose
            # database/provider internals to the caller or silently omit it.
            return BulkItemResult(label, "failed", "The case could not be updated.")

    def _mutate(
        self,
        case: Case,
        action: BulkAction,
        value: object | None,
        instant: datetime,
    ) -> tuple[dict[str, object], str, bool]:
        if action is BulkAction.ASSIGN:
            assignee_id = _uuid(value, "assignee_id")
            user = self.session.scalar(
                select(AppUser).where(AppUser.id == assignee_id, AppUser.is_active.is_(True))
            )
            if user is None:
                raise ValidationError(
                    f"Assignee {assignee_id} is not an active user.", field="assignee_id"
                )
            self._require_assignee_in_case_scope(case, assignee_id)
            changed = case.assignee_id != assignee_id
            if changed:
                case.assignee_id = assignee_id
            return {"assignee_id": str(assignee_id)}, "bulk_assigned", changed

        target_state, closure_reason = _state_value(action, value)
        if case.state == target_state.value:
            return {"state": target_state.value}, _event_type(action), False
        change = transition_result(case.state, target_state, closure_reason=closure_reason)
        case.state = change.to_state.value
        if change.to_state is CaseState.CLOSED:
            case.closed_at = instant
            case.closure_reason = closure_reason
        return (
            {
                "from_state": change.from_state.value,
                "to_state": change.to_state.value,
                "closure_reason_recorded": change.to_state is CaseState.CLOSED,
            },
            _event_type(action),
            True,
        )

    def _require_assignee_in_case_scope(self, case: Case, assignee_id: UUID) -> None:
        """Reject an assignee whose portfolio grants do not reach this case.

        The case-detail screen has always enforced this
        (`web/routes/cases.py::_assign`), but bulk assign only checked that
        the user was active.  While bulk was unreachable from the UI that gap
        cost nothing; the portfolio queue now offers bulk assign over an
        entire portfolio, so a hand-made POST naming any active user's id
        would otherwise assign a case outside that user's scope.  Restricting
        the select is not sufficient on its own — the server has to refuse.
        """

        case_path = resolve_portfolio_path(self.session, case)
        if case_path is None:
            raise ValidationError(
                "The case has no resolvable portfolio and cannot be assigned.",
                field="assignee_id",
            )
        grants = self.session.execute(
            select(Portfolio.path, UserPortfolioScope.include_descendants)
            .join(UserPortfolioScope, UserPortfolioScope.portfolio_id == Portfolio.id)
            .where(UserPortfolioScope.user_id == assignee_id)
        ).all()
        if any(
            grant_reaches_path(granted_path, case_path, bool(include_descendants))
            for granted_path, include_descendants in grants
        ):
            return
        raise ValidationError(
            "The selected assignee is not an active user in this case's portfolio scope.",
            field="assignee_id",
        )

    def _case(self, raw_item: UUID | str, scope: Scope) -> Case | None:
        if isinstance(raw_item, UUID):
            return self.repository.get_for_update(raw_item, scope=scope)
        if not isinstance(raw_item, str) or not raw_item.strip():
            raise ValidationError(
                "Each selected case must have an id or reference.", field="case_ids"
            )
        try:
            case_id = UUID(raw_item.strip())
        except ValueError:
            return self.repository.by_reference(raw_item.strip(), scope=scope)
        return self.repository.get_for_update(case_id, scope=scope)

    def _scope(self, principal: Principal, scope: Scope | None) -> Scope:
        resolved = self.scope_resolver(principal) if scope is None else scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The resolved scope does not belong to the authenticated principal."
            )
        return resolved

    def _now(self, value: datetime | None) -> datetime:
        instant = self.clock.now() if value is None else value
        if (
            not isinstance(instant, datetime)
            or instant.tzinfo is None
            or instant.utcoffset() is None
        ):
            raise ValueError("BulkService clock must return a timezone-aware datetime.")
        return instant.astimezone(UTC)


def _action(value: BulkAction | str | None) -> BulkAction:
    if isinstance(value, BulkAction):
        return value
    if not isinstance(value, str):
        raise ValidationError("A bulk action is required.", field="action")
    normalized = value.strip().lower().replace("_", "-")
    aliases = {"assignment": "assign", "status": "state", "watch-list": "watchlist"}
    try:
        return BulkAction(aliases.get(normalized, normalized))
    except ValueError as error:
        raise ValidationError(
            "Bulk action must be assign, state or watchlist.", field="action"
        ) from error


def _state_value(action: BulkAction, value: object) -> tuple[CaseState, str | None]:
    if action is BulkAction.WATCHLIST:
        if value is not None and isinstance(value, Mapping):
            enabled = value.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValidationError(
                    "watchlist.enabled must be boolean.", field="watchlist.enabled"
                )
            if not enabled:
                raise ValidationError(
                    "Removing a case from the watchlist requires a target state.",
                    field="watchlist.enabled",
                )
        elif value is not None and value is not True:
            raise ValidationError("watchlist action only supports enabled=true.", field="watchlist")
        return CaseState.MONITORING, None

    raw_state = value
    closure_reason: str | None = None
    if isinstance(value, Mapping):
        raw_state = value.get("state")
        raw_reason = value.get("reason")
        if raw_reason is not None and not isinstance(raw_reason, str):
            raise ValidationError("state.reason must be text.", field="state.reason")
        closure_reason = raw_reason.strip() if isinstance(raw_reason, str) else None
    return validate_state(cast(CaseState | str, raw_state)), closure_reason


def _event_type(action: BulkAction) -> str:
    return {
        BulkAction.ASSIGN: "bulk_assigned",
        BulkAction.STATE: "bulk_state_changed",
        BulkAction.WATCHLIST: "bulk_watchlist_changed",
    }[action]


def _item_label(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return str(value)


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value.strip())
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} must be a UUID.", field=field)


def _filters(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError("filters must be an object.", field="filters")
    if len(value) > _MAX_FILTERS:
        raise ValidationError(
            f"filters may contain at most {_MAX_FILTERS} fields.", field="filters"
        )
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("filter names must be non-empty text.", field="filters")
        if isinstance(item, str) and len(item) > _MAX_FILTER_VALUE_LENGTH:
            raise ValidationError(
                f"filter {key!r} exceeds {_MAX_FILTER_VALUE_LENGTH} characters.", field="filters"
            )
        if isinstance(item, float) and not isfinite(item):
            raise ValidationError(f"filter {key!r} must be finite.", field="filters")
        if not isinstance(item, str | int | float | bool | UUID | None):
            raise ValidationError(f"filter {key!r} has an unsupported value.", field="filters")
        result[key.strip()] = str(item) if isinstance(item, UUID) else item
    return result


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(
            f"Bulk request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
        )
    return value.strip()


__all__ = ["BulkAction", "BulkItemResult", "BulkOperationReport", "BulkService"]
