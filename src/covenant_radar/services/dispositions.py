"""Scoped recording of human warning dispositions.

Dispositions are deliberately append-only.  A later answer from the desk is
another row, rather than an update to the earlier answer, so the product can
reconstruct the sequence of decisions that followed a warning.  This module
also owns the small reason-code taxonomy used by the web control; free-form
notes remain in the disposition record and are never copied into a labelled
dataset.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol
from unicodedata import normalize
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, NotFound, ValidationError
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, TriageEntry
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Disposition
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize

_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_SUBJECT_TYPE_MAX_LENGTH: Final[int] = 50
_REASON_CODE_MAX_LENGTH: Final[int] = 50
_NOTE_MAX_LENGTH: Final[int] = 2_000


class DispositionOutcome(StrEnum):
    """The three outcomes a desk may record for a warning."""

    ACTED = "acted"
    MONITORING = "monitoring"
    DISMISSED = "dismissed"


class DispositionReasonCode(StrEnum):
    """Stable, non-personal reason codes used for desk feedback."""

    ACTION_TAKEN = "action_taken"
    BORROWER_CONTACTED = "borrower_contacted"
    MITIGATION_STARTED = "mitigation_started"
    ESCALATED = "escalated"
    MONITORING_ONLY = "monitoring_only"
    AWAITING_INFORMATION = "awaiting_information"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    COVENANT_WITHIN_TOLERANCE = "covenant_within_tolerance"
    FALSE_POSITIVE = "false_positive"
    DUPLICATE_WARNING = "duplicate_warning"
    DATA_QUALITY_ISSUE = "data_quality_issue"
    NOT_ACTIONABLE = "not_actionable"
    ALREADY_RESOLVED = "already_resolved"
    ALREADY_KNOWN = "already_known"
    NOT_MATERIAL = "not_material"
    BORROWER_ENGAGED = "borrower_engaged"


DISPOSITION_OUTCOMES: Final[tuple[str, ...]] = tuple(item.value for item in DispositionOutcome)
DISPOSITION_REASON_CODES: Final[tuple[str, ...]] = tuple(
    item.value for item in DispositionReasonCode
)
DISPOSITION_REASON_TAXONOMY: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        DispositionOutcome.ACTED.value: (
            DispositionReasonCode.ACTION_TAKEN.value,
            DispositionReasonCode.BORROWER_CONTACTED.value,
            DispositionReasonCode.MITIGATION_STARTED.value,
            DispositionReasonCode.ESCALATED.value,
        ),
        DispositionOutcome.MONITORING.value: (
            DispositionReasonCode.MONITORING_ONLY.value,
            DispositionReasonCode.AWAITING_INFORMATION.value,
            DispositionReasonCode.INSUFFICIENT_EVIDENCE.value,
            DispositionReasonCode.COVENANT_WITHIN_TOLERANCE.value,
            DispositionReasonCode.BORROWER_ENGAGED.value,
        ),
        DispositionOutcome.DISMISSED.value: (
            DispositionReasonCode.FALSE_POSITIVE.value,
            DispositionReasonCode.DUPLICATE_WARNING.value,
            DispositionReasonCode.DATA_QUALITY_ISSUE.value,
            DispositionReasonCode.NOT_ACTIONABLE.value,
            DispositionReasonCode.ALREADY_RESOLVED.value,
            DispositionReasonCode.ALREADY_KNOWN.value,
            DispositionReasonCode.NOT_MATERIAL.value,
        ),
    }
)

_SUPPORTED_SUBJECT_TYPES: Final[frozenset[str]] = frozenset(
    {"forecast", "triage_entry", "borrower"}
)
_SUBJECT_TYPE_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {"warning": "forecast", "triage": "triage_entry"}
)


class AuditWriter(Protocol):
    """The narrow append-only audit boundary used by disposition writes."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an event in the caller's transaction."""
        ...


@dataclass(frozen=True, slots=True)
class DispositionSubject:
    """A validated polymorphic subject for one disposition."""

    subject_type: str
    subject_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.subject_type, str):
            raise ValidationError("subject_type is required.", field="subject")
        normalized = _text(self.subject_type, "subject_type", _SUBJECT_TYPE_MAX_LENGTH).lower()
        normalized = _SUBJECT_TYPE_ALIASES.get(normalized, normalized)
        if normalized not in _SUPPORTED_SUBJECT_TYPES:
            raise ValidationError(
                f"Unsupported disposition subject type {normalized!r}.", field="subject"
            )
        if not isinstance(self.subject_id, UUID):
            raise ValidationError("subject_id must be a UUID.", field="subject")
        object.__setattr__(self, "subject_type", normalized)


