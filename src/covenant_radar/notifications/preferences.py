"""Per-user notification preferences and deterministic scheduling policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from covenant_radar.core.errors import ValidationError
from covenant_radar.ports.notifier import NotificationChannel

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")
NON_SUPPRESSIBLE_TEMPLATES: Final[frozenset[str]] = frozenset({"security_alert", "system_failure"})
_MAX_TEMPLATE_LENGTH: Final[int] = 100


class DigestFrequency(StrEnum):
    """Supported queueing frequencies; digest assembly is a later concern."""

    IMMEDIATE = "immediate"
    MORNING = "morning"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass(frozen=True, slots=True)
class NotificationPreference:
    """One user's choice for one template and one channel."""

    user_id: UUID
    template: str
    channel: NotificationChannel | str
    enabled: bool = True
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    digest_frequency: DigestFrequency | str = DigestFrequency.IMMEDIATE

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, UUID):
            raise TypeError("NotificationPreference.user_id must be a UUID.")
        template = _text(self.template, "template", _MAX_TEMPLATE_LENGTH).lower()
        if not isinstance(self.enabled, bool):
            raise TypeError("NotificationPreference.enabled must be a boolean.")
        channel = _channel(self.channel)
        frequency = _frequency(self.digest_frequency)
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            raise ValidationError(
                "Quiet hours require both a start and an end time.",
                field="quiet_hours",
            )
        if self.quiet_hours_start == self.quiet_hours_end and self.quiet_hours_start is not None:
            raise ValidationError(
                "Quiet hours start and end must differ.",
                field="quiet_hours",
            )
        for field_name, value in (
            ("quiet_hours_start", self.quiet_hours_start),
            ("quiet_hours_end", self.quiet_hours_end),
        ):
            if value is not None and not isinstance(value, time):
                raise ValidationError(f"{field_name} must be a time.", field=field_name)
        object.__setattr__(self, "template", template)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "digest_frequency", frequency)

    @property
    def suppressible(self) -> bool:
        """Whether this preference is allowed to disable its template."""

        return self.template not in NON_SUPPRESSIBLE_TEMPLATES

    def allows(self, *, template_non_suppressible: bool = False) -> bool:
        """Return whether delivery is enabled after the safety override."""

        return self.enabled or template_non_suppressible or not self.suppressible

    def schedule_for(self, instant: datetime) -> datetime:
        """Return the next delivery time, applying digest cadence then quiet hours."""

        now = _utc(instant, "instant")
        local = now.astimezone(IST)
        frequency = _frequency(self.digest_frequency)
        if frequency is DigestFrequency.IMMEDIATE:
            candidate = local
        else:
            candidate = _next_digest_window(local, frequency)
        if self.quiet_hours_start is not None and self.quiet_hours_end is not None:
            candidate = _defer_quiet(candidate, self.quiet_hours_start, self.quiet_hours_end)
        return candidate.astimezone(UTC)

    def deferred_until(self, instant: datetime) -> datetime | None:
        """Return quiet-window end when *instant* is currently quiet."""

        if self.quiet_hours_start is None or self.quiet_hours_end is None:
            return None
        now = _utc(instant, "instant").astimezone(IST)
        if not _in_quiet_window(
            now.timetz().replace(tzinfo=None), self.quiet_hours_start, self.quiet_hours_end
        ):
            return None
        return _quiet_end(
            now,
            self.quiet_hours_start,
            self.quiet_hours_end,
        ).astimezone(UTC)


def default_preference(
    user_id: UUID, template: str, channel: NotificationChannel | str
) -> NotificationPreference:
    """Build the safe default when no explicit row exists."""

    return NotificationPreference(user_id, template, channel)


def _next_digest_window(local: datetime, frequency: DigestFrequency) -> datetime:
    target = time(9, 0)
    date_value = local.date()
    if frequency in {DigestFrequency.MORNING, DigestFrequency.DAILY}:
        candidate = datetime.combine(date_value, target, tzinfo=IST)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate
    if frequency is DigestFrequency.WEEKLY:
        days_until_monday = (7 - local.weekday()) % 7
        candidate = datetime.combine(
            date_value + timedelta(days=days_until_monday),
            target,
            tzinfo=IST,
        )
        if candidate <= local:
            candidate += timedelta(days=7)
        return candidate
    raise ValidationError(f"Unsupported digest frequency {frequency!r}.", field="digest_frequency")


def _defer_quiet(candidate: datetime, start: time, end: time) -> datetime:
    local = candidate.astimezone(IST)
    if _in_quiet_window(local.timetz().replace(tzinfo=None), start, end):
        return _quiet_end(local, start, end)
    return local


def _in_quiet_window(value: time, start: time, end: time) -> bool:
    if start < end:
        return start <= value < end
    return value >= start or value < end


def _quiet_end(local: datetime, start: time, end: time) -> datetime:
    date_value = local.date()
    if start > end and local.timetz().replace(tzinfo=None) >= start:
        date_value += timedelta(days=1)
    return datetime.combine(date_value, end, tzinfo=IST)


def _channel(value: NotificationChannel | str) -> NotificationChannel:
    if isinstance(value, NotificationChannel):
        return value
    if not isinstance(value, str):
        raise ValidationError("channel must be text.", field="channel")
    try:
        return NotificationChannel(value.strip().lower())
    except ValueError as error:
        allowed = ", ".join(item.value for item in NotificationChannel)
        raise ValidationError(f"channel must be one of: {allowed}.", field="channel") from error


def _frequency(value: DigestFrequency | str) -> DigestFrequency:
    if isinstance(value, DigestFrequency):
        return value
    if not isinstance(value, str):
        raise ValidationError("digest_frequency must be text.", field="digest_frequency")
    try:
        return DigestFrequency(value.strip().lower())
    except ValueError as error:
        allowed = ", ".join(item.value for item in DigestFrequency)
        raise ValidationError(
            f"digest_frequency must be one of: {allowed}.",
            field="digest_frequency",
        ) from error


def _text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValidationError(
            f"{field_name} must be non-blank text of at most {maximum} characters.",
            field=field_name,
        )
    return value.strip()


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware.", field=field_name)
    return value.astimezone(UTC)


__all__ = [
    "DigestFrequency",
    "IST",
    "NON_SUPPRESSIBLE_TEMPLATES",
    "NotificationPreference",
    "default_preference",
]
