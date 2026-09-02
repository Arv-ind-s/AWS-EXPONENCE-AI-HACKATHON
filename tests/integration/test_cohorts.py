"""Integration checks for the labelled reference-portfolio cohorts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.reference_portfolio import ReferencePortfolioConfig, generate_reference_portfolio
from evaluation.reference_portfolio.cohorts import (
    DETERIORATING_COHORT,
    NOISY_TRANSIENT_COHORT,
    STABLE_COHORT,
    TEMPLATED_REMAINDER_COHORT,
    generate_reference_cohorts,
)
from evaluation.reference_portfolio.labels import (
    LabelVerificationError,
    OutcomeLabels,
    verify_labels,
)
from evaluation.reference_portfolio.signals import (
    SIGNAL_FAMILIES,
    persistence_summary,
)

pytestmark = pytest.mark.integration


def _dataset():
    portfolio = generate_reference_portfolio(
        ReferencePortfolioConfig(seed=17, borrower_count=24, facility_count=57, quarter_count=4)
    )
    return generate_reference_cohorts(portfolio)


def test_cohort_counts_and_assignment() -> None:
    dataset = _dataset()

    assert set(dataset.cohort_counts) == {
        DETERIORATING_COHORT,
        NOISY_TRANSIENT_COHORT,
        STABLE_COHORT,
        TEMPLATED_REMAINDER_COHORT,
    }
    assert all(
        dataset.assignment_for(borrower.id).borrower_id == borrower.id
        for borrower in dataset.borrowers
    )
    assert all(
        borrower.cohort == dataset.assignment_for(borrower.id).cohort
        for borrower in dataset.borrowers
    )


def test_deteriorating_trajectory_reaches_labelled_date() -> None:
    dataset = _dataset()

    for label in dataset.labels:
        utilisation = tuple(
            event
            for event in dataset.events_for_borrower(label.borrower_id)
            if event.family == "utilisation"
        )
        crossing = next(event for event in utilisation if event.event_date == label.breach_date)
        assert crossing.magnitude >= label.threshold
        assert all(
            event.magnitude < label.threshold
            for event in utilisation
            if event.event_date < label.breach_date
        )
        assert crossing.id == label.source_event_id
        assert crossing.content_hash == label.source_event_hash


def test_noisy_cohort_never_meets_persistence_rule() -> None:
    dataset = _dataset()

    for assignment in dataset.assignments:
        if assignment.cohort != NOISY_TRANSIENT_COHORT:
            continue
        summaries = persistence_summary(tuple(dataset.events_for_borrower(assignment.borrower_id)))
        assert all(not summary.sustained for summary in summaries)
        assert all(summary.consecutive_days < 14 for summary in summaries)
        assert all(summary.events_in_window < 3 for summary in summaries)


def test_stable_cohort_stays_in_band() -> None:
    dataset = _dataset()

    for assignment in dataset.assignments:
        if assignment.cohort != STABLE_COHORT:
            continue
        assert all(
            not event.is_adverse for event in dataset.events_for_borrower(assignment.borrower_id)
        )


def test_all_seven_signal_families_present() -> None:
    dataset = _dataset()

    for assignment in dataset.assignments:
        events = tuple(dataset.events_for_borrower(assignment.borrower_id))
        assert len(events) == 365 * len(SIGNAL_FAMILIES)
        assert {event.family for event in events} == set(SIGNAL_FAMILIES)
        event_dates = {event.event_date for event in events}
        assert len(event_dates) == 365
        assert min(event_dates) == dataset.signal_start_date
        assert max(event_dates) == dataset.signal_end_date


def test_labels_derived_not_asserted() -> None:
    dataset = _dataset()
    original = dataset.labels.rows[0]
    tampered = replace(original, breach_date=original.breach_date.replace(day=1))
    labels = OutcomeLabels(rows=(tampered, *dataset.labels.rows[1:]))

    with pytest.raises(LabelVerificationError, match="disagrees"):
        verify_labels(labels, dataset.signals, dataset.cohort_by_borrower)


def test_deterministic_with_generator_seed() -> None:
    first = _dataset()
    second = _dataset()

    assert first.content_hashes() == second.content_hashes()
    assert first.content_hash == second.content_hash
