"""Invariant coverage for the deterministic forecast confidence model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import confidence

pytestmark = pytest.mark.property


def test_within_bounds() -> None:
    for completeness in (Decimal("0"), Decimal("0.1"), Decimal("0.5"), Decimal("1")):
        for support in (Decimal("0"), Decimal("0.1"), Decimal("0.5"), Decimal("1")):
            for staleness_days in (0, 1, 2, 30, 365):
                result = confidence(completeness, support, staleness_days)
                assert Decimal("0") <= result.confidence <= Decimal("1")


def test_monotonic_in_each_factor() -> None:
    lower_completeness = confidence(Decimal("0.2"), Decimal("0.8"), 2)
    higher_completeness = confidence(Decimal("0.8"), Decimal("0.8"), 2)
    assert higher_completeness.confidence >= lower_completeness.confidence

    lower_support = confidence(Decimal("0.8"), Decimal("0.2"), 2)
    higher_support = confidence(Decimal("0.8"), Decimal("0.8"), 2)
    assert higher_support.confidence >= lower_support.confidence

    fresh = confidence(Decimal("0.8"), Decimal("0.8"), 0)
    stale = confidence(Decimal("0.8"), Decimal("0.8"), 10)
    assert fresh.confidence >= stale.confidence
