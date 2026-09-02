"""Property checks for T-059's total and reproducible ordering."""

from __future__ import annotations

from decimal import Decimal
from itertools import permutations
from uuid import uuid4

import pytest

from covenant_radar.domain.triage import ForecastFact, TriageInput, TriageThresholds, rank

pytestmark = pytest.mark.property

_THRESHOLDS = TriageThresholds(
    act=Decimal("0.70"),
    amber=Decimal("0.40"),
    confidence_floor=Decimal("0.50"),
)


def _inputs() -> tuple[TriageInput, ...]:
    facts = (
        ("B-000001", "0.70", "100"),
        ("B-000002", "0.40", "200"),
        ("B-000003", "0.25", "300"),
        ("B-000004", "0.80", "400"),
        ("B-000005", None, "500"),
    )
    return [
        TriageInput(
            borrower_id=uuid4(),
            reference=reference,
            exposure=Decimal(exposure),
            forecasts=(
                ForecastFact(
                    covenant_version_id=uuid4(),
                    horizon_days=30,
                    probability=probability,
                    confidence=Decimal("0.80"),
                ),
            )
            if probability is not None
            else (),
        )
        for reference, probability, exposure in facts
    ]


def test_ordering_is_total() -> None:
    entries = tuple(_inputs())

    for permutation in permutations(entries):
        ranked = rank(permutation, _THRESHOLDS)
        assert len(ranked) == len(entries)
        assert [entry.rank for entry in ranked] == list(range(1, len(entries) + 1))
        assert len({entry.reference for entry in ranked}) == len(ranked)


def test_two_runs_identical_order() -> None:
    entries = _inputs()
    first = rank(entries, _THRESHOLDS)
    second = rank(entries, _THRESHOLDS)

    assert [entry.borrower_id for entry in first] == [entry.borrower_id for entry in second]
    assert [entry.reference for entry in first] == [entry.reference for entry in second]
