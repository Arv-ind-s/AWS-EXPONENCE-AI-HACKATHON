"""Model registration, its approval path, and the auditor-visible model
register (`plan.md §5.9`'s `model_registration`, `T-107`).

Coordinates the SQLAlchemy-backed `ModelRegistryRepository` in this module,
the shared maker-checker workflow (`security/maker_checker.py`,
`services/approvals.py`) and the append-only audit port in one
caller-owned transaction — the same shape `RegistryService`
(`services/registry.py`) already established for covenant governance, down
to reusing its generic `SqlAlchemyMakerCheckerRepository` adapter: the
`maker_checker_request` table is deliberately subject-agnostic, and a
second copy of that adapter here would only be able to drift from it.

Only two permissions reach a write path. `Permission.MANAGE_JOBS` — the
same operational-configuration permission that already gates connectors
and scheduled jobs (`spec §16.1`) — registers or updates a component.
`Permission.APPROVE_MODEL_PROMOTION`, held only by Risk Head, decides the
request. No permission in the closed vocabulary grants a third path, so
`list_registrations` (gated on `Permission.VIEW_AUDIT`) is the only method
an Auditor's grant reaches: the register is readable, never writable, by
the role that needs to inspect it without being able to change what it
describes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.ai.registry import (
    APPROVED,
    REGISTERED,
    ModelRegistrationRecord,
    ModelRegistryRepository,
)
from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.operations import EvaluationRun
from covenant_radar.db.models.operations import ModelRegistration as ModelRegistrationRow
from covenant_radar.db.session import is_database_session
from covenant_radar.security.maker_checker import (
    ApplicationCallbackRegistry,
    MakerCheckerRequest,
    MakerCheckerSettings,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize
from covenant_radar.services.approvals import ApprovalService
from covenant_radar.services.registry import SqlAlchemyMakerCheckerRepository

_COMPONENT_MAX_LENGTH = 100
_PROVIDER_MAX_LENGTH = 50
_MODEL_ID_MAX_LENGTH = 100
_PROMPT_VERSION_MAX_LENGTH = 50
_PURPOSE_MAX_LENGTH = 200

PROPOSE_MODEL_REGISTRATION_PERMISSION = Permission.MANAGE_JOBS
APPROVE_MODEL_REGISTRATION_PERMISSION = Permission.APPROVE_MODEL_PROMOTION
VIEW_MODEL_REGISTRY_PERMISSION = Permission.VIEW_AUDIT

MODEL_REGISTRATION_OPERATION = "model_registration"


class AuditWriter(Protocol):
    """The append-only audit port from contract `C-60`."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's current transaction."""


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    """What `register` returns: the stored registration and its approval
    request, when the second-actor control produced one."""

    registration: ModelRegistrationRecord
    approval_request: MakerCheckerRequest | None
    reapproval_required: bool


