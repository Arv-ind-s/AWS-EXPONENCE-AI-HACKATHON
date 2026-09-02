"""Persistence-neutral audit-store contracts and an in-memory adapter.

The application boundary in :mod:`covenant_radar.audit.record` accepts this
store protocol.  SQLAlchemy stays in ``db.repositories.audit``; the same
recording and verification rules can therefore be exercised without a
database in unit tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol, cast, runtime_checkable
from uuid import UUID

from covenant_radar.audit.chain import (
    AuditChainBreak,
    AuditChainRow,
    compute_event_hash,
    normalise_payload,
    verify_chain,
)
from covenant_radar.core.ids import new_id


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """The validated, persistence-neutral content of one audit event."""

    event_type: str
    subject_type: str
    subject_id: UUID
    payload: Mapping[str, object]
    actor_id: UUID | None
    actor_label: str | None
    occurred_at: datetime
    request_id: str
    threshold_snapshot_id: UUID | None = None


@runtime_checkable
class AuditStore(Protocol):
    """Append to the audit stream inside the caller's transaction."""

    def append(self, record: AuditRecord) -> object:
        """Append one event without committing the caller's transaction."""
        ...


@dataclass(slots=True)
class InMemoryAuditEvent:
    """A mutable test row, shaped like the database model."""

    id: UUID
    sequence: int
    occurred_at: datetime
    actor_id: UUID | None
    actor_label: str | None
    event_type: str
    subject_type: str
    subject_id: UUID
    payload: dict[str, object]
    threshold_snapshot_id: UUID | None
    prev_hash: str | None
    hash: str
    created_at: datetime
    updated_at: datetime
    created_by_id: UUID | None
    updated_by_id: UUID | None
    request_id: str


class InMemoryAuditStore:
    """Thread-safe append-only store used by unit tests and offline tools."""

    def __init__(self) -> None:
        self._rows: list[InMemoryAuditEvent] = []
        self._lock = RLock()

    def append(self, record: AuditRecord) -> InMemoryAuditEvent:
        """Append a record and compute its next sequence and digest."""

        payload = normalise_payload(record.payload)
        with self._lock:
            previous = self._rows[-1] if self._rows else None
            sequence = previous.sequence + 1 if previous is not None else 1
            prev_hash = previous.hash if previous is not None else None
            digest = compute_event_hash(
                sequence,
                record.occurred_at,
                record.actor_id if record.actor_id is not None else record.actor_label,
                record.event_type,
                record.subject_type,
                record.subject_id,
                payload,
                prev_hash,
            )
            row = InMemoryAuditEvent(
                id=new_id(),
                sequence=sequence,
                occurred_at=record.occurred_at,
                actor_id=record.actor_id,
                actor_label=record.actor_label,
                event_type=record.event_type,
                subject_type=record.subject_type,
                subject_id=record.subject_id,
                payload=payload,
                threshold_snapshot_id=record.threshold_snapshot_id,
                prev_hash=prev_hash,
                hash=digest,
                created_at=record.occurred_at,
                updated_at=record.occurred_at,
                created_by_id=record.actor_id,
                updated_by_id=record.actor_id,
                request_id=record.request_id,
            )
            self._rows.append(row)
            return row

    def rows(
        self,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
    ) -> tuple[InMemoryAuditEvent, ...]:
        """Return an immutable snapshot of rows in sequence order."""

        _validate_range(from_sequence, to_sequence)
        with self._lock:
            return tuple(
                row
                for row in self._rows
                if (from_sequence is None or row.sequence >= from_sequence)
                and (to_sequence is None or row.sequence <= to_sequence)
            )

    def verify_chain(
        self,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
    ) -> AuditChainBreak | None:
        """Verify the selected rows and return the first break, if any."""

        return verify_chain(
            cast(tuple[AuditChainRow, ...], self.rows()), from_sequence, to_sequence
        )


def _validate_range(from_sequence: int | None, to_sequence: int | None) -> None:
    for name, value in (("from_sequence", from_sequence), ("to_sequence", to_sequence)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{name} must be a positive integer or None.")
    if from_sequence is not None and to_sequence is not None and from_sequence > to_sequence:
        raise ValueError("from_sequence cannot be greater than to_sequence.")


__all__ = ["AuditRecord", "AuditStore", "InMemoryAuditEvent", "InMemoryAuditStore"]
