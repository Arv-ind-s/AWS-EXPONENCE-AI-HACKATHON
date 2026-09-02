"""SQLite integration coverage for the shared approval workflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.maker_checker import MakerCheckerRequest as MakerCheckerRow
from covenant_radar.security.maker_checker import (
    ApplicationCallbackRegistry,
    MakerCheckerRequest,
    MakerCheckerSettings,
    MakerCheckerState,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.approvals import ApprovalService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        _subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, dict(payload)))
        return object()


class _Notifier:
    def notify(self, _event_type: str, _payload: Mapping[str, object]) -> object:
        return object()


class _SqlAlchemyRepository:
    """Adapter proving the service's repository port against the real model."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, request: MakerCheckerRequest) -> MakerCheckerRequest:
        row = MakerCheckerRow(
            id=request.id,
            subject_type=request.subject_type,
            subject_id=request.subject_id,
            operation=request.operation,
            payload=dict(request.payload),
            maker_id=request.maker_id,
            checker_id=None,
            state=request.state.value,
            created_at=request.created_at,
            updated_at=request.created_at,
            created_by_id=request.maker_id,
            updated_by_id=request.maker_id,
            request_id="rq-integration-maker-checker",
            version=request.version,
        )
        self.session.add(row)
        self.session.flush()
        return _record(row)

    def get_for_update(self, request_id: UUID) -> MakerCheckerRequest | None:
        row = self.session.scalar(
            select(MakerCheckerRow)
            .where(MakerCheckerRow.id == request_id)
            .with_for_update()
        )
        return _record(row) if row is not None else None

    def list_pending(self) -> Sequence[MakerCheckerRequest]:
        rows = self.session.scalars(
            select(MakerCheckerRow)
            .where(MakerCheckerRow.state == MakerCheckerState.PENDING.value)
            .order_by(MakerCheckerRow.created_at, MakerCheckerRow.id)
        ).all()
        return tuple(_record(row) for row in rows)

    def decide(
        self,
        request_id: UUID,
        *,
        checker_id: UUID,
        state: MakerCheckerState,
        decided_at: datetime,
        reason: str | None,
        expected_version: int,
    ) -> MakerCheckerRequest:
        row = self.session.get(MakerCheckerRow, request_id)
        if row is None or row.state != MakerCheckerState.PENDING.value:
            raise Conflict("The request is no longer pending.")
        if row.version != expected_version:
            raise Conflict("The request version is stale.")
        row.checker_id = checker_id
        row.state = state.value
        row.decided_at = decided_at
        row.reason = reason
        row.updated_at = decided_at
        row.updated_by_id = checker_id
        row.version += 1
        self.session.flush()
        return _record(row)

    def expire(
        self,
        request_id: UUID,
        *,
        expired_at: datetime,
        expected_version: int,
    ) -> MakerCheckerRequest:
        row = self.session.get(MakerCheckerRow, request_id)
        if row is None or row.state != MakerCheckerState.PENDING.value:
            raise Conflict("The request is no longer pending.")
        if row.version != expected_version:
            raise Conflict("The request version is stale.")
        row.state = MakerCheckerState.EXPIRED.value
        row.decided_at = expired_at
        row.reason = "Approval window elapsed."
        row.updated_at = expired_at
        row.version += 1
        self.session.flush()
        return _record(row)


def _record(row: MakerCheckerRow) -> MakerCheckerRequest:
    return MakerCheckerRequest(
        id=row.id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        operation=row.operation,
        payload=row.payload,
        maker_id=row.maker_id,
        checker_id=row.checker_id,
        state=row.state,
        created_at=row.created_at,
        decided_at=row.decided_at,
        reason=row.reason,
        version=row.version,
    )


def _user(username: str) -> AppUser:
    return AppUser(
        id=new_id(),
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
        request_id="rq-integration-maker-checker",
    )


