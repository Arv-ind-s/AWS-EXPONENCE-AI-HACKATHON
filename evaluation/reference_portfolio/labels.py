"""Outcome labels derived from the reference portfolio signal trajectory."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import UUID

from evaluation.reference_portfolio.signals import (
    DETERIORATING_COHORT,
    SignalEventRecord,
    SignalEventStream,
)

LABEL_COVENANT: Final[str] = "utilisation"
LABEL_DIRECTION: Final[str] = "max"
LABEL_THRESHOLD: Final[Decimal] = Decimal("85.00")


class LabelVerificationError(ValueError):
    """Raised when a stored outcome label disagrees with generated signals."""


def _canonical_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class OutcomeLabel:
    """One dated, source-linked covenant outcome for a deteriorating borrower."""

    borrower_id: UUID
    borrower_reference: str
    cohort: str
    covenant: str
    direction: str
    threshold: Decimal
    breach_date: date
    source_event_id: UUID
    source_event_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_event_id, UUID):
            raise TypeError("source_event_id must be a UUID value.")
        if self.cohort != DETERIORATING_COHORT:
            raise LabelVerificationError("Only deteriorating borrowers may carry outcome labels.")
        if self.covenant != LABEL_COVENANT or self.direction != LABEL_DIRECTION:
            raise LabelVerificationError(
                "Outcome label carries an unsupported covenant definition."
            )
        if self.threshold != LABEL_THRESHOLD:
            raise LabelVerificationError("Outcome label threshold is not the reference threshold.")
        if not self.borrower_reference.strip():
            raise ValueError("borrower_reference must not be blank.")
        if not isinstance(self.source_event_hash, str) or len(self.source_event_hash) != 64:
            raise ValueError("source_event_hash must be a SHA-256 hexadecimal digest.")
        try:
            int(self.source_event_hash, 16)
        except ValueError as error:
            raise ValueError("source_event_hash must be a SHA-256 hexadecimal digest.") from error


@dataclass(frozen=True, slots=True)
class OutcomeLabels(Sequence[OutcomeLabel]):
    """Immutable label set with a reproducible content hash."""

    rows: tuple[OutcomeLabel, ...]

    def __post_init__(self) -> None:
        borrower_ids = [row.borrower_id for row in self.rows]
        if len(borrower_ids) != len(set(borrower_ids)):
            raise LabelVerificationError("Each deteriorating borrower must have one outcome label.")

    def __iter__(self) -> Iterator[OutcomeLabel]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int | slice) -> OutcomeLabel | tuple[OutcomeLabel, ...]:
        return self.rows[index]

    @property
    def by_borrower(self) -> Mapping[UUID, OutcomeLabel]:
        return MappingProxyType({row.borrower_id: row for row in self.rows})

    def content_hash(self) -> str:
        payload = [
            {
                "borrower_id": row.borrower_id,
                "borrower_reference": row.borrower_reference,
                "cohort": row.cohort,
                "covenant": row.covenant,
                "direction": row.direction,
                "threshold": row.threshold,
                "breach_date": row.breach_date,
                "source_event_id": row.source_event_id,
                "source_event_hash": row.source_event_hash,
            }
            for row in self.rows
        ]
        encoded = json.dumps(
            _canonical_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _events_for_borrower(
    events: SignalEventStream | Iterable[SignalEventRecord], borrower_id: UUID
) -> Iterable[SignalEventRecord]:
    if isinstance(events, SignalEventStream):
        return events.events_for_borrower(borrower_id, family="utilisation")
    return (
        event
        for event in events
        if event.borrower_id == borrower_id and event.family == "utilisation"
    )


def _first_crossing(
    events: Iterable[SignalEventRecord],
    *,
    threshold: Decimal = LABEL_THRESHOLD,
) -> SignalEventRecord | None:
    crossing: SignalEventRecord | None = None
    for event in events:
        if event.magnitude >= threshold and (
            crossing is None or event.event_date < crossing.event_date
        ):
            crossing = event
    return crossing


def derive_labels(
    events: SignalEventStream | Iterable[SignalEventRecord],
    cohort_by_borrower: Mapping[UUID, str],
    borrower_references: Mapping[UUID, str] | None = None,
) -> OutcomeLabels:
    """Derive labels from the first computed utilisation threshold crossing.

    No breach date is accepted as an input.  The only source of the date is
    the generated event trajectory, and the selected event is retained on the
    label so a later evaluator can inspect the derivation.
    """
    if not isinstance(cohort_by_borrower, Mapping):
        raise TypeError("cohort_by_borrower must be a mapping.")
    references = borrower_references or {}
    event_source: SignalEventStream | tuple[SignalEventRecord, ...]
    event_source = events if isinstance(events, SignalEventStream) else tuple(events)
    rows: list[OutcomeLabel] = []
    for borrower_id, cohort in cohort_by_borrower.items():
        if cohort != DETERIORATING_COHORT:
            continue
        crossing = _first_crossing(_events_for_borrower(event_source, borrower_id))
        if crossing is None:
            raise LabelVerificationError(
                f"Deteriorating borrower {borrower_id} never reaches the labelled "
                "utilisation threshold."
            )
        reference = references.get(borrower_id, crossing.borrower_reference)
        rows.append(
            OutcomeLabel(
                borrower_id=borrower_id,
                borrower_reference=reference,
                cohort=DETERIORATING_COHORT,
                covenant=LABEL_COVENANT,
                direction=LABEL_DIRECTION,
                threshold=LABEL_THRESHOLD,
                breach_date=crossing.event_date,
                source_event_id=crossing.id,
                source_event_hash=crossing.content_hash or "",
            )
        )
    rows.sort(key=lambda row: (row.breach_date, row.borrower_reference, str(row.borrower_id)))
    return OutcomeLabels(rows=tuple(rows))


generate_labels = derive_labels
Label = OutcomeLabel
LabelSet = OutcomeLabels


def verify_labels(
    labels: OutcomeLabels,
    events: SignalEventStream | Iterable[SignalEventRecord],
    cohort_by_borrower: Mapping[UUID, str],
) -> None:
    """Recompute every label and refuse any disagreement."""
    if not isinstance(labels, OutcomeLabels):
        raise TypeError("labels must be an OutcomeLabels value.")
    derived = derive_labels(events, cohort_by_borrower)
    actual_by_borrower = labels.by_borrower
    expected_by_borrower = derived.by_borrower
    if set(actual_by_borrower) != set(expected_by_borrower):
        raise LabelVerificationError(
            "Outcome labels do not cover exactly the deteriorating borrowers."
        )
    for borrower_id, expected in expected_by_borrower.items():
        actual = actual_by_borrower[borrower_id]
        if actual != expected:
            raise LabelVerificationError(
                f"Outcome label for borrower {borrower_id} disagrees with its generated trajectory."
            )


__all__ = [
    "LABEL_COVENANT",
    "LABEL_DIRECTION",
    "LABEL_THRESHOLD",
    "Label",
    "LabelSet",
    "LabelVerificationError",
    "OutcomeLabel",
    "OutcomeLabels",
    "derive_labels",
    "generate_labels",
    "verify_labels",
]
