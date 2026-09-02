"""Unit tests for exception, waiver and cure rules (`T-032`)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.covenants.cure import (
    CureState,
    cure_state,
    validate_cure_period,
)
from covenant_radar.domain.covenants.exceptions import (
    ExceptionFacts,
    exception_windows_overlap,
    resolve_exception,
    validate_no_overlapping_exceptions,
)


def test_boundary_periods_inside_window() -> None:
    exception = ExceptionFacts(
        from_period="FY27Q2",
        to_period="FY27Q3",
        relaxed_threshold=Decimal("3.0"),
    )
    version = SimpleNamespace(id=uuid4(), exceptions=(exception,))

    assert resolve_exception(version, "FY27Q2") == exception
    assert resolve_exception(version, "FY27Q3") == exception
    assert resolve_exception(version, "FY27Q1") is None
    assert resolve_exception(version, "FY27Q4") is None


def test_overlapping_exceptions_refused() -> None:
    current = ExceptionFacts("FY27Q2", "FY27Q3", Decimal("3.0"))

    assert exception_windows_overlap("FY27Q3", "FY27Q4", current.from_period, current.to_period)
    with pytest.raises(ValueError, match="overlaps"):
        validate_no_overlapping_exceptions("FY27Q3", "FY27Q4", (current,))


def test_cure_open_then_confirmed_without_retest() -> None:
    test = SimpleNamespace(
        verdict="breach",
        as_of_date=date(2026, 1, 1),
        cure_ends_on=date(2026, 1, 31),
    )

    open_state = cure_state(test, (), {"as_of_date": date(2026, 1, 31)})
    confirmed_state = cure_state(test, (), {"as_of_date": date(2026, 2, 1)})

    assert open_state.state is CureState.OPEN
    assert open_state.cure_ends_on == date(2026, 1, 31)
    assert confirmed_state.state is CureState.CONFIRMED
    assert confirmed_state.verdict == "breach_confirmed"
    assert confirmed_state.cure_ends_on == date(2026, 1, 31)


def test_passing_retest_cures_and_keeps_both_states() -> None:
    test = SimpleNamespace(
        verdict="breach",
        as_of_date=date(2026, 1, 1),
        cure_ends_on=date(2026, 1, 31),
    )
    passing_retest = SimpleNamespace(verdict="pass", as_of_date=date(2026, 1, 15))

    result = cure_state(test, (passing_retest,), {"as_of_date": date(2026, 1, 20)})

    assert result.state is CureState.CURED
    assert result.cure_ends_on == date(2026, 1, 31)
    assert result.retest is passing_retest
    assert test.verdict == "breach"


def test_cure_window_shorter_than_frequency_refused() -> None:
    with pytest.raises(ValidationError, match="frequency is longer"):
        validate_cure_period("quarterly", 30)
