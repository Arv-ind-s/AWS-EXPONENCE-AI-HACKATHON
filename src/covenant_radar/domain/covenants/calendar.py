"""Testing calendar: due-date generation, FY-calendar adjustment, retest
triggers and schedule-state transitions for `covenant_schedule` (`T-035`).

`spec §R-08` requires two independent things of the same `covenant_schedule`
row: it is due on the date the contract's frequency and the bank's fiscal-year
calendar say it is due, *and* it is retested whenever a statement, waiver,
exception or conduct change touches its inputs, regardless of the calendar.
This module owns the pure rules behind both halves — due-date generation
with holiday/weekend adjustment, and the retest-trigger vocabulary a caller
resolves to affected covenant versions — so `services/engine.py` (the only
adapter that touches the database) never has to re-derive either rule.

Nothing here imports SQLAlchemy or any other adapter.  Functions that need a
persisted `covenant_schedule` row accept it duck-typed, the same convention
`domain/covenants/cure.py` and `domain/covenants/exceptions.py` use: a
persisted ORM row and a lightweight test double are equally acceptable as
long as they carry the same field names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Final
from uuid import UUID

_VALID_FREQUENCIES: Final[frozenset[str]] = frozenset(
    {"monthly", "quarterly", "half_yearly", "annual", "on_event"}
)

#: `date.weekday()` values for Saturday and Sunday — the bank-holiday
#: convention this module assumes; there is no configuration point for a
#: different weekend because none of the seed data or `spec §R-08` calls
#: for one.
_WEEKEND_WEEKDAYS: Final[frozenset[int]] = frozenset({5, 6})

#: Month offsets (0-indexed, from the fiscal year's start month) whose
#: month-end is a period boundary for each testing frequency. `monthly`
#: covers every month; `quarterly`/`half_yearly`/`annual` land on the
#: fiscal quarter-, half- and year-end exactly, wherever the fiscal year
#: itself starts.
_FREQUENCY_MONTH_OFFSETS: Final[dict[str, tuple[int, ...]]] = {
    "monthly": tuple(range(12)),
    "quarterly": (2, 5, 8, 11),
    "half_yearly": (5, 11),
    "annual": (11,),
}

#: How far past `effective_from` `first_due_date` searches before concluding
#: the frequency table itself is broken rather than the data: one calendar
#: year plus slack safely exceeds `annual`'s twelve-month interval, the
#: longest of the four calendar-bearing frequencies.
_FIRST_DUE_DATE_SEARCH_DAYS: Final[int] = 400


class AdjustmentConvention(str, Enum):
    """How a due date landing on a non-business day is moved."""

    NEXT_BUSINESS_DAY = "next_business_day"
    PREVIOUS_BUSINESS_DAY = "previous_business_day"
    NONE = "none"


class ScheduleState(str, Enum):
    """The closed lifecycle vocabulary for one `covenant_schedule` row."""

    DUE = "due"
    TESTED = "tested"
    MISSED = "missed"
    NOT_APPLICABLE = "not_applicable"


#: States a retirement or a missed-due sweep may still move — a `tested`
#: settled fact and an already `not_applicable` row are left alone.
_OPEN_SCHEDULE_STATES: Final[frozenset[str]] = frozenset(
    {ScheduleState.DUE.value, ScheduleState.MISSED.value}
)


class RetestTriggerKind(str, Enum):
    """The five data changes `spec §R-08` requires a retest for."""

    STATEMENT = "statement"
    RESTATEMENT = "restatement"
    WAIVER = "waiver"
    EXCEPTION = "exception"
    CONDUCT = "conduct"


#: Which scope field each trigger kind must carry, so a trigger can be
#: resolved to affected covenant versions without guessing which foreign key
#: is meaningful for it.
_TRIGGER_SCOPE_FIELDS: Final[dict[RetestTriggerKind, str]] = {
    RetestTriggerKind.STATEMENT: "borrower_id",
    RetestTriggerKind.RESTATEMENT: "borrower_id",
    RetestTriggerKind.CONDUCT: "facility_id",
    RetestTriggerKind.WAIVER: "covenant_id",
    RetestTriggerKind.EXCEPTION: "covenant_version_id",
}


def _validate_calendar_date(value: object, name: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{name} must be a calendar date.")


def _coerce_convention(value: object) -> AdjustmentConvention:
    if isinstance(value, AdjustmentConvention):
        return value
    if isinstance(value, str):
        try:
            return AdjustmentConvention(value)
        except ValueError as error:
            raise ValueError(f"Unknown adjustment convention {value!r}.") from error
    raise TypeError("An adjustment convention must be a string or AdjustmentConvention.")


@dataclass(frozen=True, slots=True)
class FiscalCalendar:
    """A persistence-neutral FY calendar — `db/seed/data/calendar.json`'s
    `"calendar"` object, validated and typed.

    ``weekend_adjustment`` and ``holiday_adjustment`` are deliberately
    separate fields: a bank may, for example, push a holiday-hit due date
    forward while pulling a weekend-hit one back. `adjust_due_date` picks
    between them by what actually caused the date to be non-business, never
    by a single blended convention.
    """

    fiscal_year_start_month: int
    weekend_adjustment: AdjustmentConvention = AdjustmentConvention.NEXT_BUSINESS_DAY
    holiday_adjustment: AdjustmentConvention = AdjustmentConvention.NEXT_BUSINESS_DAY
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        if isinstance(self.fiscal_year_start_month, bool) or not isinstance(
            self.fiscal_year_start_month, int
        ):
            raise TypeError("fiscal_year_start_month must be an integer.")
        if not 1 <= self.fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month must be between 1 and 12.")
        object.__setattr__(self, "weekend_adjustment", _coerce_convention(self.weekend_adjustment))
        object.__setattr__(self, "holiday_adjustment", _coerce_convention(self.holiday_adjustment))
        holidays = frozenset(self.holidays)
        for day in holidays:
            _validate_calendar_date(day, "holidays")
        object.__setattr__(self, "holidays", holidays)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FiscalCalendar:
        """Build a calendar from the seed-data shape: `fiscal_year_start_month`,
        `weekend_adjustment`, `holiday_adjustment` and a `holidays` list of
        `{"date": "YYYY-MM-DD", "name": ...}` rows (`calendar.json`)."""
        if not isinstance(value, Mapping):
            raise TypeError("A fiscal calendar mapping is required.")
        holidays_raw = value.get("holidays", ())
        if not isinstance(holidays_raw, Sequence) or isinstance(holidays_raw, str | bytes):
            raise TypeError("calendar.holidays must be a list of holiday rows.")
        holidays: list[date] = []
        for row in holidays_raw:
            raw_date = row.get("date") if isinstance(row, Mapping) else getattr(row, "date", None)
            if not isinstance(raw_date, str) or not raw_date.strip():
                raise TypeError("Every calendar holiday row needs a non-empty 'date' field.")
            holidays.append(date.fromisoformat(raw_date.strip()))
        return cls(
            fiscal_year_start_month=value.get("fiscal_year_start_month"),  # type: ignore[arg-type]
            weekend_adjustment=value.get(  # type: ignore[arg-type]
                "weekend_adjustment", AdjustmentConvention.NEXT_BUSINESS_DAY
            ),
            holiday_adjustment=value.get(  # type: ignore[arg-type]
                "holiday_adjustment", AdjustmentConvention.NEXT_BUSINESS_DAY
            ),
            holidays=frozenset(holidays),
        )


@dataclass(frozen=True, slots=True)
class ScheduleOccurrence:
    """One generated testing-calendar occurrence for a covenant version.

    ``period_end`` is the raw fiscal boundary the frequency and FY calendar
    produce; ``due_date`` is that boundary after weekend/holiday adjustment —
    the value actually persisted to `covenant_schedule.due_date`.
    ``adjustment_reason`` names why the two differ, or is `None` when they
    are the same date.
    """

    period_end: date
    due_date: date
    adjustment_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_calendar_date(self.period_end, "period_end")
        _validate_calendar_date(self.due_date, "due_date")
        if self.adjustment_reason is not None and not self.adjustment_reason.strip():
            raise ValueError("adjustment_reason must be non-empty text or None.")


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _end_months(frequency: str, fiscal_year_start_month: int) -> tuple[int, ...]:
    offsets = _FREQUENCY_MONTH_OFFSETS.get(frequency)
    if offsets is None:
        raise ValueError(f"Unsupported testing frequency {frequency!r} for calendar generation.")
    months = {((fiscal_year_start_month - 1 + offset) % 12) + 1 for offset in offsets}
    return tuple(sorted(months))


def period_end_dates(
    frequency: str,
    fiscal_year_start_month: int,
    *,
    start: date,
    end: date,
) -> tuple[date, ...]:
    """Return every fiscal period-end boundary in the half-open range
    ``[start, end)`` for ``frequency``, aligned to ``fiscal_year_start_month``.

    A `monthly` covenant tests every calendar month-end; `quarterly`,
    `half_yearly` and `annual` test the fiscal quarter-, half- and year-end
    that follows from where the fiscal year itself starts — for a fiscal
    year starting in April, that is June/September/December/March,
    September/March, and March respectively. `on_event` has no boundaries
    at all and is refused here rather than silently returning nothing, so a
    caller cannot mistake "no calendar" for "no boundaries found yet".
    """
    if frequency == "on_event":
        raise ValueError("on_event covenants have no calendar boundaries to generate.")
    _validate_calendar_date(start, "start")
    _validate_calendar_date(end, "end")
    if end <= start:
        return ()
    end_months = _end_months(frequency, fiscal_year_start_month)
    boundaries: set[date] = set()
    for year in range(start.year - 1, end.year + 2):
        for month in end_months:
            candidate = _last_day_of_month(year, month)
            if start <= candidate < end:
                boundaries.add(candidate)
    return tuple(sorted(boundaries))


def first_due_date(
    effective_from: date,
    frequency: str,
    fiscal_year_start_month: int,
) -> date | None:
    """Return the first testing-calendar boundary on or after
    ``effective_from`` — the documented rule for a covenant that becomes
    effective mid-period.

    The naive assumption — "first due date is one frequency interval after
    effective_from" — is wrong for a covenant effective mid-quarter: it
    would skip past the boundary that actually closes the covenant's first
    (partial) testing period. The correct first due date is simply the
    nearest fiscal boundary on or after the effective date; every
    subsequent one follows the same calendar at the ordinary interval.
    Returns `None` for `on_event`, which has no calendar at all.
    """
    _validate_calendar_date(effective_from, "effective_from")
    if frequency == "on_event":
        return None
    horizon = effective_from + timedelta(days=_FIRST_DUE_DATE_SEARCH_DAYS)
    boundaries = period_end_dates(
        frequency, fiscal_year_start_month, start=effective_from, end=horizon
    )
    if not boundaries:
        raise RuntimeError(
            f"No testing-calendar boundary was found for frequency {frequency!r} within "
            f"{_FIRST_DUE_DATE_SEARCH_DAYS} days of {effective_from}; this indicates a defect "
            "in the frequency table, not a data condition."
        )
    return boundaries[0]


def is_business_day(day: date, calendar: FiscalCalendar) -> bool:
    """Whether ``day`` is neither a weekend day nor a configured holiday."""
    _validate_calendar_date(day, "day")
    if not isinstance(calendar, FiscalCalendar):
        raise TypeError("calendar must be a FiscalCalendar.")
    if day.weekday() in _WEEKEND_WEEKDAYS:
        return False
    return day not in calendar.holidays


def adjust_due_date(raw_due_date: date, calendar: FiscalCalendar) -> tuple[date, str | None]:
    """Adjust one raw fiscal boundary for weekends and holidays.

    The convention applied is chosen by *why* the raw date is not a
    business day — `calendar.holiday_adjustment` when it is a configured
    holiday, `calendar.weekend_adjustment` when it is only a weekend day —
    fixed for the whole walk to the next/previous business day so the two
    conventions can never fight each other into an infinite adjustment.
    Returns the unchanged date and `None` when no adjustment is needed, or
    when the applicable convention is `NONE`.
    """
    _validate_calendar_date(raw_due_date, "raw_due_date")
    if is_business_day(raw_due_date, calendar):
        return raw_due_date, None
    is_holiday = raw_due_date in calendar.holidays
    convention = calendar.holiday_adjustment if is_holiday else calendar.weekend_adjustment
    if convention is AdjustmentConvention.NONE:
        return raw_due_date, None
    step = timedelta(days=1 if convention is AdjustmentConvention.NEXT_BUSINESS_DAY else -1)
    adjusted = raw_due_date + step
    while not is_business_day(adjusted, calendar):
        adjusted += step
    reason_kind = "a holiday" if is_holiday else "a weekend"
    direction = "next" if convention is AdjustmentConvention.NEXT_BUSINESS_DAY else "previous"
    reason = (
        f"{raw_due_date.isoformat()} fell on {reason_kind}; adjusted to the {direction} "
        f"business day {adjusted.isoformat()} per the {convention.value} convention."
    )
    return adjusted, reason


def generate_schedule_occurrences(
    *,
    effective_from: date,
    effective_to: date | None,
    frequency: str,
    calendar: FiscalCalendar,
    horizon_end: date,
) -> tuple[ScheduleOccurrence, ...]:
    """Generate every testing-calendar occurrence due in
    ``[effective_from, min(effective_to, horizon_end))``.

    Returns an empty tuple for `on_event` — that frequency is tested only
    on arrival and carries no calendar entries at all — and whenever the
    window is empty or inverted.
    """
    _validate_calendar_date(effective_from, "effective_from")
    _validate_calendar_date(horizon_end, "horizon_end")
    if effective_to is not None:
        _validate_calendar_date(effective_to, "effective_to")
    if not isinstance(frequency, str) or frequency not in _VALID_FREQUENCIES:
        raise ValueError(f"Unknown testing frequency {frequency!r}.")
    if not isinstance(calendar, FiscalCalendar):
        raise TypeError("calendar must be a FiscalCalendar.")
    if frequency == "on_event":
        return ()
    window_end = horizon_end if effective_to is None else min(effective_to, horizon_end)
    boundaries = period_end_dates(
        frequency, calendar.fiscal_year_start_month, start=effective_from, end=window_end
    )
    occurrences: list[ScheduleOccurrence] = []
    for boundary in boundaries:
        due_date, reason = adjust_due_date(boundary, calendar)
        occurrences.append(
            ScheduleOccurrence(period_end=boundary, due_date=due_date, adjustment_reason=reason)
        )
    return tuple(occurrences)


def _schedule_state(schedule: object) -> str:
    value = (
        schedule.get("state") if isinstance(schedule, Mapping) else getattr(schedule, "state", None)
    )
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A schedule row must carry a non-empty state.")
    return value.strip().lower()


def _schedule_due_date(schedule: object) -> date:
    value = (
        schedule.get("due_date")
        if isinstance(schedule, Mapping)
        else getattr(schedule, "due_date", None)
    )
    _validate_calendar_date(value, "schedule.due_date")
    return value  # type: ignore[return-value]


def not_applicable_state(schedule: object, *, cutoff: date) -> str | None:
    """Return `ScheduleState.NOT_APPLICABLE` when a covenant retired as of
    ``cutoff`` should retire this still-open row too, or `None` when the
    row's state should be left unchanged.

    Only `due` and `missed` rows ever move: a `tested` row is a settled
    fact retirement cannot erase, and a row already `not_applicable` does
    not need to be marked twice. The row is never deleted — the caller
    persists the returned state onto the same row, keeping every historical
    occurrence visible.
    """
    _validate_calendar_date(cutoff, "cutoff")
    state = _schedule_state(schedule)
    due_date = _schedule_due_date(schedule)
    if state in _OPEN_SCHEDULE_STATES and due_date >= cutoff:
        return ScheduleState.NOT_APPLICABLE.value
    return None


def is_missed(schedule: object, *, as_of: date) -> bool:
    """Whether a still-`due` schedule row's due date has passed ``as_of``
    with no test recorded against it yet."""
    _validate_calendar_date(as_of, "as_of")
    state = _schedule_state(schedule)
    due_date = _schedule_due_date(schedule)
    return state == ScheduleState.DUE.value and due_date < as_of


@dataclass(frozen=True, slots=True)
class RetestTrigger:
    """One data change that may require one or more live covenant versions
    to be retested — `spec §R-08`'s "tested again whenever the data they
    depend on changes" rule, named once so `EngineService.queue_retest` can
    resolve it to the covenant versions it affects without re-deriving the
    trigger vocabulary at the call site.

    Exactly one scope field is populated, matching ``kind``:

    * ``statement``/``restatement`` — ``borrower_id``: any ratio formula on
      any live covenant across the borrower's current facilities may read
      the changed period, so every one of them is affected.
    * ``conduct`` — ``facility_id``: only covenant versions whose ratio
      definition reads a facility-conduct fact (`utilisation`,
      `drawing_power_headroom`) on that one facility depend on it.
    * ``waiver`` — ``covenant_id``: a waiver attaches to the stable
      covenant identity, not to one version.
    * ``exception`` — ``covenant_version_id``: an exception attaches to
      exactly one version.
    """

    kind: RetestTriggerKind
    as_of_date: date
    borrower_id: UUID | None = None
    facility_id: UUID | None = None
    covenant_id: UUID | None = None
    covenant_version_id: UUID | None = None
    period_label: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RetestTriggerKind):
            raise TypeError("kind must be a RetestTriggerKind.")
        _validate_calendar_date(self.as_of_date, "as_of_date")
        for name in ("borrower_id", "facility_id", "covenant_id", "covenant_version_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, UUID):
                raise TypeError(f"{name} must be a UUID or None.")
        if self.period_label is not None and not self.period_label.strip():
            raise ValueError("period_label must be non-empty text or None.")
        required_field = _TRIGGER_SCOPE_FIELDS[self.kind]
        if getattr(self, required_field) is None:
            raise ValueError(f"A {self.kind.value} retest trigger requires {required_field}.")


__all__ = [
    "AdjustmentConvention",
    "FiscalCalendar",
    "RetestTrigger",
    "RetestTriggerKind",
    "ScheduleOccurrence",
    "ScheduleState",
    "adjust_due_date",
    "first_due_date",
    "generate_schedule_occurrences",
    "is_business_day",
    "is_missed",
    "not_applicable_state",
    "period_end_dates",
]
