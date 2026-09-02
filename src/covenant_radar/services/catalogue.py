"""Use cases for the bank-owned intervention catalogue.

The catalogue is deliberately the only source from which recommendations and
simulation inputs may be selected.  Writes are staged through the shared
maker-checker service and the approval callback applies the exact validated
payload in the caller's transaction.  This keeps the catalogue change and its
audit event atomic without giving templates or model output a write path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.forecast import Intervention
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.interventions.catalogue import (
    CatalogueEntry,
    RoleTag,
    normalize_role_tag,
)
from covenant_radar.domain.interventions.effects import EffectModelType, InterventionFacts
from covenant_radar.security.maker_checker import (
    ApplicationCallbackRegistry,
    ApprovalNotifier,
    MakerCheckerRequest,
    MakerCheckerSettings,
    MakerCheckerState,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind
from covenant_radar.services.approvals import ApprovalService
from covenant_radar.services.registry import SqlAlchemyMakerCheckerRepository

CATALOGUE_OPERATION: Final[str] = "intervention_catalogue_change"
# T-098 predates a dedicated catalogue permission in the frozen permission
# vocabulary.  The existing threshold proposal/check pair is the only
# administrative pair with distinct permissions and is used here for the
# same maker-checker configuration surface.
CATALOGUE_PROPOSE_PERMISSION: Final[Permission] = Permission.PROPOSE_THRESHOLDS
CATALOGUE_APPROVE_PERMISSION: Final[Permission] = Permission.APPROVE_THRESHOLDS
_REQUEST_ID_MAX_LENGTH: Final[int] = 40


class CatalogueAuditWriter(Protocol):
    """The append-only audit boundary required by maker-checker."""

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


class _CatalogueNotifier(ApprovalNotifier):
    """Notification sink for the service default.

    Expiry notification delivery is owned by the later notification task.
    Keeping this sink explicit means expiry remains safe and observable in
    tests and does not silently call an unavailable external system.
    """

    def notify(self, _event_type: str, _payload: Mapping[str, object]) -> object:
        return None


@dataclass(frozen=True, slots=True)
class CatalogueWrite:
    """Result of a catalogue write or a pending maker-checker proposal."""

    entry: CatalogueEntry
    approval_request: MakerCheckerRequest | None
    applied: bool

    def __post_init__(self) -> None:
        if not isinstance(self.entry, CatalogueEntry):
            raise TypeError("entry must be a CatalogueEntry.")
        if self.approval_request is not None and not isinstance(
            self.approval_request, MakerCheckerRequest
        ):
            raise TypeError("approval_request must be a MakerCheckerRequest or None.")
        if not isinstance(self.applied, bool):
            raise TypeError("applied must be boolean.")

    @property
    def pending(self) -> bool:
        """Whether the change awaits a checker."""

        return self.approval_request is not None and not self.applied

    @property
    def request(self) -> MakerCheckerRequest | None:
        """Short alias for the pending approval request."""

        return self.approval_request


class CatalogueService:
    """Manage, resolve and filter the intervention catalogue."""

    def __init__(
        self,
        session: Session,
        *,
        audit: CatalogueAuditWriter | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
        approval_service: ApprovalService | None = None,
        maker_checker_enabled: bool = True,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("CatalogueService requires a SQLAlchemy Session.")
        if audit is not None and not callable(getattr(audit, "record", None)):
            raise TypeError("CatalogueService audit must provide a callable record method.")
        if approval_service is not None and not isinstance(approval_service, ApprovalService):
            raise TypeError("approval_service must be an ApprovalService.")
        if not isinstance(maker_checker_enabled, bool):
            raise TypeError("maker_checker_enabled must be boolean.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = _request_id(
            request_id if request_id is not None else get_request_id() or new_request_id()
        )
        self.approvals = approval_service
        if self.approvals is None and audit is not None:
            self.approvals = ApprovalService(
                SqlAlchemyMakerCheckerRepository(session, request_id=self.request_id),
                audit,
                registry=ApplicationCallbackRegistry(),
                clock=self.clock,
                settings=MakerCheckerSettings(
                    enabled_operations={CATALOGUE_OPERATION: maker_checker_enabled}
                ),
                notifier=_CatalogueNotifier(),
                request_id=self.request_id,
            )
        if self.approvals is not None:
            if self.approvals.request_id != self.request_id:
                self.request_id = _request_id(self.approvals.request_id)
            self._wire_approval_callback()

    # ---- read use cases -------------------------------------------------

    def list(
        self,
        *,
        active_only: bool | None = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[CatalogueEntry, ...]:
        """Return catalogue rows in stable code order.

        ``active_only=None`` is the historical view and is intentionally
        available to memo reconstruction; retired rows are never deleted.
        """

        if active_only is not None and not isinstance(active_only, bool):
            raise ValidationError("active_only must be a boolean or null.", field="active_only")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must not be negative.", field="offset")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200
        ):
            raise ValidationError("limit must be between 1 and 200.", field="limit")
        statement = select(Intervention).order_by(Intervention.code, Intervention.id)
        if active_only is True:
            statement = statement.where(Intervention.is_active.is_(True))
        elif active_only is False:
            statement = statement.where(Intervention.is_active.is_(False))
        rows = tuple(self.session.execute(statement).scalars().all())
        entries = tuple(self._entry_from_row(row) for row in rows)
        if limit is None:
            return entries[offset:]
        return entries[offset : offset + limit]

    def get(self, code: str, *, include_retired: bool = True) -> CatalogueEntry:
        """Resolve one entry, including retired history by default."""

        if not isinstance(include_retired, bool):
            raise ValidationError("include_retired must be boolean.", field="include_retired")
        normalized = _code(code)
        row = self.session.scalar(select(Intervention).where(Intervention.code == normalized))
        if row is None or (not include_retired and not row.is_active):
            raise NotFound(f"Intervention {normalized!r} was not found.")
        return self._entry_from_row(row)

    resolve = get
    resolve_historical = get

    def find(self, code: str, *, include_retired: bool = True) -> CatalogueEntry | None:
        """Return one entry or ``None`` for optional catalogue lookups."""

        try:
            return self.get(code, include_retired=include_retired)
        except NotFound:
            return None

    def applicable(
        self,
        covenant_class: str,
        *,
        role_tag: RoleTag | str | None = None,
    ) -> tuple[CatalogueEntry, ...]:
        """Return only active, simulator-backed applicable entries."""

        normalized_class = _covenant_class(covenant_class)
        if role_tag is None:
            normalized_role = None
        else:
            try:
                normalized_role = normalize_role_tag(role_tag)
            except (TypeError, ValueError) as error:
                raise ValidationError(str(error), field="intervention.role_tag") from error
        candidates = self.list(active_only=True)
        result: list[CatalogueEntry] = []
        for entry in candidates:
            if normalized_role is not None and entry.role_tag is not normalized_role:
                continue
            if entry.is_applicable(normalized_class):
                result.append(entry)
        return tuple(result)

    for_recommendation = applicable
    for_simulation = applicable
    recommendations = applicable

    def for_simulation_facts(
        self,
        code: str,
        covenant_class: str,
    ) -> InterventionFacts:
        """Resolve an active catalogue entry as deterministic simulator facts."""

        entry = self.get(code, include_retired=False)
        return entry.for_simulation(_covenant_class(covenant_class))

    intervention_for_simulation = for_simulation_facts

    def pending_approvals(self, principal: Principal) -> tuple[MakerCheckerRequest, ...]:
        """Return pending catalogue changes visible to an eligible checker."""

        approvals = self._approval_service()
        return tuple(
            request
            for request in approvals.list_pending(principal)
            if request.operation == CATALOGUE_OPERATION
        )

    # ---- write use cases ------------------------------------------------

    def save(
        self,
        principal: Principal,
        entry: CatalogueEntry | Mapping[str, object] | None = None,
        *,
        expected_version: int | None = None,
        **fields: object,
    ) -> CatalogueWrite:
        """Create or amend an entry through maker-checker."""

        _principal(principal)
        candidate = _coerce_entry(entry, fields)
        existing = self.session.scalar(
            select(Intervention).where(Intervention.code == candidate.code)
        )
        if existing is not None:
            if candidate.database_id is not None and candidate.database_id != existing.id:
                raise Conflict(
                    f"Intervention code {candidate.code!r} is already assigned to another row."
                )
            candidate = candidate.with_database_id(existing.id)
        elif (
            candidate.database_id is not None
            and self.session.scalar(
                select(Intervention).where(Intervention.id == candidate.database_id)
            )
            is not None
        ):
            raise Conflict(
                f"Intervention record {candidate.database_id} is already assigned to another code."
            )

        current_version = existing.version if existing is not None else 0
        version = current_version if expected_version is None else _version(expected_version)
        if version != current_version:
            raise Conflict(
                f"Intervention {candidate.code!r} changed from version {version}; "
                f"the current version is {current_version}."
            )
        database_id = candidate.database_id or new_id()
        candidate = candidate.with_database_id(database_id)
        payload = self._change_payload(candidate, expected_version=current_version)
        approvals = self._approval_service()
        self._refuse_duplicate_pending(approvals, database_id, candidate.code)
        request = approvals.submit(
            CATALOGUE_OPERATION,
            ("intervention", database_id),
            payload,
            principal,
        )
        applied = request.state is MakerCheckerState.APPROVED
        if applied:
            row = self.session.get(Intervention, database_id)
            if row is None:
                raise Conflict("Approved intervention change did not create a catalogue row.")
            return CatalogueWrite(self._entry_from_row(row), request, applied=True)
        return CatalogueWrite(candidate, request, applied=False)

    create = save
    update = save
    add = save

    def retire(
        self,
        principal: Principal,
        code: str,
        *,
        expected_version: int | None = None,
    ) -> CatalogueWrite:
        """Retire an entry without deleting its historical identity."""

        entry = self.get(code)
        if entry.is_retired:
            raise Conflict(f"Intervention {entry.code!r} is already retired.")
        return self.save(
            principal,
            entry.retire(self._now()),
            expected_version=expected_version,
        )

    retire_entry = retire

    def decide_approval(
        self,
        principal: Principal,
        request_id: UUID | str,
        *,
        approved: bool,
        reason: str | None = None,
    ) -> MakerCheckerRequest:
        """Approve or reject one catalogue change, never another operation."""

        approvals = self._approval_service()
        pending = next(
            (
                request
                for request in approvals.repository.list_pending()
                if request.id == _request_uuid(request_id)
            ),
            None,
        )
        if pending is None:
            raise NotFound(f"Catalogue approval {request_id!r} was not found or is not pending.")
        if pending.operation != CATALOGUE_OPERATION:
            raise Conflict("The supplied approval is not an intervention catalogue change.")
        return approvals.decide(request_id, principal, approved, reason)

    # ---- maker-checker callback ----------------------------------------

    def _wire_approval_callback(self) -> None:
        approvals = self._approval_service()
        registry = approvals.registry
        if CATALOGUE_OPERATION in registry:
            definition = registry.get(CATALOGUE_OPERATION)
            if definition.callback != self._apply_approved_change:
                raise Conflict(
                    f"Maker-checker operation {CATALOGUE_OPERATION!r} is already wired "
                    "to a different callback."
                )
            return
        registry.register(
            CATALOGUE_OPERATION,
            self._apply_approved_change,
            propose_permission=CATALOGUE_PROPOSE_PERMISSION,
            approve_permission=CATALOGUE_APPROVE_PERMISSION,
        )

    def _apply_approved_change(
        self,
        request: MakerCheckerRequest,
        actor_id: UUID,
    ) -> Intervention:
        payload = request.payload
        if payload.get("action") != "upsert":
            raise ValidationError("Unknown catalogue approval action.", field="payload.action")
        raw_entry = payload.get("entry")
        if not isinstance(raw_entry, Mapping):
            raise ValidationError("Catalogue approval payload has no entry.", field="payload.entry")
        expected_fields = {
            "code",
            "role_tag",
            "text",
            "effect_model",
            "effect_parameters",
            "applicable_covenant_classes",
            "requires_approval",
            "is_active",
            "retired_at",
            "database_id",
        }
        unknown_fields = set(raw_entry) - expected_fields
        if unknown_fields:
            raise ValidationError(
                f"Unknown catalogue entry field {sorted(unknown_fields)[0]!r}.",
                field="payload.entry",
            )
        expected_version = payload.get("expected_version")
        expected = _version(expected_version)
        database_id = _uuid(raw_entry.get("database_id"), "entry.database_id")
        if request.subject_type != "intervention" or request.subject_id != database_id:
            raise ValidationError("Catalogue approval subject does not match its entry.")
        candidate = _coerce_entry(raw_entry, {}).with_database_id(database_id)
        row = self.session.get(Intervention, database_id)
        if expected == 0:
            if row is not None:
                raise Conflict(f"Intervention {database_id} already exists.")
            duplicate = self.session.scalar(
                select(Intervention).where(Intervention.code == candidate.code)
            )
            if duplicate is not None:
                raise Conflict(f"Intervention code {candidate.code!r} already exists.")
            now = self._now()
            values = candidate.to_record_values()
            row = Intervention(
                id=database_id,
                code=cast(str, values["code"]),
                role_tag=cast(str, values["role_tag"]),
                text=cast(str, values["text"]),
                effect_model=cast(str, values["effect_model"]),
                effect_parameters=cast(dict[str, object], values["effect_parameters"]),
                applicable_covenant_classes=cast(list[str], values["applicable_covenant_classes"]),
                requires_approval=cast(bool, values["requires_approval"]),
                is_active=cast(bool, values["is_active"]),
                retired_at=(
                    _retired_at(values["retired_at"], fallback=now)
                    if not cast(bool, values["is_active"])
                    else None
                ),
                created_at=now,
                updated_at=now,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                request_id=self.request_id,
                version=1,
            )
            self.session.add(row)
        else:
            if row is None:
                raise Conflict(f"Intervention {database_id} no longer exists.")
            if row.version != expected:
                raise Conflict(
                    f"Intervention {candidate.code!r} is at version {row.version}, "
                    f"not the proposed version {expected}."
                )
            if row.code != candidate.code:
                raise Conflict("An intervention code cannot be changed after creation.")
            self._update_row(row, candidate, actor_id)
        try:
            self.session.flush()
        except IntegrityError as error:
            raise Conflict(
                f"Intervention {candidate.code!r} conflicts with existing data."
            ) from error
        return row

    def _update_row(self, row: Intervention, candidate: CatalogueEntry, actor_id: UUID) -> None:
        values = candidate.to_record_values()
        now = self._now()
        row.role_tag = cast(str, values["role_tag"])
        row.text = cast(str, values["text"])
        row.effect_model = cast(str, values["effect_model"])
        row.effect_parameters = cast(dict[str, object], values["effect_parameters"])
        row.applicable_covenant_classes = cast(list[str], values["applicable_covenant_classes"])
        row.requires_approval = cast(bool, values["requires_approval"])
        row.is_active = cast(bool, values["is_active"])
        row.retired_at = (
            _retired_at(values["retired_at"], fallback=now)
            if not cast(bool, values["is_active"])
            else None
        )
        row.updated_at = now
        row.updated_by_id = actor_id
        row.request_id = self.request_id
        row.version += 1

    def _change_payload(self, entry: CatalogueEntry, *, expected_version: int) -> dict[str, object]:
        values = entry.to_record_values()
        values["database_id"] = str(entry.database_id)
        return {"action": "upsert", "expected_version": expected_version, "entry": values}

    def _refuse_duplicate_pending(
        self,
        approvals: ApprovalService,
        database_id: UUID,
        code: str,
    ) -> None:
        if not approvals.settings.is_enabled(CATALOGUE_OPERATION):
            return
        for request in approvals.repository.list_pending():
            if request.operation != CATALOGUE_OPERATION:
                continue
            entry = request.payload.get("entry")
            if not isinstance(entry, Mapping):
                continue
            if entry.get("database_id") == str(database_id) or entry.get("code") == code:
                raise Conflict(f"Intervention {code!r} already has a pending catalogue change.")

    def _entry_from_row(self, row: Intervention) -> CatalogueEntry:
        try:
            return CatalogueEntry.from_record(row)
        except (TypeError, ValueError, ValidationError) as error:
            raise ValidationError(
                f"Intervention {row.code!r} is invalid and cannot be used: {error}.",
                field="intervention",
            ) from error

    def _approval_service(self) -> ApprovalService:
        if self.approvals is None:
            raise ValidationError(
                "Catalogue writes require an append-only audit writer and maker-checker service.",
                field="catalogue.approval",
            )
        return self.approvals

    def _now(self) -> datetime:
        value = self.clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("Catalogue clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)


def _coerce_entry(
    entry: CatalogueEntry | Mapping[str, object] | None,
    fields: Mapping[str, object],
) -> CatalogueEntry:
    if entry is not None and fields:
        if not isinstance(entry, Mapping):
            raise TypeError("Catalogue fields cannot be combined with a CatalogueEntry.")
        values = dict(entry)
        values.update(fields)
    elif isinstance(entry, Mapping):
        values = dict(entry)
    elif isinstance(entry, CatalogueEntry):
        if fields:
            raise TypeError("Catalogue fields cannot be combined with a CatalogueEntry.")
        return entry
    elif entry is None:
        values = dict(fields)
    else:
        raise TypeError("entry must be a CatalogueEntry or a mapping.")

    raw_retired_at = values.get("retired_at")
    if isinstance(raw_retired_at, str):
        try:
            raw_retired_at = datetime.fromisoformat(raw_retired_at)
        except ValueError as error:
            raise ValidationError(
                "retired_at must be an ISO timestamp.", field="intervention.retired_at"
            ) from error
    raw_parameters = values.get("effect_parameters")
    if raw_parameters is not None and not isinstance(raw_parameters, Mapping):
        raise ValidationError(
            "effect_parameters must be a JSON object.", field="intervention.effect_parameters"
        )
    raw_classes = values.get("applicable_covenant_classes")
    if raw_classes is not None and isinstance(raw_classes, Mapping):
        raise ValidationError(
            "applicable_covenant_classes must be an array of class names.",
            field="intervention.applicable_covenant_classes",
        )
    return CatalogueEntry(
        id=cast(str | None, values.get("id", values.get("code"))),
        code=cast(str | None, values.get("code")),
        database_id=_optional_uuid(values.get("database_id", values.get("intervention_id"))),
        role_tag=cast(RoleTag | str | None, values.get("role_tag")),
        text=cast(str | None, values.get("text")),
        effect_model=cast(EffectModelType | str | None, values.get("effect_model")),
        effect_parameters=cast(Mapping[str, object] | None, raw_parameters),
        applicable_covenant_classes=cast(
            Sequence[str] | None,
            raw_classes,
        ),
        assumptions=cast(Sequence[str] | None, values.get("assumptions")),
        requires_approval=cast(bool, values.get("requires_approval", False)),
        is_active=cast(bool, values.get("is_active", True)),
        retired_at=cast(datetime | None, raw_retired_at),
        version=cast(int, values.get("version", 0)),
    )


def _code(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("code must be a non-blank intervention id.", field="intervention.id")
    return value.strip()


def _covenant_class(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            "covenant_class must be a non-blank class name.", field="covenant_class"
        )
    return value.strip().lower()


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("expected_version must be a non-negative integer.", field="version")
    return value


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} must be a UUID.", field=field)


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, "intervention.database_id")


def _request_uuid(value: UUID | str) -> UUID:
    return _uuid(value, "request_id")


def _principal(value: object) -> Principal:
    if not isinstance(value, Principal) or value.kind is not PrincipalKind.USER:
        raise ValidationError("An authenticated principal is required.", field="principal")
    return value


def _retired_at(value: object, *, fallback: datetime) -> datetime | None:
    if value is None:
        return fallback
    return _timestamp(value)


def _timestamp(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValidationError("retired_at must be an ISO timestamp.") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("retired_at must be timezone-aware.")
    return value.astimezone(UTC)


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(
            f"Catalogue request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
        )
    if not value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(
            "Catalogue request_id must be non-blank and contain no control characters."
        )
    return value.strip()


__all__ = [
    "CATALOGUE_APPROVE_PERMISSION",
    "CATALOGUE_OPERATION",
    "CATALOGUE_PROPOSE_PERMISSION",
    "CatalogueService",
    "CatalogueWrite",
]
