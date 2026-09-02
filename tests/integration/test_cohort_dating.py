"""Reference-cohort validation for forecast crossing dates (T-053)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from covenant_radar.domain.forecast import Observation, first_crossing, project
from evaluation.reference_portfolio import ReferencePortfolioConfig, generate_reference_portfolio
from evaluation.reference_portfolio.cohorts import (
    STABLE_COHORT,
    ReferenceCohorts,
    generate_reference_cohorts,
)

pytestmark = pytest.mark.integration


def _dataset() -> ReferenceCohorts:
    portfolio = generate_reference_portfolio(
        ReferencePortfolioConfig(seed=17, borrower_count=24, facility_count=57, quarter_count=4)
    )
    return generate_reference_cohorts(portfolio)


def _utilisation_observations(
    dataset: ReferenceCohorts,
    borrower_id: UUID,
    as_of: date,
) -> tuple[Observation, ...]:
    return tuple(
        Observation(
            observed_on=event.event_date,
            value=event.magnitude,
            source_id=str(event.id),
        )
        for event in dataset.signals.events_for_borrower(borrower_id, family="utilisation")
        if event.event_date <= as_of
    )


def test_deteriorating_cohort_within_ten_days_of_labels() -> None:
    dataset = _dataset()
    as_of = dataset.signal_start_date + timedelta(days=30)
    horizon_days = (dataset.signal_end_date - as_of).days

    for label in dataset.labels:
        observations = _utilisation_observations(dataset, label.borrower_id, as_of)
        projection = project(
            observations,
            pressure=Decimal("0"),
            horizon_days=horizon_days,
            threshold=label.threshold,
            direction=label.direction,
        )
        result = first_crossing(projection, as_of_date=as_of)

        assert result.crossing_date is not None
        assert abs((result.crossing_date - label.breach_date).days) <= 10
        assert result.crossing_day is not None
        assert result.crossing_value is not None
        assert result.crossing_value >= label.threshold


def test_stable_cohort_never_crosses() -> None:
    dataset = _dataset()
    as_of = dataset.signal_start_date + timedelta(days=30)
    horizon_days = (dataset.signal_end_date - as_of).days
    stable_assignments = tuple(
        assignment for assignment in dataset.assignments if assignment.cohort == STABLE_COHORT
    )

    assert stable_assignments
    for assignment in stable_assignments:
        projection = project(
            _utilisation_observations(dataset, assignment.borrower_id, as_of),
            pressure=Decimal("0"),
            horizon_days=horizon_days,
            threshold=Decimal("85"),
            direction="max",
        )
        result = first_crossing(projection, as_of_date=as_of)

        assert result.crossing_day is None
        assert result.direction.value == "max"
        assert result.reason is not None
