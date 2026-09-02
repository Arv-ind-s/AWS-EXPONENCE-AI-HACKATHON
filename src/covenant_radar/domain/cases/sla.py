"""T11 service-level agreement derivation.

T11 is read from the active threshold snapshot by the caller.  This module
accepts that snapshot's structural shape and does not import configuration or
perform I/O, which keeps the calculation deterministic and easy to replay.
The default calendar measures the configured hours as elapsed aware UTC time;
deployments that define working-hour calendars can inject an implementation of
``BusinessCalendar`` at the service boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, cast

from covenant_radar.core.errors import ValidationError

_T11: Final[str] = "T11"
_ACT: Final[str] = "act"
_AMBER: Final[str] = "amber"
_WATCH: Final[str] = "watch"
_BANDS: Final[tuple[str, ...]] = (_ACT, _AMBER, _WATCH)
_FIELDS: Final[tuple[str, ...]] = (
    "act_sla_hours",
    "amber_sla_hours",
    "watch_sla_hours",
)


class BusinessCalendar(Protocol):
    """Boundary for deployments that use working-hour calendars."""

    def add_hours(self, started_at: datetime, hours: int) -> datetime:
        """Return the deadline after ``hours`` according to the calendar."""


class ElapsedHoursCalendar:
    """The documented default: T11 hours are elapsed UTC hours."""

    __slots__ = ()

    def add_hours(self, started_at: datetime, hours: int) -> datetime:
        _aware_utc(started_at, "started_at")
        return started_at.astimezone(UTC) + timedelta(hours=hours)


@dataclass(frozen=True, slots=True)
class SlaThresholds:
    """Validated T11 SLA hours, ordered from most to least urgent band."""

    act_sla_hours: int
    amber_sla_hours: int
    watch_sla_hours: int

    def __post_init__(self) -> None:
        values = tuple(
            _positive_integer(value, f"T11.{field}")
            for field, value in zip(_FIELDS, self._values(), strict=True)
        )
        if not values[0] <= values[1] <= values[2]:
            raise ValidationError(
                "T11 invariant: SLA hours must be ordered act <= amber <= watch.", field=_T11
            )
        object.__setattr__(self, "act_sla_hours", values[0])
        object.__setattr__(self, "amber_sla_hours", values[1])
        object.__setattr__(self, "watch_sla_hours", values[2])

    def _values(self) -> tuple[int, int, int]:
        return self.act_sla_hours, self.amber_sla_hours, self.watch_sla_hours

    @classmethod
    def from_store(cls, store: object) -> SlaThresholds:
        """Build T11 from a threshold store, mapping, or simple object."""

        if isinstance(store, cls):
            return store
        section = _t11_section(store)
        return cls(
            _field(section, "act_sla_hours", "act"),
            _field(section, "amber_sla_hours", "amber"),
            _field(section, "watch_sla_hours", "watch"),
        )

    def as_mapping(self) -> Mapping[str, Mapping[str, int]]:
        """Return the threshold-store shape used by snapshots and traces."""

        return {
            _T11: {
                "act_sla_hours": self.act_sla_hours,
                "amber_sla_hours": self.amber_sla_hours,
                "watch_sla_hours": self.watch_sla_hours,
            }
        }


@dataclass(frozen=True, slots=True)
class SlaDeadline:
    """The immutable result of applying T11 to one case warning."""

    band: str
    hours: int
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        normalized_band = _band(self.band)
        started = _aware_utc(self.started_at, "started_at")
        deadline = _aware_utc(self.due_at, "due_at")
        if deadline < started:
            raise ValidationError("due_at must not precede started_at.", field="case.due_at")
        if not isinstance(self.hours, int) or isinstance(self.hours, bool) or self.hours <= 0:
            raise ValidationError("SLA hours must be a positive integer.", field="case.sla_hours")
        object.__setattr__(self, "band", normalized_band)
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "due_at", deadline)

    @property
    def overdue_at_due(self) -> bool:
        """Document the T11 boundary convention for callers displaying it."""

        return True


def sla_hours(
    band: str,
    thresholds: SlaThresholds | Mapping[str, object] | object,
) -> int:
    """Return T11's inclusive-band SLA duration in hours."""

    normalized_band = _band(band)
    configured = SlaThresholds.from_store(thresholds)
    return {
        _ACT: configured.act_sla_hours,
        _AMBER: configured.amber_sla_hours,
        _WATCH: configured.watch_sla_hours,
    }[normalized_band]


