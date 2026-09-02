"""Capture and apply human risk-view overrides.

An override is a new, append-only fact.  The service never edits a forecast,
trace row, or any other calculated record.  It snapshots the view that was
shown, stores the user's replacement value and reason, and derives the
current display by applying the latest replacement over the original view.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Protocol
from unicodedata import normalize
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.audit.trace_reader import ExplainStage, validate_subject_type
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, NotFound, ValidationError
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.workflow import OverrideRecord
from covenant_radar.db.repositories.override import (
    OverrideRepository,
    OverrideSubjectMetadata,
)
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize
from covenant_radar.services.explain import explain

_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_TEXT_MAX_LENGTH: Final[int] = 2000
_ACTION_MAX_LENGTH: Final[int] = 50
_VERSION_MAX_LENGTH: Final[int] = 50
_MAX_JSON_DEPTH: Final[int] = 20
_OVERRIDE_RECORDED_EVENT: Final[str] = AuditEventType.OVERRIDE_RECORDED.value


class AuditWriter(Protocol):
    """The append-only audit boundary required by override writes."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one audit event in the caller's transaction."""


@dataclass(frozen=True, slots=True)
class OverrideSubject:
    """A validated, why-panel-compatible override subject."""

    subject_type: str
    subject_id: UUID

    def __post_init__(self) -> None:
        try:
            subject_type = validate_subject_type(self.subject_type)
        except ValueError as error:
            raise ValidationError(str(error), field="subject") from error
        if not isinstance(self.subject_id, UUID):
            raise ValidationError("The override subject id must be a UUID.", field="subject")
        object.__setattr__(self, "subject_type", subject_type)


@dataclass(frozen=True, slots=True)
class RevisedRiskView:
    """Original and currently displayed states for one override subject."""

    subject: OverrideSubject
    original: Mapping[str, object]
    current: Mapping[str, object]
    overrides: tuple[OverrideRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.subject, OverrideSubject):
            raise TypeError("RevisedRiskView.subject must be an OverrideSubject.")
        if not isinstance(self.original, Mapping) or not isinstance(self.current, Mapping):
            raise TypeError("RevisedRiskView states must be mappings.")
        if not isinstance(self.overrides, tuple):
            raise TypeError("RevisedRiskView.overrides must be a tuple.")
        if any(not isinstance(item, OverrideRecord) for item in self.overrides):
            raise TypeError("RevisedRiskView.overrides must contain OverrideRecord values.")

    @property
    def latest_override(self) -> OverrideRecord | None:
        """Return the latest retained override, if one exists."""

        return self.overrides[-1] if self.overrides else None

    @property
    def view(self) -> Mapping[str, object]:
        """Compatibility-facing name for the current displayed state."""

        return self.current

    def as_dict(self) -> dict[str, object]:
        """Return both states and the retained sequence as JSON-safe data."""

        return {
            "subject": {
                "type": self.subject.subject_type,
                "id": str(self.subject.subject_id),
            },
            "original": _portable_copy(self.original),
            "current": _portable_copy(self.current),
            "overrides": [
                {
                    "id": str(item.id),
                    "stage": item.stage,
                    "shown": _portable_copy(item.shown),
                    "user_action": item.user_action,
                    "user_value": _portable_copy(item.user_value),
                    "reason": item.reason,
                    "prompt_version": item.prompt_version,
                    "model_version": item.model_version,
                    "threshold_snapshot_id": (
                        str(item.threshold_snapshot_id)
                        if item.threshold_snapshot_id is not None
                        else None
                    ),
                    "actor_id": str(item.actor_id),
                    "recorded_at": item.created_at.astimezone(UTC).isoformat(),
                }
                for item in self.overrides
            ],
        }


