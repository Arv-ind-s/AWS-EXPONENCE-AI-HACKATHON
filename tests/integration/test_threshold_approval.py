"""Integration tests for persisted threshold proposals and approval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.config.thresholds import (
    ThresholdProposalRecord,
    ThresholdSnapshotRecord,
    ThresholdStore,
)
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent, ThresholdSnapshot
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.maker_checker import MakerCheckerRequest

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_CLOCK = FixedClock(_NOW)


class _SqlAlchemyThresholdRepository:
    """Database adapter used to prove the config-layer persistence port."""

    def __init__(self, session: Session) -> None:
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
            select(ThresholdSnapshot.version).order_by(ThresholdSnapshot.version.desc()).limit(1)
        )
        snapshot = ThresholdSnapshot(
            id=snapshot_id,
            values=dict(values),
            source=source,
            effective_from=effective_from,
            proposed_by_id=proposed_by_id,
            approved_by_id=approved_by_id,
            note=note,
            created_at=effective_from,
            updated_at=effective_from,
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
        proposal = MakerCheckerRequest(
            id=proposal_id,
            subject_type="threshold_change",
            subject_id=proposal_id,
            operation="threshold_change",
            payload=dict(payload),
            maker_id=maker_id,
            state="pending",
            created_at=created_at,
            updated_at=created_at,
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
        assert proposal is not None
        proposal.checker_id = approver_id
        proposal.state = "approved"
        proposal.decided_at = decided_at
        proposal.reason = "Threshold change approved."
        proposal.updated_at = decided_at
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
        latest = self.session.scalar(
            select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)
        )
        sequence = latest.sequence + 1 if latest is not None else 1
        previous_hash = latest.hash if latest is not None else None
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        actor_id = _actor_id(actor)
        hash_input = "\x1f".join(
            (
                str(sequence),
                occurred_at.isoformat(),
                str(actor_id),
                event_type,
                subject_type,
                str(subject_id),
                canonical_payload,
                previous_hash or "",
            )
        )
        event = AuditEvent(
            id=new_id(),
            sequence=sequence,
            occurred_at=occurred_at,
            actor_id=actor_id,
            actor_label=str(actor_id),
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=dict(payload),
            threshold_snapshot_id=subject_id,
            prev_hash=previous_hash,
            hash=hashlib.sha256(hash_input.encode("utf-8")).hexdigest(),
            created_at=occurred_at,
            updated_at=occurred_at,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        self.session.add(event)
        self.session.flush()
        return event


def _actor_id(actor: object) -> UUID:
    value = actor if isinstance(actor, UUID) else getattr(actor, "id", None)
    assert isinstance(value, UUID)
    return value


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
        payload=proposal.payload,
        version=proposal.version,
    )


@pytest.fixture
def db_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'thresholds.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def _user(username: str) -> AppUser:
    user_id = new_id()
    return AppUser(
        id=user_id,
        username=username,
        email=f"{username}@example.test",
        full_name=username.title(),
        password_hash=None,
        auth_source="local",
        is_active=True,
        failed_attempts=0,
        must_change_password=False,
        locale="en",
        theme="light",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-threshold-integration",
    )


def _seed_users(session: Session) -> tuple[AppUser, AppUser]:
    maker = _user("maker")
    checker = _user("checker")
    session.add_all([maker, checker])
    session.commit()
    return maker, checker


def test_proposal_pending_until_approved(db_session: Session) -> None:
    maker, checker = _seed_users(db_session)
    store = ThresholdStore(
        _SqlAlchemyThresholdRepository(db_session), clock=_CLOCK, request_id="rq-pending-test"
    )
    initial_snapshot_id = store.snapshot_id()
    proposal = store.propose({"T1": {"act": "0.75"}}, maker, note="calibration")
    db_session.commit()

    pending = db_session.get(MakerCheckerRequest, proposal.id)
    assert pending is not None
    assert pending.state == "pending"
    assert pending.maker_id == maker.id
    assert (
        db_session.scalar(select(ThresholdSnapshot).order_by(ThresholdSnapshot.version.desc())).id
        == initial_snapshot_id
    )
    assert store.get("T1")["act"] == Decimal("0.70")
    assert checker.id != maker.id


def test_proposer_cannot_approve(db_session: Session) -> None:
    maker, _checker = _seed_users(db_session)
    store = ThresholdStore(
        _SqlAlchemyThresholdRepository(db_session),
        clock=_CLOCK,
        request_id="rq-self-approval-test",
    )
    proposal = store.propose({"T2": {"confidence_floor": "0.55"}}, maker)
    db_session.commit()

    with pytest.raises(Conflict, match="distinct-actor rule"):
        store.approve(proposal.id, maker)

    pending = db_session.get(MakerCheckerRequest, proposal.id)
    assert pending is not None
    assert pending.state == "pending"
    assert (
        db_session.scalar(
            select(ThresholdSnapshot).order_by(ThresholdSnapshot.version.desc())
        ).version
        == 1
    )


def test_approval_writes_snapshot_and_audit_with_before_and_after(db_session: Session) -> None:
    maker, checker = _seed_users(db_session)
    store = ThresholdStore(
        _SqlAlchemyThresholdRepository(db_session), clock=_CLOCK, request_id="rq-approval-test"
    )
    proposal = store.propose({"T1": {"act": "0.75"}}, maker)
    db_session.commit()

    snapshot = store.approve(proposal.id, checker)
    db_session.commit()

    assert snapshot.source == "approved"
    assert snapshot.proposed_by_id == maker.id
    assert snapshot.approved_by_id == checker.id
    assert store.snapshot_id() == snapshot.id
    assert store.get("T1")["act"] == Decimal("0.75")

    event = db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "threshold_change_approved")
    )
    assert event is not None
    assert event.threshold_snapshot_id == snapshot.id
    assert event.payload["before"]["T1"]["act"] == pytest.approx(0.70)
    assert event.payload["after"]["T1"]["act"] == pytest.approx(0.75)
    assert event.payload["proposer_id"] == str(maker.id)
    assert event.payload["approver_id"] == str(checker.id)
