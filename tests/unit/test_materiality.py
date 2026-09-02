"""Unit coverage for the T4 materiality scorer."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.signals.materiality import (
    CovenantHeadroom,
    MaterialityThresholds,
    score_materiality,
)

pytestmark = pytest.mark.unit


class _ThresholdStore:
    def __init__(self, value: Decimal = Decimal("0.05")) -> None:
        self.value = value
        self.requested: list[str] = []

    def get(self, name: str) -> dict[str, Decimal]:
        self.requested.append(name)
        return {"headroom_erosion_pct": self.value}


def _covenant(
    covenant_id: str,
    *,
    current: str,
    projected: str,
    threshold: str = "100",
) -> CovenantHeadroom:
    return CovenantHeadroom(
        covenant_id=covenant_id,
        threshold=Decimal(threshold),
        current_headroom_pct=Decimal(current),
        projected_headroom_pct=Decimal(projected),
    )


def test_exactly_five_percent_counts() -> None:
    result = score_materiality(
        [_covenant("leverage", current="10", projected="5")],
        _ThresholdStore(),
    )

    assert result.materiality_pct == Decimal("5")
    assert result.materiality == Decimal("0.05")
    assert result.counts_toward_pressure is True
    assert result.driving_covenant_id == "leverage"


def test_below_threshold_visible_but_excluded_with_reason() -> None:
    result = score_materiality(
        [_covenant("leverage", current="10", projected="6")],
        _ThresholdStore(),
    )

    assert result.materiality_pct == Decimal("4")
    assert result.counts_toward_pressure is False
    assert result.driving_covenant_id == "leverage"
    assert "below T4 threshold" in result.reason
    assert result.covenant_scores[0].included is True


def test_no_affected_covenant_zero_with_reason() -> None:
    result = score_materiality([], _ThresholdStore())

    assert result.materiality_pct == Decimal("0")
    assert result.materiality == Decimal("0")
    assert result.counts_toward_pressure is False
    assert result.driving_covenant_id is None
    assert result.reason == "no affected covenant"


def test_maximum_across_covenants_and_driver_named() -> None:
    result = score_materiality(
        [
            _covenant("liquidity", current="8", projected="4"),
            _covenant("leverage", current="20", projected="11"),
        ],
        _ThresholdStore(),
    )

    assert result.materiality_pct == Decimal("9")
    assert result.driving_covenant_id == "leverage"
    assert result.covenant_scores[0].erosion_pct == Decimal("4")
    assert result.covenant_scores[1].erosion_pct == Decimal("9")
    assert result.counts_toward_pressure is True


def test_zero_threshold_excluded_not_divided() -> None:
    result = score_materiality(
        [
            CovenantHeadroom(
                covenant_id="invalid-covenant",
                threshold=Decimal("0"),
                current_value=Decimal("10"),
                projected_value_90d=Decimal("5"),
                direction="max",
            )
        ],
        _ThresholdStore(),
    )

    assert result.materiality_pct == Decimal("0")
    assert result.counts_toward_pressure is False
    assert result.covenant_scores[0].included is False
    assert "threshold is zero" in result.reason

    missing = score_materiality(
        [
            CovenantHeadroom(
                covenant_id="missing-covenant",
                threshold=None,
                current_headroom_pct=Decimal("10"),
                projected_headroom_pct=Decimal("5"),
            )
        ],
        _ThresholdStore(),
    )

    assert missing.materiality_pct == Decimal("0")
    assert missing.covenant_scores[0].included is False
    assert "threshold is absent" in missing.reason


def test_threshold_read_from_store() -> None:
    store = _ThresholdStore(Decimal("0.10"))

    result = score_materiality(
        [_covenant("leverage", current="10", projected="5")],
        store,
    )

    assert store.requested == ["T4"]
    assert result.thresholds == MaterialityThresholds(Decimal("0.10"))
    assert result.counts_toward_pressure is False
    assert "0.10" not in result.reason
    assert "below T4 threshold 10%" in result.reason
