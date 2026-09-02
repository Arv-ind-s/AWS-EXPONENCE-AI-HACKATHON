"""The single application-facing audit write boundary.

Services receive an :class:`AuditRecorder` and call its ``record`` method.
They never construct ORM rows, choose a sequence, or calculate a digest.
Those responsibilities remain in the persistence adapter behind the
``AuditStore`` protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from unicodedata import normalize
from uuid import NAMESPACE_URL, UUID, uuid5

from covenant_radar.audit.chain import normalise_payload
from covenant_radar.audit.store import AuditRecord, AuditStore
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id

_EVENT_TYPE_MAX_LENGTH: Final[int] = 100
_SUBJECT_TYPE_MAX_LENGTH: Final[int] = 50
_SUBJECT_REFERENCE_MAX_LENGTH: Final[int] = 200
_ACTOR_LABEL_MAX_LENGTH: Final[int] = 200
_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_DEFAULT_REQUEST_ID: Final[str] = "system-audit"
_SUBJECT_NAMESPACE: Final[UUID] = uuid5(NAMESPACE_URL, "https://covenant-radar/audit-subject")


@dataclass(frozen=True, slots=True, init=False)
class AuditSubject:
    """A subject type and stable UUID used by the audit table."""

    subject_type: str
    subject_id: UUID

    def __init__(self, subject_type: str, subject_id: UUID | str) -> None:
        validated_type = _text(subject_type, "subject_type", _SUBJECT_TYPE_MAX_LENGTH)
        object.__setattr__(self, "subject_type", validated_type)
        object.__setattr__(self, "subject_id", _subject_uuid(validated_type, subject_id))


class AuditRecorder:
    """Validate and forward audit events to the caller's transaction."""

    def __init__(
        self,
        store: AuditStore,
        *,
        clock: Clock | None = None,
        request_id: str | None = None,
    ) -> None:
        if not isinstance(store, AuditStore):
            raise TypeError("AuditRecorder requires an AuditStore implementation.")
        self.store = store
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or _DEFAULT_REQUEST_ID)

    def record(
        self,
        event_type: str,
        subject: AuditSubject | tuple[str, UUID | str],
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str | None = None,
        occurred_at: datetime | None = None,
        threshold_snapshot_id: UUID | str | None = None,
    ) -> object:
        """Append one validated event without committing the transaction."""

        validated_subject = _subject(subject)
        validated_payload = normalise_payload(payload)
        actor_id, actor_label = _actor(actor)
        instant = _instant(occurred_at if occurred_at is not None else self.clock.now())
        resolved_request_id = _request_id(request_id or self.request_id)
        snapshot_id = _optional_uuid(threshold_snapshot_id, "threshold_snapshot_id")
        entry = AuditRecord(
            event_type=_text(event_type, "event_type", _EVENT_TYPE_MAX_LENGTH),
            subject_type=validated_subject.subject_type,
            subject_id=validated_subject.subject_id,
            payload=validated_payload,
            actor_id=actor_id,
            actor_label=actor_label,
            occurred_at=instant,
            request_id=resolved_request_id,
            threshold_snapshot_id=snapshot_id,
        )
        return self.store.append(entry)


def record(
    event_type: str,
    subject: AuditSubject | tuple[str, UUID | str],
    payload: Mapping[str, object],
    *,
    actor: object,
    request_id: str,
    store: AuditStore,
    clock: Clock | None = None,
    occurred_at: datetime | None = None,
    threshold_snapshot_id: UUID | str | None = None,
) -> object:
    """Functional form of :class:`AuditRecorder.record` for one-off callers."""

    return AuditRecorder(store, clock=clock, request_id=request_id).record(
        event_type,
        subject,
        payload,
        actor=actor,
        request_id=request_id,
        occurred_at=occurred_at,
        threshold_snapshot_id=threshold_snapshot_id,
    )


def _subject(value: AuditSubject | tuple[str, UUID | str]) -> AuditSubject:
    if isinstance(value, AuditSubject):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return AuditSubject(value[0], value[1])
    raise TypeError("Audit subject must be an AuditSubject or a (subject_type, subject_id) pair.")


def _subject_uuid(subject_type: str, value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        raise TypeError("Audit subject_id must be a UUID or a non-empty reference.")
    reference = _text(value.strip(), "subject_id", _SUBJECT_REFERENCE_MAX_LENGTH)
    try:
        return UUID(reference)
    except ValueError:
        # Some system events use a stable provider or batch reference rather
        # than an entity UUID.  The schema has only a UUID subject column, so
        # derive an opaque, deterministic UUID without retaining the source
        # reference in the audit payload.
        return uuid5(_SUBJECT_NAMESPACE, f"{subject_type}\x1f{reference}")


def _actor(value: object) -> tuple[UUID | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, UUID):
        return value, None
    if isinstance(value, str):
        return _actor_string(value)

    actor_id = getattr(value, "id", None)
    if actor_id is not None:
        parsed_id = _optional_uuid(actor_id, "actor")
        if parsed_id is None:
            raise TypeError("Audit actor.id must be a UUID or UUID string.")
        return parsed_id, None
    raise TypeError("Audit actor must be None, a UUID, UUID string, or an object with a UUID id.")


def _actor_string(value: str) -> tuple[UUID | None, str | None]:
    cleaned = _text(value, "actor", _ACTOR_LABEL_MAX_LENGTH)
    try:
        return UUID(cleaned), None
    except ValueError:
        return None, cleaned


def _optional_uuid(value: UUID | str | object | None, field: str) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValueError(f"Audit {field} must be a UUID.") from error
    raise TypeError(f"Audit {field} must be a UUID or None.")


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Audit {field} must be non-empty text.")
    cleaned = normalize("NFC", value.strip())
    if len(cleaned) > maximum:
        raise ValueError(f"Audit {field} must be at most {maximum} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError(f"Audit {field} contains a control character.")
    return cleaned


def _request_id(value: object) -> str:
    return _text(value, "request_id", _REQUEST_ID_MAX_LENGTH)


def _instant(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Audit occurred_at must be a timezone-aware datetime.")
    return value.astimezone(UTC)


# The name used by service protocols reads naturally at injection sites.
AuditWriter = AuditRecorder


__all__ = [
    "AuditRecord",
    "AuditRecorder",
    "AuditSubject",
    "AuditStore",
    "AuditWriter",
    "record",
]