class ModelGovernanceService:
    """Register, approve and read the model register in one transaction.

    `session` must belong to the caller's current unit of work, the same
    requirement `RegistryService` documents: a repository read and its
    audit event must never drift into different transactions.
    """

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        approval_service: ApprovalService | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("ModelGovernanceService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("ModelGovernanceService requires an append-only audit writer.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 40:
            raise ValueError("Model governance request_id must be between 1 and 40 characters.")
        self.registrations = SqlAlchemyModelRegistryRepository(session)
        self.approvals = approval_service or _default_approval_service(
            session, audit=audit, clock=self.clock, request_id=self.request_id
        )
        self._wire_approval_callback()

    # ---- use cases ---------------------------------------------------

    def register(
        self,
        principal: Principal,
        *,
        component: str,
        provider: str,
        model_id: str,
        prompt_version: str | None = None,
        purpose: str | None = None,
        owner_id: UUID | None = None,
        evaluation_run_id: UUID | None = None,
    ) -> RegisteredModel:
        """Register a new component, or re-register an existing one.

        Re-registering a component whose provider, model identifier or
        prompt version has changed always resets it to `registered` and
        clears any prior approval — the approved thing has changed, so the
        approval no longer describes what is running. An identical
        re-registration (nothing about provider/model/prompt differs) is a
        no-op that returns the existing row untouched, so a redeploy that
        registers the same component again does not manufacture a fresh
        approval request every time.
        """
        self._require(principal, PROPOSE_MODEL_REGISTRATION_PERMISSION)
        actor_id = self._registering_user_id(principal)
        name = _required_text(component, "model_registration.component", _COMPONENT_MAX_LENGTH)
        provider = _required_text(provider, "model_registration.provider", _PROVIDER_MAX_LENGTH)
        model_id = _required_text(model_id, "model_registration.model_id", _MODEL_ID_MAX_LENGTH)
        prompt_version = _optional_text(
            prompt_version, "model_registration.prompt_version", _PROMPT_VERSION_MAX_LENGTH
        )
        purpose = _optional_text(purpose, "model_registration.purpose", _PURPOSE_MAX_LENGTH)
        self._validate_evaluation_run(evaluation_run_id)

        now = self._now()
        existing = self.registrations.get_for_update(name)
        unchanged = existing is not None and (
            existing.provider,
            existing.model_id,
            existing.prompt_version,
        ) == (provider, model_id, prompt_version)
        if unchanged:
            assert existing is not None
            return RegisteredModel(existing, approval_request=None, reapproval_required=False)

        reapproval_required = existing is not None
        record = ModelRegistrationRecord(
            id=existing.id if existing is not None else new_id(),
            component=name,
            provider=provider,
            model_id=model_id,
            prompt_version=prompt_version,
            purpose=purpose,
            owner_id=owner_id,
            evaluation_run_id=evaluation_run_id,
            approved_by_id=None,
            approved_at=None,
            state=REGISTERED,
            version=existing.version if existing is not None else 1,
        )
        if existing is None:
            stored = self.registrations.create(
                record, actor_id=actor_id, now=now, request_id=self.request_id
            )
        else:
            stored = self.registrations.update(
                record, expected_version=existing.version, actor_id=actor_id, now=now
            )

        event_type = (
            AuditEventType.MODEL_REGISTRATION_REAPPROVAL_REQUIRED
            if reapproval_required
            else AuditEventType.MODEL_REGISTRATION_REGISTERED
        )
        self._audit(
            event_type.value,
            stored,
            actor=principal,
            payload={
                "action": "reapproval_required" if reapproval_required else "registered",
                "component": stored.component,
                "provider": stored.provider,
                "model_id": stored.model_id,
                "prompt_version": stored.prompt_version,
                "previous_prompt_version": (
                    existing.prompt_version if existing is not None else None
                ),
            },
        )
        approval_request = self.approvals.submit(
            MODEL_REGISTRATION_OPERATION,
            ("model_registration", stored.id),
            {"component": stored.component},
            principal,
        )
        return RegisteredModel(
            stored, approval_request=approval_request, reapproval_required=reapproval_required
        )

    def decide_approval(
        self,
        principal: Principal,
        request_id: UUID | str,
        *,
        approved: bool,
        reason: str | None = None,
    ) -> MakerCheckerRequest:
        """Approve or reject one pending registration request."""
        self._require(principal, APPROVE_MODEL_REGISTRATION_PERMISSION)
        parsed_id = _coerce_uuid(request_id, "request_id")
        return self.approvals.decide(parsed_id, principal, approved, reason)

    def list_registrations(self, principal: Principal) -> tuple[ModelRegistrationRecord, ...]:
        """Return every registration — the register an auditor may read."""
        self._require(principal, VIEW_MODEL_REGISTRY_PERMISSION)
        return tuple(self.registrations.list_all())

    def get_registration(
        self, principal: Principal, component: str
    ) -> ModelRegistrationRecord | None:
        """Return one component's current registration, or `None`."""
        self._require(principal, VIEW_MODEL_REGISTRY_PERMISSION)
        return self.registrations.get_by_component(component)

    # ---- internal invariants ------------------------------------------

    def _wire_approval_callback(self) -> None:
        if MODEL_REGISTRATION_OPERATION in self.approvals.registry:
            existing = self.approvals.registry.get(MODEL_REGISTRATION_OPERATION)
            if existing.callback != self._apply_approval:
                raise Conflict(
                    f"Maker-checker operation {MODEL_REGISTRATION_OPERATION!r} is already "
                    "wired to another callback."
                )
            return
        self.approvals.registry.register(
            MODEL_REGISTRATION_OPERATION,
            self._apply_approval,
            propose_permission=PROPOSE_MODEL_REGISTRATION_PERMISSION,
            approve_permission=APPROVE_MODEL_REGISTRATION_PERMISSION,
        )

    def _apply_approval(self, request: MakerCheckerRequest, checker_id: UUID) -> object:
        registration_id = _payload_uuid(
            request.payload, "model_registration_id", request.subject_id
        )
        row = self.session.scalar(
            select(ModelRegistrationRow)
            .where(ModelRegistrationRow.id == registration_id)
            .with_for_update()
        )
        if row is None:
            raise NotFound(f"Model registration {registration_id} was not found.")
        if row.state != REGISTERED:
            raise Conflict(
                f"Model registration {row.component!r} is {row.state!r}; only a pending "
                "registration can be approved."
            )
        now = self._now()
        row.state = APPROVED
        row.approved_by_id = checker_id
        row.approved_at = now
        row.updated_at = now
        row.updated_by_id = checker_id
        row.version += 1
        self.session.flush()
        principal = Principal.user(checker_id, (APPROVE_MODEL_REGISTRATION_PERMISSION,))
        self._audit(
            AuditEventType.MODEL_REGISTRATION_APPROVED.value,
            _record_from_row(row),
            actor=principal,
            payload={
                "action": "approved",
                "component": row.component,
                "approved_by_id": str(checker_id),
            },
        )
        return row

    def _validate_evaluation_run(self, evaluation_run_id: UUID | None) -> None:
        if evaluation_run_id is None:
            return
        exists = self.session.scalar(
            select(EvaluationRun.id).where(EvaluationRun.id == evaluation_run_id)
        )
        if exists is None:
            raise NotFound(f"Evaluation run {evaluation_run_id} was not found.")

    def _require(self, principal: Principal, permission: Permission) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, permission)

    @staticmethod
    def _registering_user_id(principal: Principal) -> UUID:
        """`maker_checker_request.maker_id` is a required foreign key to
        `app_user`, so only a session-user principal — never an API key —
        may register a model."""
        if principal.kind is not PrincipalKind.USER:
            raise ValidationError(
                "Only an authenticated user, not an API key, may register a model.",
                field="principal",
            )
        return principal.id

    def _audit(
        self,
        event_type: str,
        record: ModelRegistrationRecord,
        *,
        actor: object,
        payload: Mapping[str, object],
    ) -> None:
        actor_id = actor if isinstance(actor, UUID) else getattr(actor, "id", None)
        if not isinstance(actor_id, UUID):
            raise TypeError("Model governance audit actor must expose a UUID id.")
        self.audit.record(
            event_type,
            ("model_registration", record.id),
            dict(payload),
            actor=actor_id,
            request_id=self.request_id,
        )

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Model governance clock must return an aware datetime.")
        return now.astimezone(UTC)


