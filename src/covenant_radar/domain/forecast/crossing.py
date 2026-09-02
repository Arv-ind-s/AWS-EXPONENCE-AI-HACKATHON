"""Deterministic threshold crossing and dating for forecast paths.

Crossing is intentionally separate from projection.  Projection owns the
trajectory; this module owns the contractual boundary and converts a day
offset into a calendar date when an as-of date is available.  The boundary is
the same inclusive convention used by the covenant engine: ``max`` covenants
cross at or above their threshold and ``min`` covenants cross at or below it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as CalendarDate
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, cast

from covenant_radar.domain.forecast.path import PathPoint, Projection
from covenant_radar.domain.forecast.trend import Direction

_ZERO: Final[Decimal] = Decimal("0")
_MAX_TEXT_LENGTH: Final[int] = 200


@dataclass(frozen=True, slots=True)
class ThresholdChange:
    """One effective-dated or day-indexed threshold change.

    Exactly one of ``effective_day`` and ``effective_date`` is normally
    supplied.  A date is resolved against the caller's forecast as-of date;
    the normalized result is exposed as a :class:`ThresholdPoint` in the
    returned crossing result.
    """

    threshold: Decimal
    effective_day: int | None = None
    effective_date: CalendarDate | str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.effective_day is None) == (self.effective_date is None):
            raise ValueError("Provide exactly one of effective_day or effective_date.")
        threshold = _decimal(self.threshold, "threshold")
        if self.effective_day is not None:
            _non_negative_integer(self.effective_day, "effective_day")
        effective_date = (
            None
            if self.effective_date is None
            else _calendar_date(self.effective_date, "effective_date")
        )
        reason = self.reason
        if reason is not None:
            reason = _bounded_text(reason, "reason", _MAX_TEXT_LENGTH)
        object.__setattr__(self, "threshold", threshold)
        object.__setattr__(self, "effective_date", effective_date)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True, slots=True)
class ThresholdPoint:
    """The threshold effective at one projected day."""

    day: int
    threshold: Decimal

    def __post_init__(self) -> None:
        _non_negative_integer(self.day, "day")
        object.__setattr__(self, "threshold", _decimal(self.threshold, "threshold"))

    @property
    def day_offset(self) -> int:
        return self.day

    @property
    def value(self) -> Decimal:
        return self.threshold


@dataclass(frozen=True, slots=True)
class CrossingResult:
    """The first contractual threshold crossing, or an explicit no-crossing result."""

    direction: Direction
    threshold: Decimal
    path: tuple[PathPoint, ...]
    threshold_path: tuple[ThresholdPoint, ...]
    crossing_day: int | None = None
    crossing_date: CalendarDate | None = None
    crossing_value: Decimal | None = None
    threshold_used: Decimal | None = None
    margin: Decimal | None = None
    as_of_date: CalendarDate | None = None
    reason: str | None = None
    threshold_changes: tuple[ThresholdPoint, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        normalized_direction = Direction.from_value(self.direction)
        threshold = _decimal(self.threshold, "threshold")
        if len(self.path) != len(self.threshold_path):
            raise ValueError("threshold_path must contain one point per forecast path point.")
        if self.path and tuple(point.day for point in self.path) != tuple(range(len(self.path))):
            raise ValueError("Forecast path days must be contiguous and start at zero.")
        if self.threshold_path and tuple(point.day for point in self.threshold_path) != tuple(
            range(len(self.threshold_path))
        ):
            raise ValueError("Threshold path days must be contiguous and start at zero.")
        if self.crossing_day is None:
            if any(
                value is not None
                for value in (
                    self.crossing_date,
                    self.crossing_value,
                    self.threshold_used,
                    self.margin,
                )
            ):
                raise ValueError("A non-crossing result cannot carry crossing values.")
        else:
            if not 0 <= self.crossing_day < len(self.path):
                raise ValueError("crossing_day must identify a point in the forecast path.")
            if self.crossing_value is None or self.threshold_used is None or self.margin is None:
                raise ValueError("A crossing result must carry value, threshold and margin.")
            if self.crossing_date is not None and self.as_of_date is None:
                raise ValueError("crossing_date requires as_of_date.")
        if self.reason is not None:
            _bounded_text(self.reason, "reason", _MAX_TEXT_LENGTH)
        object.__setattr__(self, "direction", normalized_direction)
        object.__setattr__(self, "threshold", threshold)
        if self.as_of_date is not None:
            object.__setattr__(self, "as_of_date", _calendar_date(self.as_of_date, "as_of_date"))

    @property
    def crossed(self) -> bool:
        return self.crossing_day is not None

    @property
    def day(self) -> int | None:
        return self.crossing_day

    @property
    def value(self) -> Decimal | None:
        return self.crossing_value

    @property
    def date(self) -> CalendarDate | None:
        return self.crossing_date

    @property
    def projected_cross_date(self) -> CalendarDate | None:
        return self.crossing_date

    @property
    def crossing_margin(self) -> Decimal | None:
        return self.margin

    @property
    def threshold_at_crossing(self) -> Decimal | None:
        return self.threshold_used

    @property
    def effective_thresholds(self) -> tuple[ThresholdPoint, ...]:
        return self.threshold_path


def first_crossing(
    path: Projection | Sequence[PathPoint | Mapping[str, object] | Decimal | None],
    threshold: Decimal | None = None,
    direction: Direction | str | None = None,
    as_of_date: CalendarDate | str | None = None,
    *,
    as_of: CalendarDate | str | None = None,
    current_date: CalendarDate | str | None = None,
    threshold_changes: Iterable[ThresholdChange | Mapping[str, object] | Sequence[object]] = (),
    threshold_schedule: Iterable[ThresholdChange | Mapping[str, object] | Sequence[object]]
    | None = None,
) -> CrossingResult:
    """Return the first inclusive threshold crossing in a forecast path.

    A threshold change takes effect on its effective day, including that day.
    Changes may use an ``effective_date`` when ``as_of_date`` is supplied, or
    an ``effective_day`` directly.  The returned ``threshold_path`` records
    the effective threshold at every forecast day, including days where no
    crossing occurs.

    ``as_of_date`` is optional for callers that only need the crossing day and
    value.  When a :class:`Projection` contains dated observations, its latest
    usable observation date is used automatically.  A calendar crossing date
    is never invented when no date is available.
    """

    projection = path if isinstance(path, Projection) else None
    normalized_path = _normalise_path(path)
    normalized_threshold = _resolve_threshold(threshold, projection)
    normalized_direction = _resolve_direction(direction, projection)
    normalized_as_of = _resolve_as_of(as_of_date, as_of, current_date, projection)
    changes = threshold_changes
    if threshold_schedule is not None:
        if tuple(threshold_changes):
            raise ValueError("Provide threshold_changes or threshold_schedule, not both.")
        changes = threshold_schedule
    normalized_changes = _normalise_changes(
        changes,
        normalized_as_of,
        horizon_days=max(0, len(normalized_path) - 1),
    )
    threshold_path = _threshold_path(normalized_path, normalized_threshold, normalized_changes)
    path_days = tuple(zip(normalized_path, threshold_path, strict=True))

    missing_value_seen = False
    for point, threshold_point in path_days:
        if point.value is None:
            missing_value_seen = True
            continue
        if _breaches(point.value, threshold_point.threshold, normalized_direction):
            if missing_value_seen:
                return _no_crossing(
                    normalized_path,
                    threshold_path,
                    normalized_changes,
                    normalized_threshold,
                    normalized_direction,
                    normalized_as_of,
                    "projection path contains unavailable values before a possible crossing",
                )
            crossing_date = (
                None if normalized_as_of is None else normalized_as_of + timedelta(days=point.day)
            )
            return CrossingResult(
                direction=normalized_direction,
                threshold=normalized_threshold,
                path=normalized_path,
                threshold_path=threshold_path,
                crossing_day=point.day,
                crossing_date=crossing_date,
                crossing_value=point.value,
                threshold_used=threshold_point.threshold,
                margin=_breach_margin(point.value, threshold_point.threshold, normalized_direction),
                as_of_date=normalized_as_of,
                threshold_changes=normalized_changes,
            )

    if not normalized_path or all(point.value is None for point in normalized_path):
        reason = "projection path contains no computable values"
    elif missing_value_seen:
        reason = "projection path contains unavailable values"
    elif _is_improving(normalized_path, normalized_direction):
        reason = (
            f"trajectory is moving away from the threshold for direction "
            f"{normalized_direction.value!r}"
        )
    else:
        reason = "trajectory does not cross the threshold within the forecast horizon"
    return _no_crossing(
        normalized_path,
        threshold_path,
        normalized_changes,
        normalized_threshold,
        normalized_direction,
        normalized_as_of,
        reason,
    )


def crossing(
    path: Projection | Sequence[PathPoint | Mapping[str, object] | Decimal | None],
    threshold: Decimal | None = None,
    direction: Direction | str | None = None,
    as_of_date: CalendarDate | str | None = None,
    *,
    as_of: CalendarDate | str | None = None,
    current_date: CalendarDate | str | None = None,
    threshold_changes: Iterable[ThresholdChange | Mapping[str, object] | Sequence[object]] = (),
    threshold_schedule: Iterable[ThresholdChange | Mapping[str, object] | Sequence[object]]
    | None = None,
) -> CrossingResult:
    """Compatibility entry point for :func:`first_crossing`."""

    return first_crossing(
        path,
        threshold,
        direction,
        as_of_date,
        as_of=as_of,
        current_date=current_date,
        threshold_changes=threshold_changes,
        threshold_schedule=threshold_schedule,
    )


def crossing_for_projection(
    projection: Projection,
    as_of_date: CalendarDate | str | None = None,
    *,
    threshold_changes: Iterable[ThresholdChange | Mapping[str, object] | Sequence[object]] = (),
) -> CrossingResult:
    """Find a crossing using the threshold and direction stored on a projection."""

    return first_crossing(
        projection,
        as_of_date=as_of_date,
        threshold_changes=threshold_changes,
    )


def _no_crossing(
    path: tuple[PathPoint, ...],
    threshold_path: tuple[ThresholdPoint, ...],
    changes: tuple[ThresholdPoint, ...],
    threshold: Decimal,
    direction: Direction,
    as_of_date: CalendarDate | None,
    reason: str,
) -> CrossingResult:
    return CrossingResult(
        direction=direction,
        threshold=threshold,
        path=path,
        threshold_path=threshold_path,
        as_of_date=as_of_date,
        reason=reason,
        threshold_changes=changes,
    )


def _normalise_path(
    value: Projection | Sequence[PathPoint | Mapping[str, object] | Decimal | None],
) -> tuple[PathPoint, ...]:
    raw = value.path if isinstance(value, Projection) else value
    if isinstance(raw, str | bytes | bytearray):
        raise TypeError("path must be an iterable of projected values, not text.")
    try:
        iterator = iter(raw)
    except TypeError as error:
        raise TypeError("path must be an iterable of projected values.") from error
    points: list[PathPoint] = []
    for index, item in enumerate(iterator):
        if isinstance(item, PathPoint):
            point = item
        elif isinstance(item, Mapping):
            day = _read(item, "day", "day_offset", default=index)
            projected_value = _read(item, "value", "projected_value", default=None)
            point = PathPoint(
                day=_non_negative_integer_value(day, "day"),
                value=(
                    None
                    if projected_value is None
                    else _decimal(projected_value, "projected_value")
                ),
                trend_component=_decimal(
                    _read(item, "trend_component", default=_ZERO),
                    "trend_component",
                ),
                pressure_component=_decimal(
                    _read(item, "pressure_component", default=_ZERO), "pressure_component"
                ),
            )
        else:
            point = PathPoint(
                day=index,
                value=None if item is None else _decimal(item, "projected_value"),
                trend_component=_ZERO,
                pressure_component=_ZERO,
            )
        if point.day != index:
            raise ValueError("path days must be contiguous and start at zero.")
        points.append(point)
    return tuple(points)


def _resolve_threshold(threshold: Decimal | None, projection: Projection | None) -> Decimal:
    if threshold is None:
        if projection is None:
            raise TypeError("threshold is required when path is not a Projection.")
        threshold = projection.threshold
    return _decimal(threshold, "threshold")


def _resolve_direction(
    direction: Direction | str | None,
    projection: Projection | None,
) -> Direction:
    if direction is None:
        if projection is None:
            raise TypeError("direction is required when path is not a Projection.")
        direction = projection.direction
    return Direction.from_value(direction)


def _resolve_as_of(
    as_of_date: CalendarDate | str | None,
    as_of: CalendarDate | str | None,
    current_date: CalendarDate | str | None,
    projection: Projection | None,
) -> CalendarDate | None:
    supplied = tuple(
        candidate for candidate in (as_of_date, as_of, current_date) if candidate is not None
    )
    if len(supplied) > 1:
        normalized = tuple(_calendar_date(value, "as_of_date") for value in supplied)
        if len(set(normalized)) != 1:
            raise ValueError("as_of_date, as_of, and current_date must agree.")
        return normalized[0]
    if supplied:
        return _calendar_date(supplied[0], "as_of_date")
    if projection is not None and projection.trend.usable_observations:
        return projection.trend.usable_observations[-1].observed_on
    return None


def _normalise_changes(
    values: Iterable[ThresholdChange | Mapping[str, object] | Sequence[object]],
    as_of_date: CalendarDate | None,
    *,
    horizon_days: int,
) -> tuple[ThresholdPoint, ...]:
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("threshold changes must be iterable records, not text.")
    records = _change_records(values)
    normalized: list[ThresholdPoint] = []
    for record in records:
        day, threshold = _change_record(record, as_of_date)
        if day > horizon_days:
            raise ValueError(
                f"threshold change day {day} is outside the forecast horizon 0..{horizon_days}."
            )
        normalized.append(ThresholdPoint(day=day, threshold=threshold))
    normalized.sort(key=lambda item: item.day)
    if len({item.day for item in normalized}) != len(normalized):
        raise ValueError("Only one threshold change may take effect on a forecast day.")
    return tuple(normalized)


def _change_records(
    values: Iterable[ThresholdChange | Mapping[str, object] | Sequence[object]],
) -> tuple[ThresholdChange | Mapping[str, object] | Sequence[object], ...]:
    if isinstance(values, Mapping):
        change_keys = {
            "day",
            "day_offset",
            "effective_day",
            "date",
            "effective_date",
            "effective_on",
        }
        if change_keys.intersection(values):
            return (values,)
        return tuple((key, item) for key, item in values.items())
    try:
        return tuple(values)
    except TypeError as error:
        raise TypeError("threshold changes must be iterable records.") from error


def _change_record(
    record: ThresholdChange | Mapping[str, object] | Sequence[object],
    as_of_date: CalendarDate | None,
) -> tuple[int, Decimal]:
    if isinstance(record, ThresholdChange):
        threshold = record.threshold
        if record.effective_day is not None:
            return record.effective_day, threshold
        assert record.effective_date is not None
        if as_of_date is None:
            raise ValueError("as_of_date is required for date-based threshold changes.")
        return _date_day(record.effective_date, as_of_date), threshold
    if isinstance(record, Mapping):
        effective_day = _read(record, "effective_day", "day", "day_offset", default=None)
        effective_date = _read(record, "effective_date", "effective_on", "date", default=None)
        threshold_value = _read(record, "threshold", "threshold_used", "value", default=None)
    else:
        if isinstance(record, str | bytes | bytearray) or len(record) != 2:
            raise ValueError("A threshold change sequence must contain (effective_day, threshold).")
        effective_day, threshold_value = record
        effective_date = effective_day if isinstance(effective_day, CalendarDate | str) else None
    if threshold_value is None:
        raise ValueError("A threshold change must provide threshold.")
    if effective_day is not None and effective_date is not None:
        raise ValueError("A threshold change cannot provide both day and date.")
    if effective_day is not None:
        return _non_negative_integer_value(effective_day, "effective_day"), _decimal(
            threshold_value, "threshold"
        )
    if effective_date is None:
        raise ValueError("A threshold change requires effective_day or effective_date.")
    if as_of_date is None:
        raise ValueError("as_of_date is required for date-based threshold changes.")
    return _date_day(effective_date, as_of_date), _decimal(threshold_value, "threshold")


def _threshold_path(
    path: tuple[PathPoint, ...],
    base_threshold: Decimal,
    changes: tuple[ThresholdPoint, ...],
) -> tuple[ThresholdPoint, ...]:
    changes_by_day = {change.day: change.threshold for change in changes}
    active = base_threshold
    points: list[ThresholdPoint] = []
    for point in path:
        if point.day in changes_by_day:
            active = changes_by_day[point.day]
        points.append(ThresholdPoint(day=point.day, threshold=active))
    return tuple(points)


def _breaches(value: Decimal, threshold: Decimal, direction: Direction) -> bool:
    return value >= threshold if direction is Direction.MAX else value <= threshold


def _breach_margin(value: Decimal, threshold: Decimal, direction: Direction) -> Decimal:
    return value - threshold if direction is Direction.MAX else threshold - value


def _is_improving(path: tuple[PathPoint, ...], direction: Direction) -> bool:
    values = tuple(point.value for point in path if point.value is not None)
    if len(values) < 2:
        return False
    first, last = values[0], values[-1]
    deterioration_delta = last - first if direction is Direction.MAX else first - last
    return deterioration_delta < _ZERO


def _read(value: Mapping[str, object], *names: str, default: object = None) -> object:
    for name in names:
        if name in value:
            return value[name]
    return default


def _date_day(value: object, as_of_date: CalendarDate) -> int:
    effective_date = _calendar_date(value, "effective_date")
    day = (effective_date - as_of_date).days
    if day < 0:
        raise ValueError("threshold change effective_date must not precede as_of_date.")
    return day


def _calendar_date(value: object, field_name: str) -> CalendarDate:
    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a calendar date, not a datetime.")
    if isinstance(value, CalendarDate):
        return value
    if isinstance(value, str):
        try:
            return CalendarDate.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO calendar date.") from error
    raise TypeError(f"{field_name} must be a calendar date.")


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field_name} must be a finite Decimal.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite Decimal.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal.")
    return result


def _non_negative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _non_negative_integer_value(value: object, field_name: str) -> int:
    _non_negative_integer(value, field_name)
    return cast(int, value)


def _bounded_text(value: object, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains a control character.")
    return normalized


# Keep the vocabulary discoverable for callers that use a verb-first or
# noun-first name; all aliases point to this one implementation.
find_crossing = first_crossing
crossing_date = first_crossing
date_crossing = first_crossing


__all__ = [
    "CrossingResult",
    "ThresholdChange",
    "ThresholdPoint",
    "crossing",
    "crossing_date",
    "crossing_for_projection",
    "date_crossing",
    "find_crossing",
    "first_crossing",
]