def derive_sla(
    band: str,
    started_at: datetime,
    thresholds: SlaThresholds | Mapping[str, object] | object,
    *,
    calendar: BusinessCalendar | Callable[[datetime, int], datetime] | None = None,
) -> SlaDeadline:
    """Apply T11 to ``started_at`` and return the durable deadline."""

    normalized_start = _aware_utc(started_at, "started_at")
    hours = sla_hours(band, thresholds)
    deadline = _add_hours(calendar, normalized_start, hours)
    return SlaDeadline(_band(band), hours, normalized_start, deadline)


def due_at(
    started_at: datetime,
    band: str,
    thresholds: SlaThresholds | Mapping[str, object] | object,
    *,
    calendar: BusinessCalendar | Callable[[datetime, int], datetime] | None = None,
) -> datetime:
    """Compatibility helper returning only the derived due instant."""

    return derive_sla(band, started_at, thresholds, calendar=calendar).due_at


def is_overdue(due: datetime, now: datetime) -> bool:
    """Return whether T11 has expired; equality at the due instant is overdue."""

    deadline = _aware_utc(due, "due_at")
    current = _aware_utc(now, "now")
    return current >= deadline


def _add_hours(
    calendar: BusinessCalendar | Callable[[datetime, int], datetime] | None,
    started_at: datetime,
    hours: int,
) -> datetime:
    adapter = calendar or ElapsedHoursCalendar()
    if callable(adapter) and not callable(getattr(adapter, "add_hours", None)):
        result = adapter(started_at, hours)
    else:
        add = getattr(adapter, "add_hours", None)
        if not callable(add):
            raise TypeError("calendar must expose add_hours(started_at, hours).")
        result = add(started_at, hours)
    return _aware_utc(result, "calendar due_at")


def _t11_section(store: object) -> Mapping[str, object]:
    if isinstance(store, Mapping):
        section = store.get(_T11, store)
        return _mapping_section(section)

    getter = getattr(store, "get", None)
    if callable(getter):
        section = getter(_T11)
        if section is not None:
            return _mapping_section(section)

    for name in (_T11, "t11"):
        section = getattr(store, name, None)
        if section is not None:
            return _mapping_section(section)
    raise ValidationError("T11 threshold section is missing.", field=_T11)


def _mapping_section(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    values = {field: getattr(value, field, None) for field in _FIELDS}
    values.update(
        {
            _ACT: getattr(value, _ACT, None),
            _AMBER: getattr(value, _AMBER, None),
            _WATCH: getattr(value, _WATCH, None),
        }
    )
    if any(item is not None for item in values.values()):
        return values
    raise ValidationError("T11 threshold section must be a mapping or object.", field=_T11)


def _field(section: Mapping[str, object], field: str, short_name: str) -> int:
    value = section.get(field, section.get(short_name))
    if value is None:
        raise ValidationError(f"T11 threshold is missing {field!r}.", field=f"T11.{field}")
    return _positive_integer(value, f"T11.{field}")


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field} must be a positive integer.", field=field)
    return value


def _band(value: object) -> str:
    if not isinstance(value, str) or value.strip().lower() not in _BANDS:
        raise ValidationError(f"Case band must be one of {', '.join(_BANDS)}.", field="case.band")
    return value.strip().lower()


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be a timezone-aware datetime.", field=field)
    return value.astimezone(UTC)


__all__ = [
    "BusinessCalendar",
    "ElapsedHoursCalendar",
    "SlaDeadline",
    "SlaThresholds",
    "derive_sla",
    "due_at",
    "is_overdue",
    "sla_hours",
]
