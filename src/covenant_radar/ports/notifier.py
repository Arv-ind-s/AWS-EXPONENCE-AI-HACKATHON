"""The outbound notification port.

The notification service owns recipient resolution, rendering, preference
policy and durable delivery state.  Channel adapters only receive this
already-authorised message and report what happened.  Keeping that boundary
small makes it possible to run the complete notification policy against an
in-memory adapter in tests and to add email, webhook and in-app transports
without duplicating disclosure rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from covenant_radar.core.errors import ValidationError

_MAX_TEMPLATE_LENGTH: Final[int] = 100
_MAX_CHANNEL_LENGTH: Final[int] = 20
_MAX_SUBJECT_TYPE_LENGTH: Final[int] = 50
_MAX_BODY_LENGTH: Final[int] = 100_000
_MAX_PROVIDER_ID_LENGTH: Final[int] = 200
_MAX_ERROR_LENGTH: Final[int] = 2_000


class NotificationChannel(StrEnum):
    """Channels implemented by the notification adapters."""

    IN_APP = "inapp"
    EMAIL = "email"
    WEBHOOK = "webhook"


class DeliveryStatus(StrEnum):
    """Adapter outcome understood by the durable delivery service."""

    SENT = "sent"
    RETRY = "retry"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A fully rendered, scope-filtered message ready for one adapter.

    ``payload`` is retained for channel-specific structured delivery (for
    example a webhook) but it is required to be the same safe data used to
    render ``body``.  Adapters must not fetch records or add fields.
    """

    recipient_id: UUID
    channel: NotificationChannel | str
    template: str
    subject: str
    body: str
    payload: Mapping[str, object]
    subject_type: str | None = None
    subject_id: UUID | None = None
    scheduled_for: datetime | None = None
    attempt: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.recipient_id, UUID):
            raise TypeError("OutboundMessage.recipient_id must be a UUID.")
        channel = _choice(self.channel, "channel", NotificationChannel, _MAX_CHANNEL_LENGTH)
        template = _text(self.template, "template", _MAX_TEMPLATE_LENGTH)
        subject = _text(self.subject, "subject", 500)
        body = _text(self.body, "body", _MAX_BODY_LENGTH)
        if not isinstance(self.payload, Mapping):
            raise TypeError("OutboundMessage.payload must be a mapping.")
        if self.subject_type is None and self.subject_id is not None:
            raise ValidationError("subject_id requires subject_type.", field="subject_id")
        if self.subject_type is not None:
            subject_type = _text(
                self.subject_type,
                "subject_type",
                _MAX_SUBJECT_TYPE_LENGTH,
            ).lower()
            if self.subject_id is None:
                raise ValidationError("subject_type requires subject_id.", field="subject_id")
        else:
            subject_type = None
        if self.scheduled_for is not None:
            _aware_utc(self.scheduled_for, "scheduled_for")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValidationError("attempt must be a positive integer.", field="attempt")
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "template", template)
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "subject_type", subject_type)
        object.__setattr__(self, "scheduled_for", _aware_utc(self.scheduled_for, "scheduled_for"))


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The adapter's explicit result; an adapter may never silently drop."""

    status: DeliveryStatus | str
    provider_message_id: str | None = None
    error: str | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        status = _choice(self.status, "status", DeliveryStatus, 20)
        if self.provider_message_id is not None:
            _text(self.provider_message_id, "provider_message_id", _MAX_PROVIDER_ID_LENGTH)
        if self.error is not None:
            _text(self.error, "error", _MAX_ERROR_LENGTH)
        if self.retry_after_seconds is not None and (
            isinstance(self.retry_after_seconds, bool)
            or not isinstance(self.retry_after_seconds, int)
            or self.retry_after_seconds < 0
        ):
            raise ValidationError(
                "retry_after_seconds must be a non-negative integer.",
                field="retry_after_seconds",
            )
        if status is DeliveryStatus.SENT and self.error is not None:
            raise ValidationError("A sent delivery cannot carry an error.", field="error")
        if status is DeliveryStatus.DEAD_LETTERED and not self.error:
            raise ValidationError(
                "A dead-lettered delivery must carry an error.",
                field="error",
            )
        object.__setattr__(self, "status", status)

    @property
    def delivered(self) -> bool:
        """Whether the adapter has durably accepted the message."""

        return self.status is DeliveryStatus.SENT


@runtime_checkable
class Notifier(Protocol):
    """The single outbound delivery seam required by contract C-54."""

    def send(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver one rendered message or return an explicit failure."""
        ...


def _choice(
    value: object,
    field_name: str,
    enum_type: type[StrEnum],
    maximum: int,
) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text.", field=field_name)
    normalized = value.strip().lower()
    if not normalized or len(normalized) > maximum:
        raise ValidationError(
            f"{field_name} must be non-blank text of at most {maximum} characters.",
            field=field_name,
        )
    try:
        return enum_type(normalized)
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValidationError(
            f"{field_name} must be one of: {allowed}.",
            field=field_name,
        ) from error


def _text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text.", field=field_name)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValidationError(
            f"{field_name} must be non-blank text of at most {maximum} characters.",
            field=field_name,
        )
    if any(ord(character) < 32 and character not in "\n\r\t" for character in normalized):
        raise ValidationError(f"{field_name} contains a control character.", field=field_name)
    return normalized


def _aware_utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware.", field=field_name)
    return value.astimezone(UTC)


__all__ = [
    "DeliveryResult",
    "DeliveryStatus",
    "NotificationChannel",
    "Notifier",
    "OutboundMessage",
]