class DispositionService:
    """Authorize, scope-check, append and audit warning dispositions."""

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("DispositionService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("DispositionService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("DispositionService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("DispositionService scope_resolver must be callable.")

        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )

    def record_disposition(
        self,
        principal: Principal,
        subject: DispositionSubject | tuple[str, UUID] | Mapping[str, object] | str | None = None,
        outcome: DispositionOutcome | str | None = None,
        reason_code: DispositionReasonCode | str | None = None,
        note: str | None = None,
        *,
        subject_type: str | None = None,
        subject_id: UUID | str | None = None,
        scope: Scope | None = None,
        request_id: str | None = None,
    ) -> Disposition:
        """Append one scoped disposition and retain all prior dispositions."""

        self._require_write_principal(principal)
        resolved_scope = self._validated_scope(principal, scope)
        resolved_subject = _coerce_subject(
            subject,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        normalized_outcome = _outcome(outcome)
        normalized_reason_code = _reason_code(reason_code, normalized_outcome)
        normalized_note = _optional_text(note, "note", _NOTE_MAX_LENGTH)
        effective_request_id = _request_id(request_id or self.request_id)

        if not self.subject_visible(resolved_subject, scope=resolved_scope):
            raise NotFound(
                f"{resolved_subject.subject_type} {resolved_subject.subject_id} "
                "was not found within the current scope."
            )

        now = self._now()
        record = Disposition(
            subject_type=resolved_subject.subject_type,
            subject_id=resolved_subject.subject_id,
            outcome=normalized_outcome,
            reason_code=normalized_reason_code,
            note=normalized_note,
            actor_id=principal.id,
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=effective_request_id,
        )

        # The nested transaction keeps the row and its audit event atomic
        # without taking commit ownership from the caller's unit of work.
        with self.session.begin_nested():
            self.session.add(record)
            self.session.flush()
            self.audit.record(
                AuditEventType.CASE_LIFECYCLE_CHANGED.value,
                (resolved_subject.subject_type, resolved_subject.subject_id),
                {
                    "workflow_event": "disposition_recorded",
                    "disposition_id": str(record.id),
                    "subject_type": resolved_subject.subject_type,
                    "subject_id": str(resolved_subject.subject_id),
                    "outcome": normalized_outcome,
                    "reason_code": normalized_reason_code,
                    "note_recorded": normalized_note is not None,
                },
                actor=principal.id,
                request_id=effective_request_id,
            )
        return record

    record = record_disposition
    create = record_disposition
    disposition = record_disposition

    def list_dispositions(
        self,
        principal: Principal,
        subject: DispositionSubject | tuple[str, UUID] | Mapping[str, object] | str,
        *,
        scope: Scope | None = None,
    ) -> tuple[Disposition, ...]:
        """Return the complete retained sequence for one visible subject."""

        self._require_read_principal(principal)
        resolved_scope = self._validated_scope(principal, scope)
        resolved_subject = _coerce_subject(subject)
        if not self.subject_visible(resolved_subject, scope=resolved_scope):
            raise NotFound(
                f"{resolved_subject.subject_type} {resolved_subject.subject_id} "
                "was not found within the current scope."
            )
        return tuple(
            self.session.execute(
                select(Disposition)
                .where(
                    Disposition.subject_type == resolved_subject.subject_type,
                    Disposition.subject_id == resolved_subject.subject_id,
                )
                .order_by(Disposition.created_at.asc(), Disposition.id.asc())
            )
            .scalars()
            .all()
        )

    history = list_dispositions
    list_for_subject = list_dispositions
    history_for = list_dispositions
    get_dispositions = list_dispositions

    def subject_visible(
        self,
        subject: DispositionSubject | tuple[str, UUID] | Mapping[str, object] | str,
        *,
        scope: Scope,
    ) -> bool:
        """Return whether a disposition subject exists inside ``scope``."""

        if not isinstance(scope, Scope):
            raise TypeError("subject_visible requires a portfolio Scope.")
        resolved_subject = _coerce_subject(subject)
        statement = _subject_visibility_statement(resolved_subject, scope)
        return self.session.scalar(statement) is not None

    is_subject_visible = subject_visible

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        resolved = self.scope_resolver(principal) if scope is None else scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied scope does not belong to the authenticated principal."
            )
        return resolved

    @staticmethod
    def _require_write_principal(principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.RECORD_DISPOSITION)
        if principal.kind is not PrincipalKind.USER:
            raise AuthorizationError("Dispositions require an authenticated user principal.")

    @staticmethod
    def _require_read_principal(principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        if not any(
            principal.has(permission)
            for permission in (
                Permission.RECORD_DISPOSITION,
                Permission.VIEW_BORROWER,
                Permission.VIEW_AUDIT,
            )
        ):
            authorize(principal, Permission.VIEW_BORROWER)

    def _now(self) -> datetime:
        now = self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Disposition clock must return an aware datetime.")
        return now.astimezone(UTC)


def _subject_visibility_statement(subject: DispositionSubject, scope: Scope) -> Select[tuple[UUID]]:
    if subject.subject_type == "forecast":
        return (
            select(Forecast.id)
            .join(CovenantVersion, CovenantVersion.id == Forecast.covenant_version_id)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .join(Facility, Facility.id == Covenant.facility_id)
            .join(Borrower, Borrower.id == Facility.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(Forecast.id == subject.subject_id, scope.predicate(Portfolio.path))
            .limit(1)
        )
    if subject.subject_type == "triage_entry":
        return (
            select(TriageEntry.id)
            .join(Borrower, Borrower.id == TriageEntry.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(TriageEntry.id == subject.subject_id, scope.predicate(Portfolio.path))
            .limit(1)
        )
    return (
        select(Borrower.id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(Borrower.id == subject.subject_id, scope.predicate(Portfolio.path))
        .limit(1)
    )


def _coerce_subject(
    value: DispositionSubject | tuple[str, UUID] | Mapping[str, object] | str | None,
    *,
    subject_type: str | None = None,
    subject_id: UUID | str | None = None,
) -> DispositionSubject:
    if value is None and subject_type is not None:
        value = {"subject_type": subject_type, "subject_id": subject_id}
    if isinstance(value, DispositionSubject):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return DispositionSubject(value[0], _uuid(value[1], "subject_id"))
    if isinstance(value, Mapping):
        raw_type = value.get("subject_type", value.get("type"))
        raw_id = value.get("subject_id", value.get("id"))
        if not isinstance(raw_type, str):
            raise ValidationError("subject_type is required.", field="subject")
        return DispositionSubject(raw_type, _uuid(raw_id, "subject_id"))
    if isinstance(value, str):
        for separator in (":", "/"):
            if separator in value:
                raw_type, raw_id = value.split(separator, 1)
                return DispositionSubject(raw_type, _uuid(raw_id, "subject_id"))
        raise ValidationError(
            "The disposition subject must contain a type and UUID.", field="subject"
        )
    raise ValidationError("The disposition subject is required.", field="subject")


def _outcome(value: object) -> str:
    if isinstance(value, DispositionOutcome):
        return value.value
    if not isinstance(value, str):
        raise ValidationError("Outcome is required.", field="outcome")
    normalized = value.strip().lower()
    if normalized not in DISPOSITION_OUTCOMES:
        raise ValidationError(f"Unknown disposition outcome {normalized!r}.", field="outcome")
    return normalized


def _reason_code(value: object, outcome: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if outcome == DispositionOutcome.DISMISSED.value:
            raise ValidationError(
                "A reason code is required when dismissing a warning.", field="reason_code"
            )
        return None
    if isinstance(value, DispositionReasonCode):
        normalized = value.value
    elif isinstance(value, str):
        normalized = value.strip().lower()
    else:
        raise ValidationError("Reason code must be text.", field="reason_code")
    if normalized not in DISPOSITION_REASON_CODES:
        raise ValidationError(
            "Unknown disposition reason code; choose one from the supplied taxonomy.",
            field="reason_code",
        )
    if normalized not in DISPOSITION_REASON_TAXONOMY[outcome]:
        raise ValidationError(
            f"Reason code {normalized!r} is not valid for {outcome!r}.",
            field="reason_code",
        )
    return normalized


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required.", field=field)
    normalized = normalize("NFC", value.strip())
    if len(normalized) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValidationError(f"{field} contains an invalid control character.", field=field)
    return normalized


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _text(value, field, maximum)


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value.strip())
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} must be a UUID.", field=field)


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(
            f"Disposition request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
        )
    return value.strip()


__all__ = [
    "AuditWriter",
    "DISPOSITION_OUTCOMES",
    "DISPOSITION_REASON_CODES",
    "DISPOSITION_REASON_TAXONOMY",
    "DispositionOutcome",
    "DispositionReasonCode",
    "DispositionService",
    "DispositionSubject",
]