@pytest.fixture
def db_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'approval-flow.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def _build(
    session: Session,
    *,
    enabled_operations: Mapping[str, bool] | None = None,
) -> tuple[ApprovalService, AppUser, AppUser, _Audit, list[MakerCheckerRequest]]:
    maker_row = _user("maker")
    checker_row = _user("checker")
    session.add_all([maker_row, checker_row])
    session.commit()
    applied: list[MakerCheckerRequest] = []
    registry = ApplicationCallbackRegistry()
    registry.register(
        "covenant_registration",
        lambda request, _actor: applied.append(request),
        propose_permission=Permission.REGISTER_COVENANT,
        approve_permission=Permission.APPROVE_COVENANT,
    )
    registry.register(
        "threshold_change",
        lambda request, _actor: applied.append(request),
        propose_permission=Permission.PROPOSE_THRESHOLDS,
        approve_permission=Permission.APPROVE_THRESHOLDS,
    )
    audit = _Audit()
    service = ApprovalService(
        _SqlAlchemyRepository(session),
        audit,
        registry=registry,
        clock=FixedClock(_NOW),
        settings=MakerCheckerSettings(enabled_operations=enabled_operations or {}),
        notifier=_Notifier(),
        request_id="rq-integration-maker-checker",
    )
    return service, maker_row, checker_row, audit, applied


def test_pending_list_scoped_to_checker(db_session: Session) -> None:
    service, maker_row, checker_row, _audit, _applied = _build(db_session)
    maker = Principal.user(
        maker_row.id,
        (Permission.REGISTER_COVENANT, Permission.PROPOSE_THRESHOLDS),
    )
    checker = Principal.user(checker_row.id, (Permission.APPROVE_COVENANT,))
    covenant = service.submit("covenant_registration", ("covenant", new_id()), {"v": 1}, maker)
    service.submit("threshold_change", ("threshold_snapshot", new_id()), {"v": 2}, maker)

    pending = service.list_pending(checker)

    assert [request.id for request in pending] == [covenant.id]


def test_approval_applies_payload_and_audits(db_session: Session) -> None:
    service, maker_row, checker_row, audit, applied = _build(db_session)
    maker = Principal.user(maker_row.id, (Permission.REGISTER_COVENANT,))
    checker = Principal.user(checker_row.id, (Permission.APPROVE_COVENANT,))
    request = service.submit(
        "covenant_registration",
        ("covenant", new_id()),
        {"threshold": "3.0"},
        maker,
    )

    approved = service.decide(request.id, checker, True, "Reviewed against sanction letter")
    db_session.commit()
    row = db_session.get(MakerCheckerRow, request.id)

    assert approved.state is MakerCheckerState.APPROVED
    assert row is not None
    assert row.state == MakerCheckerState.APPROVED.value
    assert row.checker_id == checker_row.id
    assert len(applied) == 1
    assert applied[0].payload == {"threshold": "3.0"}
    assert [event[0] for event in audit.events] == [
        "maker_checker_submitted",
        "maker_checker_approved",
    ]
    assert audit.events[-1][1]["applied"] is True


def test_disabled_mode_records_absence_of_checker(db_session: Session) -> None:
    service, maker_row, _checker_row, audit, applied = _build(
        db_session,
        enabled_operations={"covenant_registration": False},
    )
    maker = Principal.user(maker_row.id, (Permission.REGISTER_COVENANT,))

    direct = service.submit("covenant_registration", ("covenant", new_id()), {"v": 1}, maker)
    db_session.commit()

    assert direct.state is MakerCheckerState.APPROVED
    assert direct.checker_id is None
    assert len(applied) == 1
    assert db_session.scalars(select(MakerCheckerRow)).all() == []
    assert [event[0] for event in audit.events] == ["maker_checker_disabled_applied"]
    event_payload = audit.events[0][1]
    assert event_payload["checker_required"] is False
    assert event_payload["no_second_actor_required"] is True
