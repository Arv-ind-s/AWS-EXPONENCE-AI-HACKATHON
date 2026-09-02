"""Unit coverage for `T-035`'s testing calendar: due-date generation,
holiday/weekend adjustment and schedule-state transitions.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from covenant_radar.domain.covenants.calendar import (
    AdjustmentConvention,
    FiscalCalendar,
    ScheduleState,
    adjust_due_date,
    first_due_date,
    generate_schedule_occurrences,
    not_applicable_state,
)

_APRIL_FY_CALENDAR = FiscalCalendar(fiscal_year_start_month=4, holidays=frozenset())


def test_first_due_date_for_mid_period_start() -> None:
    # A covenant effective 2026-02-10 falls mid fiscal-Q4 (Jan-Mar, FY
    # starting April). The naive "one interval later" rule would land on
    # 2026-05-10; the documented rule is the next fiscal quarter-end,
    # 2026-03-31, which closes the covenant's first (partial) test period.
    due = first_due_date(date(2026, 2, 10), "quarterly", fiscal_year_start_month=4)

    assert due == date(2026, 3, 31)


def test_first_due_date_on_boundary_is_that_same_day() -> None:
    due = first_due_date(date(2026, 3, 31), "quarterly", fiscal_year_start_month=4)

    assert due == date(2026, 3, 31)


def test_first_due_date_for_on_event_is_none() -> None:
    assert first_due_date(date(2026, 2, 10), "on_event", fiscal_year_start_month=4) is None


def test_holiday_adjustment_recorded() -> None:
    calendar = FiscalCalendar(
        fiscal_year_start_month=4,
        holiday_adjustment=AdjustmentConvention.NEXT_BUSINESS_DAY,
        weekend_adjustment=AdjustmentConvention.NEXT_BUSINESS_DAY,
        # 2026-03-31 is itself a Tuesday; declare it a holiday so the
        # adjustment is caused by the holiday, not the weekend.
        holidays=frozenset({date(2026, 3, 31)}),
    )

    due_date, reason = adjust_due_date(date(2026, 3, 31), calendar)

    assert due_date == date(2026, 4, 1)
    assert reason is not None
    assert "2026-03-31" in reason
    assert "holiday" in reason
    assert "2026-04-01" in reason
    assert "next_business_day" in reason


def test_holiday_adjustment_previous_business_day_convention() -> None:
    calendar = FiscalCalendar(
        fiscal_year_start_month=4,
        holiday_adjustment=AdjustmentConvention.PREVIOUS_BUSINESS_DAY,
        weekend_adjustment=AdjustmentConvention.NEXT_BUSINESS_DAY,
        holidays=frozenset({date(2026, 3, 31)}),
    )

    due_date, reason = adjust_due_date(date(2026, 3, 31), calendar)

    assert due_date == date(2026, 3, 30)
    assert reason is not None
    assert "previous_business_day" in reason


def test_weekend_due_date_adjusted_without_being_a_holiday() -> None:
    # 2026-08-30 is a Sunday.
    due_date, reason = adjust_due_date(date(2026, 8, 30), _APRIL_FY_CALENDAR)

    assert due_date == date(2026, 8, 31)
    assert reason is not None
    assert "weekend" in reason


def test_business_day_due_date_is_unchanged() -> None:
    # 2026-06-30 is a Tuesday and not a holiday.
    due_date, reason = adjust_due_date(date(2026, 6, 30), _APRIL_FY_CALENDAR)

    assert due_date == date(2026, 6, 30)
    assert reason is None


def test_retired_covenant_marks_remaining_not_applicable() -> None:
    cutoff = date(2026, 7, 1)
    due_row = SimpleNamespace(state=ScheduleState.DUE.value, due_date=date(2026, 9, 30))
    missed_row = SimpleNamespace(state=ScheduleState.MISSED.value, due_date=date(2026, 8, 15))
    tested_row = SimpleNamespace(state=ScheduleState.TESTED.value, due_date=date(2026, 9, 30))
    already_not_applicable = SimpleNamespace(
        state=ScheduleState.NOT_APPLICABLE.value, due_date=date(2026, 12, 31)
    )
    before_cutoff_due_row = SimpleNamespace(
        state=ScheduleState.DUE.value, due_date=date(2026, 6, 30)
    )

    assert not_applicable_state(due_row, cutoff=cutoff) == ScheduleState.NOT_APPLICABLE.value
    assert not_applicable_state(missed_row, cutoff=cutoff) == ScheduleState.NOT_APPLICABLE.value
    # A settled test is never erased by a later retirement.
    assert not_applicable_state(tested_row, cutoff=cutoff) is None
    # Already-retired rows are not re-marked.
    assert not_applicable_state(already_not_applicable, cutoff=cutoff) is None
    # A due date before the retirement cutoff remains due — it fell inside
    # the window the covenant was still live for.
    assert not_applicable_state(before_cutoff_due_row, cutoff=cutoff) is None


def test_on_event_frequency_has_no_calendar() -> None:
    occurrences = generate_schedule_occurrences(
        effective_from=date(2026, 1, 1),
        effective_to=None,
        frequency="on_event",
        calendar=_APRIL_FY_CALENDAR,
        horizon_end=date(2030, 1, 1),
    )

    assert occurrences == ()


def test_generate_schedule_occurrences_within_effective_window() -> None:
    occurrences = generate_schedule_occurrences(
        effective_from=date(2026, 2, 10),
        effective_to=date(2026, 10, 1),
        frequency="quarterly",
        calendar=_APRIL_FY_CALENDAR,
        horizon_end=date(2030, 1, 1),
    )

    assert [occurrence.period_end for occurrence in occurrences] == [
        date(2026, 3, 31),
        date(2026, 6, 30),
        date(2026, 9, 30),
    ]
    # Effective_to (2026-10-01) is exclusive, so the covenant's last
    # occurrence inside the window is still Sept 30, not skipped.
    assert occurrences[-1].due_date == date(2026, 9, 30)


def test_unknown_frequency_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown testing frequency"):
        generate_schedule_occurrences(
            effective_from=date(2026, 1, 1),
            effective_to=None,
            frequency="weekly",
            calendar=_APRIL_FY_CALENDAR,
            horizon_end=date(2027, 1, 1),
        )
