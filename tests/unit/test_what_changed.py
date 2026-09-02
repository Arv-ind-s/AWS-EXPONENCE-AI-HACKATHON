"""Unit coverage for deterministic triage-run comparison (T-060)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_radar.domain.triage.changes import (
    BAND_WORSENED,
    FIRST_RUN,
    NEWLY_MONITORED,
    NEWLY_UNMONITORED,
    NO_CHANGE,
    ChangeThresholds,
    compute_what_changed,
)
from covenant_radar.domain.triage.urgency import TriageEntry

pytestmark = pytest.mark.unit

_THRESHOLDS = ChangeThresholds(
    reporting_threshold=Decimal("0.05"),
    dominant_driver_share=Decimal("0.50"),
)


def _entry(
    reference: str,
    *,
    band: str = "watch",
    probability: str | None = "0.20",
    drivers: dict[str, str] | None = None,
) -> TriageEntry:
    probability_value = None if probability is None else Decimal(probability)
    why: dict[str, object] = {}
    if drivers is not None:
        why["drivers"] = {name: Decimal(share) for name, share in drivers.items()}
    return TriageEntry(
        borrower_id=uuid4(),
        reference=reference,
        exposure=Decimal("100"),
        worst_covenant_version_id=uuid4() if probability_value is not None else None,
        worst_horizon=30 if probability_value is not None else None,
        probability=probability_value,
        confidence=Decimal("0.80") if probability_value is not None else None,
        urgency=Decimal("1") if probability_value is not None else None,
        band=band,
        sma_band=None,
        state="available" if probability_value is not None else "no_forecast",
        reason="test entry",
        rank=1,
        tie_break_rule="test ordering",
        why=why,
    )


def _same_borrower(current: TriageEntry, **changes: object) -> TriageEntry:
    from dataclasses import replace

    return replace(current, **changes)


def test_first_run_state_is_distinct_from_no_change() -> None:
    current = _entry("B-000001")

    result = compute_what_changed([current], None, _THRESHOLDS)

    assert result[current.borrower_id].kind is FIRST_RUN
    assert result[current.borrower_id].kind is not NO_CHANGE
    assert "first run" in result[current.borrower_id].summary


def test_band_worsened_named() -> None:
    previous = _entry("B-000001", band="watch", probability="0.20")
    current = _same_borrower(
        previous,
        band="amber",
        probability=Decimal("0.45"),
    )

    result = compute_what_changed([current], [previous], _THRESHOLDS)

    change = result[current.borrower_id]
    assert change.kind is BAND_WORSENED
    assert "watch" in change.summary
    assert "amber" in change.summary


def test_new_borrower_marked_newly_monitored() -> None:
    current = _entry("B-000002")

    result = compute_what_changed([current], [], _THRESHOLDS)

    assert result[current.borrower_id].kind is NEWLY_MONITORED
    assert "newly monitored" in result[current.borrower_id].summary


def test_disappeared_borrower_surfaced() -> None:
    previous = _entry("B-000003")

    result = compute_what_changed([], [previous], _THRESHOLDS)

    change = result[previous.borrower_id]
    assert change.kind is NEWLY_UNMONITORED
    assert change.is_disappearance is True
    assert "absent" in change.summary


def test_movement_below_reporting_threshold_is_no_change() -> None:
    previous = _entry("B-000004", probability="0.50")
    current = _same_borrower(previous, probability=Decimal("0.52"))

    result = compute_what_changed([current], [previous], _THRESHOLDS)

    change = result[current.borrower_id]
    assert change.kind is NO_CHANGE
    assert change.probability_delta == Decimal("0.02")
    assert "0.05" in change.summary


def test_dominant_driver_named_only_when_dominant() -> None:
    previous_dominant = _entry("B-000005", probability="0.50")
    current_dominant = _same_borrower(
        previous_dominant,
        probability=Decimal("0.70"),
        why={"drivers": {"cash flow pressure": Decimal("0.51")}},
    )
    previous_balanced = _entry("B-000006", probability="0.50")
    current_balanced = _same_borrower(
        previous_balanced,
        probability=Decimal("0.70"),
        why={"drivers": {"cash flow pressure": Decimal("0.50")}},
    )

    result = compute_what_changed(
        [current_dominant, current_balanced],
        [previous_dominant, previous_balanced],
        _THRESHOLDS,
    )

    assert result[current_dominant.borrower_id].dominant_driver == "cash flow pressure"
    assert "cash flow pressure" in result[current_dominant.borrower_id].summary
    assert result[current_balanced.borrower_id].dominant_driver is None
    assert "cash flow pressure" not in result[current_balanced.borrower_id].summary
