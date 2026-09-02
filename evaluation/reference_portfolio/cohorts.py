"""Authored cohorts and the complete deterministic evaluation dataset."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import UUID

from evaluation.reference_portfolio.generator import BorrowerRecord, ReferencePortfolio
from evaluation.reference_portfolio.labels import (
    OutcomeLabel,
    OutcomeLabels,
    derive_labels,
    verify_labels,
)
from evaluation.reference_portfolio.signals import (
    DEFAULT_SIGNAL_DAYS,
    DETERIORATING_COHORT,
    NOISY_TRANSIENT_COHORT,
    SIGNAL_FAMILIES,
    STABLE_COHORT,
    TEMPLATED_REMAINDER_COHORT,
    SignalEventRecord,
    SignalEventStream,
    generate_signal_events,
    persistence_summary,
)

DEFAULT_AUTHORED_COHORT_SIZE: Final[int] = 100
COHORT_NAMES: Final[tuple[str, ...]] = (
    DETERIORATING_COHORT,
    NOISY_TRANSIENT_COHORT,
    STABLE_COHORT,
    TEMPLATED_REMAINDER_COHORT,
)

STABLE_BANDS: Final[Mapping[str, tuple[Decimal, Decimal]]] = MappingProxyType(
    {
        "payment": (Decimal("0"), Decimal("7")),
        "account_activity": (Decimal("0"), Decimal("10")),
        "utilisation": (Decimal("0"), Decimal("70")),
        "treasury": (Decimal("0"), Decimal("0.20")),
        "concentration": (Decimal("0"), Decimal("45")),
        "industry": (Decimal("0"), Decimal("0.50")),
        "news": (Decimal("0"), Decimal("0.50")),
    }
)


class CohortGenerationError(ValueError):
    """Raised when a reference cohort invariant cannot be proven."""


@dataclass(frozen=True, slots=True)
class CohortAssignment:
    """The immutable cohort assignment recorded alongside one borrower."""

    borrower_id: UUID
    borrower_reference: str
    cohort: str
    ordinal: int

    def __post_init__(self) -> None:
        if self.cohort not in COHORT_NAMES:
            raise CohortGenerationError(f"Unknown cohort {self.cohort!r}.")
        if self.ordinal < 1:
            raise ValueError("Cohort assignment ordinal must be positive.")
        if not self.borrower_reference.strip():
            raise ValueError("borrower_reference must not be blank.")

    @property
    def cohort_name(self) -> str:
        return self.cohort

    @property
    def profile(self) -> str:
        return self.cohort


@dataclass(frozen=True, slots=True)
class BorrowerWithCohort:
    """A view of a generated borrower with its explicit cohort field."""

    borrower: BorrowerRecord
    cohort: str

    @property
    def id(self) -> UUID:
        return self.borrower.id

    @property
    def reference(self) -> str:
        return self.borrower.reference

    @property
    def legal_name(self) -> str:
        return self.borrower.legal_name

    def __getattr__(self, name: str) -> object:
        """Expose the remaining borrower fields without duplicating the model."""
        return getattr(self.borrower, name)


@dataclass(frozen=True, slots=True)
class ReferenceCohorts:
    """Reference portfolio plus assignments, lazy signals and derived labels."""

    portfolio: ReferencePortfolio
    assignments: tuple[CohortAssignment, ...]
    signals: SignalEventStream
    labels: OutcomeLabels

    @property
    def cohort_assignments(self) -> tuple[CohortAssignment, ...]:
        return self.assignments

    @property
    def assignments_by_borrower(self) -> Mapping[UUID, CohortAssignment]:
        return MappingProxyType(
            {assignment.borrower_id: assignment for assignment in self.assignments}
        )

    @property
    def events(self) -> SignalEventStream:
        return self.signals

    @property
    def labelled_outcomes(self) -> OutcomeLabels:
        return self.labels

    @property
    def outcomes(self) -> OutcomeLabels:
        return self.labels

    @property
    def labelled_outcome_rows(self) -> tuple[OutcomeLabel, ...]:
        return self.labels.rows

    @property
    def cohort_by_borrower(self) -> Mapping[UUID, str]:
        return self.signals.cohort_by_borrower

    @property
    def borrowers(self) -> tuple[BorrowerWithCohort, ...]:
        return tuple(
            BorrowerWithCohort(borrower, self.cohort_by_borrower[borrower.id])
            for borrower in self.portfolio.borrowers
        )

    @property
    def cohort_counts(self) -> Mapping[str, int]:
        return MappingProxyType(dict(Counter(assignment.cohort for assignment in self.assignments)))

    @property
    def signal_start_date(self) -> date:
        return self.signals.start_date

    @property
    def signal_end_date(self) -> date:
        return self.signals.end_date

    def assignment_for(self, borrower_id: UUID) -> CohortAssignment:
        for assignment in self.assignments:
            if assignment.borrower_id == borrower_id:
                return assignment
        raise KeyError(f"Unknown borrower id {borrower_id}.")

    def events_for_borrower(self, borrower_id: UUID) -> Iterator[SignalEventRecord]:
        return self.signals.events_for_borrower(borrower_id)

    def content_hashes(self) -> Mapping[str, str]:
        assignment_payload = [
            {
                "borrower_id": assignment.borrower_id,
                "borrower_reference": assignment.borrower_reference,
                "cohort": assignment.cohort,
                "ordinal": assignment.ordinal,
            }
            for assignment in self.assignments
        ]
        encoded = json.dumps(
            _canonical_value(assignment_payload), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return MappingProxyType(
            {
                "assignments": hashlib.sha256(encoded).hexdigest(),
                "signals": self.signals.content_hash(),
                "labels": self.labels.content_hash(),
            }
        )

    @property
    def content_hash(self) -> str:
        hashes = self.content_hashes()
        payload = "\n".join(f"{key}:{hashes[key]}" for key in sorted(hashes))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    return value


def _resolve_authored_size(borrower_count: int, requested: int | None) -> int:
    if borrower_count < len(COHORT_NAMES) - 1:
        raise CohortGenerationError(
            "At least three borrowers are required to author the three reference cohorts."
        )
    if requested is not None:
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
            raise ValueError("authored_cohort_size must be a positive integer.")
        size = requested
    else:
        size = min(DEFAULT_AUTHORED_COHORT_SIZE, max(1, borrower_count // 10))
    if size * (len(COHORT_NAMES) - 1) > borrower_count:
        raise CohortGenerationError(
            "authored_cohort_size leaves no room for all three authored cohorts."
        )
    return size


def assign_cohorts(
    portfolio: ReferencePortfolio, *, authored_cohort_size: int | None = None
) -> tuple[CohortAssignment, ...]:
    """Assign three authored cohorts and a deterministic templated remainder."""
    if not isinstance(portfolio, ReferencePortfolio):
        raise TypeError("portfolio must be a ReferencePortfolio.")
    size = _resolve_authored_size(len(portfolio.borrowers), authored_cohort_size)
    authored = (
        DETERIORATING_COHORT,
        NOISY_TRANSIENT_COHORT,
        STABLE_COHORT,
    )
    assignments: list[CohortAssignment] = []
    for index, borrower in enumerate(portfolio.borrowers):
        cohort = (
            authored[index // size] if index < size * len(authored) else TEMPLATED_REMAINDER_COHORT
        )
        assignments.append(
            CohortAssignment(
                borrower_id=borrower.id,
                borrower_reference=borrower.reference,
                cohort=cohort,
                ordinal=index + 1,
            )
        )
    return tuple(assignments)


def _mapping_for_assignments(assignments: tuple[CohortAssignment, ...]) -> Mapping[UUID, str]:
    mapping = {assignment.borrower_id: assignment.cohort for assignment in assignments}
    if len(mapping) != len(assignments):
        raise CohortGenerationError("A borrower cannot have more than one cohort assignment.")
    return MappingProxyType(mapping)


def _references_for_portfolio(portfolio: ReferencePortfolio) -> Mapping[UUID, str]:
    return MappingProxyType({borrower.id: borrower.reference for borrower in portfolio.borrowers})


def _validate_stream_coverage(dataset: ReferenceCohorts) -> None:
    expected_per_borrower = dataset.signals.signal_days * len(SIGNAL_FAMILIES)
    expected_total = len(dataset.portfolio.borrowers) * expected_per_borrower
    if len(dataset.signals) != expected_total:
        raise CohortGenerationError(
            f"Signal stream has {len(dataset.signals)} events; expected {expected_total}."
        )

    # SignalEventStream validates the complete borrower/facility keyspace in
    # its constructor.  Materialising every row here would turn a bounded
    # generator into an 11-million-object validation pass.  Sample the first
    # and last borrower plus one borrower from each authored profile to prove
    # the row shape and date/family axes without changing the memory bound.
    sample_ids = {
        dataset.portfolio.borrowers[0].id,
        dataset.portfolio.borrowers[-1].id,
    }
    for cohort in (DETERIORATING_COHORT, NOISY_TRANSIENT_COHORT, STABLE_COHORT):
        sample_ids.add(
            next(
                assignment for assignment in dataset.assignments if assignment.cohort == cohort
            ).borrower_id
        )
    for borrower_id in sample_ids:
        borrower = next(
            borrower for borrower in dataset.portfolio.borrowers if borrower.id == borrower_id
        )
        events = tuple(dataset.signals.events_for_borrower(borrower.id))
        if len(events) != expected_per_borrower:
            raise CohortGenerationError(
                f"Borrower {borrower.reference} does not have "
                f"{expected_per_borrower} signal events."
            )
        if {event.family for event in events} != set(SIGNAL_FAMILIES):
            raise CohortGenerationError(
                f"Borrower {borrower.reference} is missing one of the seven signal families."
            )


def _validate_deteriorating_trajectories(dataset: ReferenceCohorts) -> None:
    for assignment in dataset.assignments:
        if assignment.cohort != DETERIORATING_COHORT:
            continue
        utilisation = tuple(
            event.magnitude
            for event in dataset.signals.events_for_borrower(
                assignment.borrower_id, family="utilisation"
            )
        )
        payment = tuple(
            event.magnitude
            for event in dataset.signals.events_for_borrower(
                assignment.borrower_id, family="payment"
            )
        )
        if (
            not utilisation
            or utilisation[-1] <= utilisation[0]
            or any(
                current < previous
                for previous, current in zip(utilisation, utilisation[1:], strict=False)
            )
        ):
            raise CohortGenerationError(
                f"Deteriorating borrower {assignment.borrower_reference} has no "
                "climbing utilisation trajectory."
            )
        if (
            not payment
            or payment[-1] <= payment[0]
            or any(
                current < previous for previous, current in zip(payment, payment[1:], strict=False)
            )
        ):
            raise CohortGenerationError(
                f"Deteriorating borrower {assignment.borrower_reference} has no "
                "lengthening payment trajectory."
            )


def _validate_noise_and_stable(dataset: ReferenceCohorts) -> None:
    for assignment in dataset.assignments:
        if assignment.cohort not in {NOISY_TRANSIENT_COHORT, STABLE_COHORT}:
            continue
        events = tuple(dataset.signals.events_for_borrower(assignment.borrower_id))
        if assignment.cohort == NOISY_TRANSIENT_COHORT:
            sustained = [summary for summary in persistence_summary(events) if summary.sustained]
            if sustained:
                families = ", ".join(summary.family for summary in sustained)
                raise CohortGenerationError(
                    f"Noisy borrower {assignment.borrower_reference} crosses T3 in {families}."
                )
        else:
            for event in events:
                lower, upper = STABLE_BANDS[event.family]
                if not lower <= event.magnitude <= upper:
                    raise CohortGenerationError(
                        f"Stable borrower {assignment.borrower_reference} leaves the "
                        f"{event.family} band."
                    )


def validate_cohorts(dataset: ReferenceCohorts) -> None:
    """Prove the generation invariants before the dataset is handed to a test."""
    if len(dataset.assignments) != len(dataset.portfolio.borrowers):
        raise CohortGenerationError("Cohort assignments are incomplete.")
    if set(dataset.cohort_by_borrower) != {borrower.id for borrower in dataset.portfolio.borrowers}:
        raise CohortGenerationError("Cohort assignments do not cover the portfolio exactly.")
    counts = dataset.cohort_counts
    for cohort in (DETERIORATING_COHORT, NOISY_TRANSIENT_COHORT, STABLE_COHORT):
        if counts.get(cohort, 0) == 0:
            raise CohortGenerationError(f"Authored cohort {cohort!r} is empty.")
    _validate_stream_coverage(dataset)
    _validate_deteriorating_trajectories(dataset)
    _validate_noise_and_stable(dataset)
    verify_labels(dataset.labels, dataset.signals, dataset.cohort_by_borrower)


def generate_reference_cohorts(
    portfolio: ReferencePortfolio,
    *,
    authored_cohort_size: int | None = None,
    signal_days: int = DEFAULT_SIGNAL_DAYS,
) -> ReferenceCohorts:
    """Generate, validate and return the complete labelled cohort dataset."""
    assignments = assign_cohorts(portfolio, authored_cohort_size=authored_cohort_size)
    cohort_by_borrower = _mapping_for_assignments(assignments)
    signals = generate_signal_events(
        portfolio,
        cohort_by_borrower,
        signal_days=signal_days,
    )
    labels = derive_labels(
        signals,
        cohort_by_borrower,
        _references_for_portfolio(portfolio),
    )
    dataset = ReferenceCohorts(
        portfolio=portfolio,
        assignments=assignments,
        signals=signals,
        labels=labels,
    )
    validate_cohorts(dataset)
    return dataset


generate_cohorts = generate_reference_cohorts
build_cohorts = generate_reference_cohorts
CohortDataset = ReferenceCohorts

DETERIORATING = DETERIORATING_COHORT
NOISY_TRANSIENT = NOISY_TRANSIENT_COHORT
STABLE = STABLE_COHORT
TEMPLATED_REMAINDER = TEMPLATED_REMAINDER_COHORT


__all__ = [
    "COHORT_NAMES",
    "DEFAULT_AUTHORED_COHORT_SIZE",
    "DETERIORATING",
    "DETERIORATING_COHORT",
    "NOISY_TRANSIENT_COHORT",
    "NOISY_TRANSIENT",
    "STABLE_BANDS",
    "STABLE_COHORT",
    "STABLE",
    "TEMPLATED_REMAINDER_COHORT",
    "TEMPLATED_REMAINDER",
    "BorrowerWithCohort",
    "CohortAssignment",
    "CohortDataset",
    "CohortGenerationError",
    "ReferenceCohorts",
    "assign_cohorts",
    "build_cohorts",
    "generate_cohorts",
    "generate_reference_cohorts",
    "validate_cohorts",
]
