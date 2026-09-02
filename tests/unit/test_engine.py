"""Exact decision-table coverage for the T-034 covenant engine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from covenant_radar.domain.covenants.evaluate import (
    CovenantVerdict,
    CovenantVersionFacts,
    PeriodFacts,
    evaluate_covenant,
)
from covenant_radar.domain.covenants.exceptions import ExceptionFacts, WaiverFacts
from covenant_radar.domain.ratios.compute import RatioResult
from covenant_radar.domain.ratios.reasons import NotComputableReason


def _version(**overrides: object) -> CovenantVersionFacts:
    values: dict[str, object] = {
        "threshold": Decimal("2.5"),
        "direction": "max",
    }
    values.update(overrides)
    return CovenantVersionFacts(**values)


def _ratio(value: Decimal | None, *, computable: bool = True) -> RatioResult:
    if computable:
        return RatioResult(
            code="leverage_ratio",
            value=value,
            computable=True,
            reason=None,
            inputs_used={"total_debt": Decimal("500")},
            band_breached=False,
        )
    return RatioResult(
        code="leverage_ratio",
        value=None,
        computable=False,
        reason=NotComputableReason.MISSING_LINE,
        inputs_used={"total_debt": Decimal("500")},
        band_breached=False,
        reason_context={"names": "tangible_net_worth"},
    )


def _period(**overrides: object) -> PeriodFacts:
    values: dict[str, object] = {
        "period_label": "FY27Q2",
        "is_complete": True,
        "as_of_date": date(2026, 6, 30),
    }
    values.update(overrides)
    return PeriodFacts(**values)


@pytest.mark.parametrize(
    ("direction", "value", "expected_headroom", "expected_verdict"),
    (
        ("max", Decimal("2.0"), Decimal("20"), "pass"),
        ("max", Decimal("2.4"), Decimal("4"), "warning"),
        ("max", Decimal("3.0"), Decimal("-20"), "breach"),
        ("min", Decimal("3.0"), Decimal("20"), "pass"),
        ("min", Decimal("2.6"), Decimal("4"), "warning"),
        ("min", Decimal("2.0"), Decimal("-20"), "breach"),
        ("max", Decimal("2.5"), Decimal("0"), "breach"),
        ("min", Decimal("2.5"), Decimal("0"), "breach"),
        ("max", Decimal("2.49"), Decimal("0.4"), "warning"),
        ("min", Decimal("2.51"), Decimal("0.4"), "warning"),
        ("max", Decimal("1.25"), Decimal("50"), "pass"),
        ("min", Decimal("5.0"), Decimal("100"), "pass"),
    ),
)
def test_twelve_hand_worked_cases_exact(
    direction: str,
    value: Decimal,
    expected_headroom: Decimal,
    expected_verdict: str,
) -> None:
    result = evaluate_covenant(
        _version(direction=direction, warning_headroom_pct=Decimal("10")),
        _ratio(value),
        _period(),
        None,
        None,
        {},
    )

    assert result.value == value
    assert result.threshold_used == Decimal("2.5")
    assert result.headroom_pct == expected_headroom
    assert result.verdict == expected_verdict


@pytest.mark.parametrize("direction", ("min", "max"))
def test_value_at_threshold_boundary(direction: str) -> None:
    result = evaluate_covenant(
        _version(direction=direction),
        _ratio(Decimal("2.5")),
        _period(),
        None,
        None,
        {},
    )

    assert result.headroom_pct == Decimal("0")
    assert result.verdict == CovenantVerdict.BREACH.value
    assert result.thresholds_compared[0] == {
        "name": "covenant_threshold",
        "value": Decimal("2.5"),
        "observed": Decimal("2.5"),
        "side": "at",
    }


def test_not_computable_reason_carried() -> None:
    result = evaluate_covenant(
        _version(), _ratio(None, computable=False), _period(), None, None, {}
    )

    assert result.verdict == CovenantVerdict.NOT_COMPUTABLE.value
    assert result.value is None
    assert result.headroom_pct is None
    assert result.reason is NotComputableReason.MISSING_LINE
    assert result.reason_context == {"names": "tangible_net_worth"}
    assert result.thresholds_compared == ()


def test_incomplete_period_marks_stale_naming_last() -> None:
    result = evaluate_covenant(
        _version(),
        _ratio(Decimal("2.0")),
        _period(is_complete=False, last_complete_period="FY27Q1"),
        None,
        None,
        {},
    )

    assert result.verdict == CovenantVerdict.STALE.value
    assert result.value is None
    assert result.headroom_pct is None
    assert result.stale_reason == "last complete period: FY27Q1"


def test_ratio_incomplete_reason_also_marks_stale() -> None:
    incomplete_ratio = RatioResult(
        code="leverage_ratio",
        value=None,
        computable=False,
        reason=NotComputableReason.PERIOD_INCOMPLETE,
        inputs_used={},
        band_breached=False,
    )

    result = evaluate_covenant(
        _version(), incomplete_ratio, _period(last_complete_period="FY27Q1"), None, None, {}
    )

    assert result.verdict == CovenantVerdict.STALE.value
    assert result.stale_reason == "last complete period: FY27Q1"


def test_exception_threshold_used_and_named() -> None:
    exception = ExceptionFacts(
        from_period="FY27Q2",
        to_period="FY27Q3",
        relaxed_threshold=Decimal("3.0"),
        id=uuid4(),
    )
    result = evaluate_covenant(_version(), _ratio(Decimal("2.7")), _period(), exception, None, {})

    assert result.threshold_used == Decimal("3.0")
    assert result.exception_applied == exception
    assert result.headroom_pct == Decimal("10")
    assert result.verdict == CovenantVerdict.PASS.value


def test_waiver_recorded() -> None:
    waiver = WaiverFacts(
        from_date=date(2026, 6, 1),
        to_date=date(2026, 6, 30),
        id=uuid4(),
        state="approved",
        approved_by_id=uuid4(),
    )
    result = evaluate_covenant(_version(), _ratio(Decimal("3.0")), _period(), None, waiver, {})

    assert result.waiver_applied == waiver
    assert result.verdict == CovenantVerdict.PASS.value
    assert result.threshold_used == Decimal("2.5")
    assert result.headroom_pct == Decimal("-20")


def test_warning_band_between_warning_and_threshold() -> None:
    result = evaluate_covenant(
        _version(warning_headroom_pct=Decimal("10")),
        _ratio(Decimal("2.4")),
        _period(),
        None,
        None,
        {},
    )

    assert result.verdict == CovenantVerdict.WARNING.value
    assert result.thresholds_compared[-1] == {
        "name": "warning_threshold",
        "value": Decimal("2.25"),
        "observed": Decimal("2.4"),
        "side": "above",
    }


def test_thresholds_compared_carries_side() -> None:
    min_result = evaluate_covenant(
        _version(direction="min"), _ratio(Decimal("2.0")), _period(), None, None, {}
    )
    max_result = evaluate_covenant(
        _version(direction="max"), _ratio(Decimal("3.0")), _period(), None, None, {}
    )

    assert min_result.thresholds_compared[0]["side"] == "below"
    assert max_result.thresholds_compared[0]["side"] == "above"


def test_breach_with_cure_has_inclusive_window_end() -> None:
    result = evaluate_covenant(
        _version(cure_days=30),
        _ratio(Decimal("3.0")),
        _period(as_of_date=date(2026, 6, 30)),
        None,
        None,
        {},
    )

    assert result.verdict == CovenantVerdict.BREACH_CURE_OPEN.value
    assert result.cure_ends_on == date(2026, 7, 30)


def test_unapproved_waiver_is_not_applied() -> None:
    waiver = WaiverFacts(from_date=date(2026, 6, 1), to_date=date(2026, 6, 30))
    result = evaluate_covenant(_version(), _ratio(Decimal("3.0")), _period(), None, waiver, {})

    assert result.waiver_applied is None
    assert result.verdict == CovenantVerdict.BREACH.value


def test_evaluator_does_not_raise_for_ratio_arithmetic_failure() -> None:
    malformed_ratio = SimpleNamespace(computable=True, value=Decimal("NaN"))
    result = evaluate_covenant(_version(), malformed_ratio, _period(), None, None, {})

    assert result.verdict == CovenantVerdict.NOT_COMPUTABLE.value
    assert result.reason is NotComputableReason.FORMULA_NOT_COMPUTABLE