class SqlAlchemyModelRegistryRepository(ModelRegistryRepository):
    """SQLAlchemy adapter for `model_registration`.

    Satisfies the persistence-neutral `ModelRegistryRepository` Protocol
    (`get_by_component`, used by `ModelRegistryGuard`) and offers the
    richer create/update/list surface `ModelGovernanceService` needs; every
    method participates in the caller's transaction and none commits.
    """

    def __init__(self, session: Session) -> None:
        if not is_database_session(session):
            raise TypeError("SqlAlchemyModelRegistryRepository requires a SQLAlchemy Session.")
        self.session = session

    def get_by_component(self, component: str) -> ModelRegistrationRecord | None:
        row = self.session.scalar(
            select(ModelRegistrationRow).where(ModelRegistrationRow.component == component)
        )
        return _record_from_row(row) if row is not None else None

    def get_for_update(self, component: str) -> ModelRegistrationRecord | None:
        row = self.session.scalar(
            select(ModelRegistrationRow)
            .where(ModelRegistrationRow.component == component)
            .with_for_update()
        )
        return _record_from_row(row) if row is not None else None

    def list_all(self) -> Sequence[ModelRegistrationRecord]:
        rows = self.session.scalars(
            select(ModelRegistrationRow).order_by(ModelRegistrationRow.component)
        ).all()
        return tuple(_record_from_row(row) for row in rows)

    def create(
        self,
        record: ModelRegistrationRecord,
        *,
        actor_id: UUID | None,
        now: datetime,
        request_id: str,
    ) -> ModelRegistrationRecord:
        row = ModelRegistrationRow(
            id=record.id,
            component=record.component,
            provider=record.provider,
            model_id=record.model_id,
            prompt_version=record.prompt_version,
            purpose=record.purpose,
            owner_id=record.owner_id,
            evaluation_run_id=record.evaluation_run_id,
            approved_by_id=record.approved_by_id,
            approved_at=record.approved_at,
            state=record.state,
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
            version=record.version,
        )
        self.session.add(row)
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as error:
            raise Conflict(f"Component {record.component!r} is already registered.") from error
        return _record_from_row(row)

    def update(
        self,
        record: ModelRegistrationRecord,
        *,
        expected_version: int,
        actor_id: UUID | None,
        now: datetime,
    ) -> ModelRegistrationRecord:
        row = self.session.scalar(
            select(ModelRegistrationRow)
            .where(ModelRegistrationRow.id == record.id)
            .with_for_update()
        )
        if row is None:
            raise NotFound(f"Model registration {record.id} was not found.")
        if row.version != expected_version:
            raise Conflict(f"Model registration {record.component!r} has a stale version.")
        row.provider = record.provider
        row.model_id = record.model_id
        row.prompt_version = record.prompt_version
        row.purpose = record.purpose
        row.owner_id = record.owner_id
        row.evaluation_run_id = record.evaluation_run_id
        row.approved_by_id = record.approved_by_id
        row.approved_at = record.approved_at
        row.state = record.state
        row.updated_at = now
        row.updated_by_id = actor_id
        row.version += 1
        self.session.flush()
        return _record_from_row(row)


