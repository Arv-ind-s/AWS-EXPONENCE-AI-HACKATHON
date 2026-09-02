"""SQLAlchemy adapter for the versioned threshold store.

The threshold domain deliberately knows nothing about SQLAlchemy.  This
adapter keeps the active calibration snapshot durable while preserving the
domain store's maker/checker transaction boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.config.thresholds import (
    ThresholdProposalRecord,
    ThresholdRepository,
    ThresholdSnapshotRecord,
)
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.audit import AuditEvent, ThresholdSnapshot
from covenant_radar.db.models.maker_checker import MakerCheckerRequest
from covenant_radar.db.session import is_database_session


class SqlAlchemyThresholdRepository(ThresholdRepository):
    """Persist and read threshold snapshots in the caller's transaction."""

    def __init__(self, session: Session) -> None:
        if not is_database_session(session):
            raise TypeError("SqlAlchemyThresholdRepository requires a SQLAlchemy Session.")
        self.session = session

    def get_active_snapshot(self, *, as_of: datetime) -> ThresholdSnapshotRecord | None:
        snapshot = self.session.scalar(
            select(ThresholdSnapshot)
            .where(ThresholdSnapshot.effective_from <= as_of)
            .order_by(
                ThresholdSnapshot.effective_from.desc(),
                ThresholdSnapshot.created_at.desc(),
                ThresholdSnapshot.id.desc(),
            )
            .limit(1)
        )
        return _snapshot_record(snapshot) if snapshot is not None else None

    def create_snapshot(
        self,
        *,
        snapshot_id: UUID,
        values: Mapping[str, object],
        source: str,
        effective_from: datetime,
        proposed_by_id: UUID | None,
        approved_by_id: UUID | None,
        note: str | None,
        actor_id: UUID | None,
        request_id: str,
    ) -> ThresholdSnapshotRecord:
        latest_version = self.session.scalar(
            select(ThresholdSnapshot.version)
            .order_by(ThresholdSnapshot.version.desc())
            .limit(1)
        )
        snapshot = ThresholdSnapshot(
            id=snapshot_id,
            values=dict(values),
            source=source,
            effective_from=_aware(effective_from),
            proposed_by_id=proposed_by_id,
            approved_by_id=approved_by_id,
            note=note,
            created_at=_aware(effective_from),
            updated_at=_aware(effective_from),
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
            version=(latest_version or 0) + 1,
        )
        self.session.add(snapshot)
        self.session.flush()
        return _snapshot_record(snapshot)

    def create_pending_proposal(
        self,
        *,
        proposal_id: UUID,
        maker_id: UUID,
        payload: Mapping[str, object],
        created_at: datetime,
        request_id: str,
    ) -> ThresholdProposalRecord:
        created = _aware(created_at)
        proposal = MakerCheckerRequest(
            id=proposal_id,
            subject_type="threshold_change",
            subject_id=proposal_id,
            operation="threshold_change",
            payload=dict(payload),
            maker_id=maker_id,
            state="pending",
            created_at=created,
            updated_at=created,
            created_by_id=maker_id,
            updated_by_id=maker_id,
            request_id=request_id,
            version=1,
        )
        self.session.add(proposal)
        self.session.flush()
        return _proposal_record(proposal)

    def lock_pending_proposal(self, proposal_id: UUID) -> ThresholdProposalRecord | None:
        proposal = self.session.scalar(
            select(MakerCheckerRequest)
            .where(MakerCheckerRequest.id == proposal_id)
            .with_for_update()
        )
        return _proposal_record(proposal) if proposal is not None else None

    def mark_proposal_approved(
        self,
        *,
        proposal_id: UUID,
        approver_id: UUID,
        decided_at: datetime,
        request_id: str,
    ) -> None:
        proposal = self.session.get(MakerCheckerRequest, proposal_id)
        if proposal is None:
            raise LookupError(f"Threshold proposal {proposal_id} disappeared during approval.")
        proposal.checker_id = approver_id
        proposal.state = "approved"
        proposal.decided_at = _aware(decided_at)
        proposal.reason = "Threshold change approved."
        proposal.updated_at = _aware(decided_at)
        proposal.updated_by_id = approver_id
        proposal.request_id = request_id
        proposal.version += 1
        self.session.flush()

    def record_audit(
        self,
        *,
        event_type: str,
        subject_type: str,
        subject_id: UUID,
        payload: Mapping[str, object],
        actor: object,
        occurred_at: datetime,
        request_id: str,
    ) -> AuditEvent:
        actor_id = actor if isinstance(actor, UUID) else getattr(actor, "id", None)
        if not isinstance(actor_id, UUID):
            raise TypeError("Threshold audit actors must expose a UUID id.")
        previous = self.session.scalar(
            select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)
        )
        sequence = previous.sequence + 1 if previous is not None else 1
        previous_hash = previous.hash if previous is not None else ""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(
            "\x1f".join(
                (
                    str(sequence),
                    _aware(occurred_at).isoformat(),
                    str(actor_id),
                    event_type,
                    subject_type,
                    str(subject_id),
                    canonical,
                    previous_hash,
                )
            ).encode("utf-8")
        ).hexdigest()
        event = AuditEvent(
            id=new_id(),
            sequence=sequence,
            occurred_at=_aware(occurred_at),
            actor_id=actor_id,
            actor_label=str(actor_id),
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=dict(payload),
            threshold_snapshot_id=subject_id,
            prev_hash=previous.hash if previous is not None else None,
            hash=digest,
            created_at=_aware(occurred_at),
            updated_at=_aware(occurred_at),
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        self.session.add(event)
        self.session.flush()
        return event


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Threshold timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _snapshot_record(snapshot: ThresholdSnapshot) -> ThresholdSnapshotRecord:
    return ThresholdSnapshotRecord(
        id=snapshot.id,
        values=snapshot.values,
        source=snapshot.source,
        effective_from=snapshot.effective_from,
        version=snapshot.version,
        proposed_by_id=snapshot.proposed_by_id,
        approved_by_id=snapshot.approved_by_id,
        note=snapshot.note,
    )


def _proposal_record(proposal: MakerCheckerRequest) -> ThresholdProposalRecord:
    return ThresholdProposalRecord(
        id=proposal.id,
        state=proposal.state,
        maker_id=proposal.maker_id,
        payload=cast(Mapping[str, object], proposal.payload),
        version=proposal.version,
    )


__all__ = ["SqlAlchemyThresholdRepository"]