class OverrideService:
    """Authorize, persist, audit and read risk-view overrides."""

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        repository: OverrideRepository | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("OverrideService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("OverrideService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("OverrideService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("OverrideService scope_resolver must be callable.")
        if repository is not None and not isinstance(repository, OverrideRepository):
            raise TypeError("OverrideService repository must be an OverrideRepository.")
        if repository is not None and repository.session is not session:
            raise ValueError("OverrideService repository and session must be identical.")

        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )
        self.repository = repository or OverrideRepository(session)

    def record_override(
        self,
        principal: Principal,
        subject: OverrideSubject | tuple[str, UUID] | Mapping[str, object] | str | None = None,
        stage: int | str | None = None,
        user_action: str | None = None,
        user_value: Mapping[str, object] | None = None,
        reason: str | None = None,
        *,
        subject_type: str | None = None,
        subject_id: UUID | str | None = None,
        shown: Mapping[str, object] | None = None,
        prompt_version: str | None = None,
        model_version: str | None = None,
        threshold_snapshot_id: UUID | str | None = None,
        scope: Scope | None = None,
        request_id: str | None = None,
    ) -> OverrideRecord:
        """Append one override without mutating the underlying risk record.

        ``shown`` is optional for trusted internal callers.  When omitted,
        it is rebuilt from the persisted trace and the existing override
        sequence.  The web route never accepts a client-supplied ``shown``
        value, preventing a caller from rewriting the evidence of what the
        application displayed.
        """

        self._require_write_principal(principal)
        resolved_scope = self._validated_scope(principal, scope)
        resolved_subject = _coerce_subject(
            subject,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        normalized_stage = _stage(stage)
        normalized_action = _required_text(user_action, "user_action", maximum=_ACTION_MAX_LENGTH)
        normalized_reason = _required_reason(reason)
        normalized_user_value = _optional_mapping(user_value, "user_value")
        effective_request_id = _request_id(request_id or self.request_id)

        if not self.repository.subject_visible(
            resolved_subject.subject_type,
            resolved_subject.subject_id,
            scope=resolved_scope,
        ):
            raise NotFound(
                f"{resolved_subject.subject_type} {resolved_subject.subject_id} "
                "was not found within the current scope."
            )

        current_view, stage_view, metadata = self._current_context(
            resolved_subject,
            normalized_stage,
            resolved_scope,
        )
        effective_shown = (
            _optional_mapping(shown, "shown")
            if shown is not None
            else _portable_copy(current_view.current)
        )
        if effective_shown is None:
            effective_shown = {}

        prompt = _optional_version(prompt_version, "prompt_version")
        model = _optional_version(model_version, "model_version")
        if prompt is None and stage_view.decider == "model":
            prompt = _optional_version(stage_view.rule_or_prompt_version, "prompt_version")
        if model is None:
            model = metadata.model_version
            if model is None:
                model = _version_from_mapping(stage_view.outputs, "model_version")
        snapshot_id = _optional_uuid(threshold_snapshot_id, "threshold_snapshot_id")
        if snapshot_id is None:
            snapshot_id = metadata.threshold_snapshot_id

        now = self._now()
        record = OverrideRecord(
            subject_type=resolved_subject.subject_type,
            subject_id=resolved_subject.subject_id,
            stage=str(normalized_stage),
            shown=effective_shown,
            user_action=normalized_action,
            user_value=normalized_user_value,
            reason=normalized_reason,
            prompt_version=prompt,
            model_version=model,
            threshold_snapshot_id=snapshot_id,
            actor_id=principal.id,
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=effective_request_id,
        )

        # The nested transaction makes the row and its audit event atomic
        # without stealing commit ownership from the caller's unit of work.
        with self.session.begin_nested():
            self.repository.add(record)
            self.audit.record(
                _OVERRIDE_RECORDED_EVENT,
                (resolved_subject.subject_type, resolved_subject.subject_id),
                {
                    "override_record_id": str(record.id),
                    "subject_type": resolved_subject.subject_type,
                    "subject_id": str(resolved_subject.subject_id),
                    "stage": normalized_stage,
                    "user_action": normalized_action,
                    "prompt_version": prompt,
                    "model_version": model,
                    "threshold_snapshot_id": (
                        str(snapshot_id) if snapshot_id is not None else None
                    ),
                    # Free text and replacement payloads remain in the local,
                    # scoped override row.  The audit chain records their
                    # existence and stable row reference without duplicating
                    # content that must not appear in logs or outbound data.
                    "shown_recorded": True,
                    "user_value_recorded": normalized_user_value is not None,
                    "reason_recorded": True,
                },
                actor=principal.id,
                request_id=effective_request_id,
            )
        return record

    # All aliases share one implementation, so each call has identical
    # authorization, validation, audit and transaction semantics.
    override = record_override
    capture = record_override
    record = record_override

    def current_view(
        self,
        principal: Principal,
        subject: OverrideSubject | tuple[str, UUID] | Mapping[str, object] | str,
        *,
        scope: Scope | None = None,
    ) -> RevisedRiskView:
        """Return the server-derived original and latest displayed states."""

        self._require_read_principal(principal)
        resolved_scope = self._validated_scope(principal, scope)
        resolved_subject = _coerce_subject(subject)
        if not self.repository.subject_visible(
            resolved_subject.subject_type,
            resolved_subject.subject_id,
            scope=resolved_scope,
        ):
            raise NotFound(
                f"{resolved_subject.subject_type} {resolved_subject.subject_id} "
                "was not found within the current scope."
            )
        return self._view_for_subject(resolved_subject, resolved_scope)

    revised_view = current_view
    view_revision = current_view
    get_view = current_view

    def list_overrides(
        self,
        principal: Principal,
        subject: OverrideSubject | tuple[str, UUID] | Mapping[str, object] | str,
        *,
        scope: Scope | None = None,
    ) -> tuple[OverrideRecord, ...]:
        """Return the complete retained sequence for one visible subject."""

        self._require_read_principal(principal)
        resolved_scope = self._validated_scope(principal, scope)
        resolved_subject = _coerce_subject(subject)
        if not self.repository.subject_visible(
            resolved_subject.subject_type,
            resolved_subject.subject_id,
            scope=resolved_scope,
        ):
            raise NotFound(
                f"{resolved_subject.subject_type} {resolved_subject.subject_id} "
                "was not found within the current scope."
            )
        return self.repository.for_subject(
            resolved_subject.subject_type,
            resolved_subject.subject_id,
            scope=resolved_scope,
        )

    overrides_for = list_overrides
    get_overrides = list_overrides

    def redirect_path(
        self,
        principal: Principal,
        subject: OverrideSubject | tuple[str, UUID] | Mapping[str, object] | str,
        *,
        scope: Scope | None = None,
    ) -> str:
        """Return the only in-application destination for an override subject."""

        self._require_read_principal(principal)
        resolved_scope = self._validated_scope(principal, scope)
        resolved_subject = _coerce_subject(subject)
        if not self.repository.subject_visible(
            resolved_subject.subject_type,
            resolved_subject.subject_id,
            scope=resolved_scope,
        ):
            raise NotFound(
                f"{resolved_subject.subject_type} {resolved_subject.subject_id} "
                "was not found within the current scope."
            )
        if resolved_subject.subject_type == "borrower":
            reference = self.session.scalar(
                select(Borrower.reference).where(Borrower.id == resolved_subject.subject_id)
            )
            if isinstance(reference, str) and reference:
                return f"/borrowers/{reference}"
        return f"/why/{resolved_subject.subject_type}/{resolved_subject.subject_id}"

    destination = redirect_path

    def _current_context(
        self,
        subject: OverrideSubject,
        stage: int,
        scope: Scope,
    ) -> tuple[RevisedRiskView, ExplainStage, OverrideSubjectMetadata]:
        view = self._view_for_subject(subject, scope)
        stage_view = next(item for item in self._stages(subject) if item.stage == stage)
        metadata = self.repository.subject_metadata(
            subject.subject_type,
            subject.subject_id,
            scope=scope,
        )
        return view, stage_view, metadata

    def _view_for_subject(self, subject: OverrideSubject, scope: Scope) -> RevisedRiskView:
        stages = self._stages(subject)
        base_stage = next((item for item in reversed(stages) if not item.not_run), stages[0])
        base = _stage_snapshot(base_stage)
        # A subject with no trace history still has an explicit not-run state.
        # Overrides may target any defined stage, so the sequence itself
        # remains reconstructable in that degraded case.
        rows = self.repository.for_subject(subject.subject_type, subject.subject_id, scope=scope)
        current_value = _portable_copy(base)
        if not isinstance(current_value, dict):
            raise TypeError("The server-derived risk view must be a JSON object.")
        current = current_value
        for row in rows:
            if row.user_value is not None:
                current = _merge_mappings(current, row.user_value)
        return RevisedRiskView(
            subject=subject,
            original=MappingProxyType(base),
            current=MappingProxyType(current),
            overrides=rows,
        )

    def _stages(self, subject: OverrideSubject) -> tuple[ExplainStage, ...]:
        return explain(self.session, (subject.subject_type, subject.subject_id))

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
        authorize(principal, Permission.OVERRIDE_RISK_VIEW)
        if principal.kind is not PrincipalKind.USER:
            raise AuthorizationError("Risk-view overrides require an authenticated user principal.")

    @staticmethod
    def _require_read_principal(principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        if not (
            principal.has(Permission.VIEW_BORROWER) or principal.has(Permission.OVERRIDE_RISK_VIEW)
        ):
            authorize(principal, Permission.VIEW_BORROWER)

    def _now(self) -> datetime:
        now = self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Override clock must return an aware datetime.")
        return now.astimezone(UTC)


def _coerce_subject(
    value: OverrideSubject | tuple[str, UUID] | Mapping[str, object] | str | None,
    *,
    subject_type: str | None = None,
    subject_id: UUID | str | None = None,
) -> OverrideSubject:
    if value is None and subject_type is not None:
        value = {"subject_type": subject_type, "subject_id": subject_id}
    if isinstance(value, OverrideSubject):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return OverrideSubject(value[0], _parse_uuid(value[1], "subject_id"))
    if isinstance(value, Mapping):
        raw_type = value.get("subject_type", value.get("type"))
        raw_id = value.get("subject_id", value.get("id"))
        if not isinstance(raw_type, str):
            raise ValidationError("subject_type is required.", field="subject")
        return OverrideSubject(raw_type, _parse_uuid(raw_id, "subject_id"))
    if isinstance(value, str):
        for separator in (":", "/"):
            if separator in value:
                raw_type, raw_id = value.split(separator, 1)
                return OverrideSubject(raw_type, _parse_uuid(raw_id, "subject_id"))
        raise ValidationError("The override subject must contain a type and UUID.", field="subject")
    raise ValidationError("The override subject is required.", field="subject")


def _stage(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise ValidationError("Stage must be between 1 and 7.", field="stage")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        candidate = value.strip().lower().replace("_", "-")
        if candidate.startswith("stage-"):
            candidate = candidate.removeprefix("stage-")
        try:
            number = int(candidate)
        except ValueError as error:
            raise ValidationError("Stage must be between 1 and 7.", field="stage") from error
    else:
        raise ValidationError("Stage must be between 1 and 7.", field="stage")
    if not 1 <= number <= 7:
        raise ValidationError("Stage must be between 1 and 7.", field="stage")
    return number


def _required_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("A reason is required.", field="reason")
    return _required_text(value, "reason", maximum=_TEXT_MAX_LENGTH)


def _required_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required.", field=field)
    normalized = normalize("NFC", value.strip())
    if len(normalized) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValidationError(f"{field} contains an invalid control character.", field=field)
    return normalized


def _optional_mapping(value: object, field: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be a JSON object.", field=field)
    result = _portable_copy(value)
    if not isinstance(result, dict):
        raise ValidationError(f"{field} must be a JSON object.", field=field)
    return result


def _optional_version(value: object, field: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _required_text(value, field, maximum=_VERSION_MAX_LENGTH)


def _optional_uuid(value: object, field: str) -> UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_uuid(value, field)


def _parse_uuid(value: object, field: str) -> UUID:
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
            f"Override request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
        )
    return value.strip()


def _stage_snapshot(stage: ExplainStage) -> dict[str, object]:
    return {
        "stage": stage.stage,
        "name": stage.name,
        "not_run": stage.not_run,
        "decider": stage.decider,
        "inputs": _portable_copy(stage.inputs),
        "outputs": _portable_copy(stage.outputs),
        "rule_or_prompt_version": stage.rule_or_prompt_version,
        "thresholds_compared": _portable_copy(stage.thresholds_compared),
        "confidence": _portable_copy(stage.confidence),
        "sources": _portable_copy(stage.sources),
    }


def _merge_mappings(base: Mapping[str, object], patch: Mapping[str, object]) -> dict[str, object]:
    merged = _portable_copy(base)
    normalized_patch = _portable_copy(patch)
    if not isinstance(merged, dict) or not isinstance(normalized_patch, dict):
        raise TypeError("Risk-view states must be JSON objects.")
    merged.update(normalized_patch)
    return merged


def _version_from_mapping(value: Mapping[str, object], key: str) -> str | None:
    candidate = value.get(key)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return None


def _portable_copy(value: object, *, _depth: int = 0) -> object:
    """Copy a JSON-shaped value while converting lossless scalar types."""

    if _depth > _MAX_JSON_DEPTH:
        raise ValidationError("Override JSON is nested too deeply.", field="user_value")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValidationError(
                    "Override JSON object keys must be non-empty text.",
                    field="user_value",
                )
            result[key] = _portable_copy(item, _depth=_depth + 1)
        return result
    if isinstance(value, list | tuple):
        return [_portable_copy(item, _depth=_depth + 1) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValidationError("Override JSON contains a non-finite number.", field="user_value")
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError(
                "Override JSON datetime must be timezone-aware.",
                field="user_value",
            )
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("Override JSON contains a non-finite number.", field="user_value")
        return value
    raise ValidationError(
        f"Override JSON contains unsupported value type {type(value).__name__}.",
        field="user_value",
    )


__all__ = [
    "AuditWriter",
    "OverrideService",
    "OverrideSubject",
    "RevisedRiskView",
]