def _default_approval_service(
    session: Session,
    *,
    audit: AuditWriter,
    clock: Clock,
    request_id: str,
) -> ApprovalService:
    return ApprovalService(
        SqlAlchemyMakerCheckerRepository(session, request_id=request_id),
        audit,
        registry=ApplicationCallbackRegistry(),
        clock=clock,
        settings=MakerCheckerSettings(enabled_operations={MODEL_REGISTRATION_OPERATION: True}),
        notifier=_NullApprovalNotifier(),
        request_id=request_id,
    )


class _NullApprovalNotifier:
    """No in-app notification channel for model-registration expiry yet.

    `ApprovalService` requires a notifier so an expiry can never be raised
    silently; a component whose registration expires unapproved is exactly
    the finding `spec §N-12.a` wants an inspector to be able to see in the
    register itself (`state='registered'`, no approval), not in a
    notification inbox this component does not have a screen for yet.
    """

    def notify(self, event_type: str, payload: Mapping[str, object]) -> object:
        return None


def _record_from_row(row: ModelRegistrationRow) -> ModelRegistrationRecord:
    return ModelRegistrationRecord(
        id=row.id,
        component=row.component,
        provider=row.provider,
        model_id=row.model_id,
        prompt_version=row.prompt_version,
        purpose=row.purpose,
        owner_id=row.owner_id,
        evaluation_run_id=row.evaluation_run_id,
        approved_by_id=row.approved_by_id,
        approved_at=row.approved_at,
        state=row.state,
        version=row.version,
    )


def _payload_uuid(payload: Mapping[str, object], key: str, fallback: UUID) -> UUID:
    value = payload.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValidationError(
                f"Approval payload field {key!r} must be a UUID.", field=key
            ) from error
    return fallback


def _coerce_uuid(value: UUID | str, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} must be a UUID.", field=field)


def _required_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} is required.", field=field)
    clean = value.strip()
    if not clean or len(clean) > maximum:
        raise ValidationError(
            f"{field} must be a non-empty value of at most {maximum} characters.", field=field
        )
    return clean


def _optional_text(value: object | None, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, maximum)


__all__ = [
    "APPROVE_MODEL_REGISTRATION_PERMISSION",
    "AuditWriter",
    "MODEL_REGISTRATION_OPERATION",
    "ModelGovernanceService",
    "PROPOSE_MODEL_REGISTRATION_PERMISSION",
    "RegisteredModel",
    "SqlAlchemyModelRegistryRepository",
    "VIEW_MODEL_REGISTRY_PERMISSION",
]
