"""Integration coverage for stage-3 ledger trace emission."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, EvidenceItem, Portfolio
from covenant_radar.db.models.audit import TraceRow
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.signals.evidence import SignalEventFacts
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.ledger import LedgerService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_THRESHOLD_SNAPSHOT_ID = UUID("01990a6e-8e60-7e5b-b7aa-9d8a0dbf0701")


class _Audit:
    def record(
        self,
        event_type: str,
        subject: object,
        payload: object,
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del event_type, subject, payload, actor, request_id
        return object()


class _ThresholdStore:
    def __init__(self) -> None:
        self._values = {
            "T3": {
                "sustained_days": 14,
                "sustained_events": 3,
                "event_window_days": 30,
            },
            "T4": {"headroom_erosion_pct": Decimal("0.05")},
        }

    def get(self, name: str) -> dict[str, object]:
        values = cast(Mapping[str, object], self._values[name])
        return dict(values)

    def snapshot_id(self) -> UUID:
        return _THRESHOLD_SNAPSHOT_ID


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.user_id = uuid4()
        portfolio = Portfolio.create(
            code="STAGE3",
            name="Stage 3 portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-stage3-portfolio",
        )
        self.session.add(portfolio)
        self.session.flush()
        self.session.add(
            AppUser(
                id=self.user_id,
                username="stage3-user",
                email="stage3@example.com",
                full_name="Stage 3 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-stage3-user",
            )
        )
        borrower = Borrower(
            reference="B-STAGE3",
            legal_name="Stage 3 Borrower Private Limited",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-stage3-borrower",
        )
        self.session.add(borrower)
        self.session.flush()
        self.borrower = borrower
        self.scope = Scope.from_paths(self.user_id, [portfolio.path])
        self.principal = Principal.user(
            self.user_id,
            (Permission.INGEST_DATA, Permission.VIEW_EVIDENCE),
        )
        self.service = LedgerService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-stage3-service",
            threshold_store=_ThresholdStore(),
        )

    def add_item(
        self,
        *,
        evidence_type: str = "payment_delay",
        state: str = "sustained",
        persistence_days: int | None = 14,
        event_count_window: int | None = 3,
        materiality_pct: Decimal | None = Decimal("10"),
        decay_factor: Decimal | None = Decimal("0.8"),
    ) -> EvidenceItem:
        item = EvidenceItem(
            id=uuid4(),
            borrower_id=self.borrower.id,
            facility_id=None,
            family="payment",
            evidence_type=evidence_type,
            first_seen=date(2026, 8, 1),
            last_seen=date(2026, 8, 1),
            persistence_days=persistence_days,
            event_count_window=event_count_window,
            materiality_pct=materiality_pct,
            decay_factor=decay_factor,
            state=state,
            counts_toward_pressure=materiality_pct is not None,
            source_event_ids=[f"{evidence_type}-prior-{uuid4()}"],
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-stage3-item",
        )
        self.session.add(item)
        self.session.flush()
        return item

    def received(self) -> SignalEventFacts:
        return SignalEventFacts(
            borrower_id=self.borrower.id,
            facility_id=None,
            event_date=date(2026, 8, 2),
            family="payment",
            event_type="payment_received",
            magnitude=Decimal("0"),
            payload={"is_adverse": False},
            event_id="stage3-payment-received-1",
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def _trace_row(fixture: _Fixture) -> TraceRow:
    row = fixture.session.scalar(
        select(TraceRow).where(
            TraceRow.subject_type == "borrower",
            TraceRow.subject_id == fixture.borrower.id,
            TraceRow.stage == "3",
        )
    )
    assert row is not None
    return row


def _trace_items(row: TraceRow) -> tuple[Mapping[str, object], ...]:
    raw_items = cast(list[object], row.outputs["items"])
    return tuple(cast(Mapping[str, object], item) for item in raw_items)


def test_one_row_per_borrower_per_run() -> None:
    fixture = _Fixture()
    try:
        fixture.add_item()
        fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [fixture.received()],
            as_of=date(2026, 8, 2),
            scope=fixture.scope,
        )
        fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [],
            as_of=date(2026, 8, 2),
            scope=fixture.scope,
        )
        fixture.session.commit()

        rows = fixture.session.scalars(
            select(TraceRow)
            .where(
                TraceRow.subject_type == "borrower",
                TraceRow.subject_id == fixture.borrower.id,
                TraceRow.stage == "3",
            )
            .order_by(TraceRow.occurred_at, TraceRow.id)
        ).all()
        assert len(rows) == 2
        assert rows[0].outputs["supersession_count"] == 1
        assert rows[1].outputs["supersession_count"] == 0
    finally:
        fixture.close()


def test_row_names_t3_arm_and_t4_side() -> None:
    fixture = _Fixture()
    try:
        fixture.add_item()
        fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [],
            as_of=date(2026, 8, 1),
            scope=fixture.scope,
        )
        fixture.session.commit()

        row = _trace_row(fixture)
        comparisons = {comparison["name"]: comparison for comparison in row.thresholds_compared}
        assert comparisons["T3.sustained_days"]["side"] == "at"
        assert comparisons["T3.sustained_events"]["side"] == "at"
        assert comparisons["T4.headroom_erosion_pct"]["side"] == "above"
        item = _trace_items(row)[0]
        persistence = cast(Mapping[str, object], item["persistence"])
        assert persistence["firing_arm"] == "sustained_days"
    finally:
        fixture.close()


def test_unchanged_items_present() -> None:
    fixture = _Fixture()
    try:
        first = fixture.add_item(evidence_type="payment_delay")
        second = fixture.add_item(evidence_type="facility_utilisation", state="transient")
        fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [],
            as_of=date(2026, 8, 1),
            scope=fixture.scope,
        )
        fixture.session.commit()

        row = _trace_row(fixture)
        traced_items = _trace_items(row)
        traced_ids = {item["id"] for item in traced_items}
        assert traced_ids == {str(first.id), str(second.id)}
        states = {item["id"]: item["state"] for item in traced_items}
        assert states[str(first.id)] == "sustained"
        assert states[str(second.id)] == "transient"
    finally:
        fixture.close()


def test_no_evidence_borrower_still_traced() -> None:
    fixture = _Fixture()
    try:
        fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [],
            as_of=date(2026, 8, 1),
            scope=fixture.scope,
        )
        fixture.session.commit()

        row = _trace_row(fixture)
        assert row.inputs["items"] == []
        assert row.outputs["no_evidence"] is True
        assert row.outputs["item_count"] == 0
        assert row.thresholds_compared == []
    finally:
        fixture.close()


def test_rule_version_stamped() -> None:
    fixture = _Fixture()
    try:
        fixture.add_item()
        fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [],
            as_of=date(2026, 8, 1),
            scope=fixture.scope,
        )
        fixture.session.commit()

        row = _trace_row(fixture)
        assert row.rule_or_prompt_version == "evidence.ledger.v1"
        assert row.inputs["threshold_snapshot_id"] == str(_THRESHOLD_SNAPSHOT_ID)
    finally:
        fixture.close()
