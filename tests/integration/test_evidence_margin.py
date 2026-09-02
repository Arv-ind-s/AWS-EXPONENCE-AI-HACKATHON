"""Integration coverage for the T-078 evidence margin, document strip and
case actions extension of the borrower case file."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    Borrower,
    Case,
    EvidenceItem,
    Portfolio,
    UserPortfolioScope,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.borrower import create_borrower_router

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_BORROWER_NAME = "Sundown Textiles Private Limited"


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user_id = uuid4()
        self.principal = Principal.user(self.user_id, (Permission.VIEW_BORROWER,))

        self.portfolio = Portfolio.create(
            code="T078",
            name="T-078 portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t078-portfolio",
        )
        self.borrower = Borrower(
            id=uuid4(),
            reference="B-T078",
            legal_name=_BORROWER_NAME,
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t078-borrower",
        )
        self.session.add_all(
            [
                self.portfolio,
                self.borrower,
                UserPortfolioScope(
                    user_id=self.user_id,
                    portfolio_id=self.portfolio.id,
                    include_descendants=True,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-t078-scope",
                ),
            ]
        )
        self.session.flush()

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def client(
        self, *, permissions: tuple[Permission, ...] = (Permission.VIEW_BORROWER,)
    ) -> TestClient:
        principal = Principal.user(self.user_id, permissions)
        app = create_app(
            routers=(create_borrower_router(self.session),),
            principal_resolver=lambda _request: principal,
        )
        return TestClient(app)

    def case(self, *, reference: str = "CASE-T078", state: str = "open") -> Case:
        """Open one case for the fixture borrower, in the fixture's scope."""
        row = Case(
            id=uuid4(),
            reference=reference,
            borrower_id=self.borrower.id,
            state=state,
            created_at=_NOW,
            updated_at=_NOW,
            created_by_id=self.user_id,
            updated_by_id=self.user_id,
            request_id="rq-t078-case",
            version=1,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def evidence(
        self,
        *,
        family: str = "payment",
        evidence_type: str = "delay",
        state: str = "sustained",
        counts_toward_pressure: bool = True,
        decay_factor: Decimal | None = Decimal("1"),
        materiality_pct: Decimal | None = Decimal("8"),
        superseded_by_id: UUID | None = None,
        supersedes_id: UUID | None = None,
        first_seen: date = date(2026, 7, 1),
        last_seen: date = date(2026, 8, 1),
        suffix: str = "",
    ) -> EvidenceItem:
        row = EvidenceItem(
            id=uuid4(),
            borrower_id=self.borrower.id,
            facility_id=None,
            family=family,
            evidence_type=evidence_type,
            first_seen=first_seen,
            last_seen=last_seen,
            persistence_days=21,
            event_count_window=3,
            materiality_pct=materiality_pct,
            decay_factor=decay_factor,
            state=state,
            counts_toward_pressure=counts_toward_pressure,
            superseded_by_id=superseded_by_id,
            supersedes_id=supersedes_id,
            source_event_ids=[f"evt-{suffix or evidence_type}"],
            last_scored_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t078-evidence-{suffix or evidence_type}",
        )
        self.session.add(row)
        self.session.flush()
        return row


def test_decayed_item_listed_with_state() -> None:
    fixture = _Fixture()
    try:
        item = fixture.evidence(
            evidence_type="stock_shortfall",
            decay_factor=Decimal("0"),
            counts_toward_pressure=False,
            materiality_pct=Decimal("4"),
        )
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        assert f'id="evidence-item-{item.id}"' in body
        assert 'data-decay-state="decayed"' in body
        assert "Decayed" in body
        assert "Stock Shortfall" in body
    finally:
        fixture.close()


def test_supersession_links_both_directions() -> None:
    fixture = _Fixture()
    try:
        predecessor = fixture.evidence(
            evidence_type="delay",
            state="sustained",
            counts_toward_pressure=True,
            suffix="predecessor",
        )
        successor = fixture.evidence(
            evidence_type="delay",
            state="sustained",
            counts_toward_pressure=True,
            supersedes_id=predecessor.id,
            first_seen=date(2026, 8, 2),
            last_seen=date(2026, 8, 15),
            suffix="successor",
        )
        predecessor.state = "superseded"
        predecessor.counts_toward_pressure = False
        predecessor.superseded_by_id = successor.id
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        assert f'id="evidence-item-{predecessor.id}"' in body
        assert f'id="evidence-item-{successor.id}"' in body
        assert f'href="#evidence-item-{successor.id}"' in body
        assert f'href="#evidence-item-{predecessor.id}"' in body
    finally:
        fixture.close()


def test_non_counting_item_shows_reason() -> None:
    fixture = _Fixture()
    try:
        fixture.evidence(
            evidence_type="utilisation_spike",
            counts_toward_pressure=False,
            materiality_pct=Decimal("2"),
            state="transient",
        )
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert "Does not count toward pressure" in body
        assert (
            "Recorded materiality did not meet the pressure threshold on its last scored run."
            in body
        )
    finally:
        fixture.close()


def test_no_documents_states_and_offers_upload() -> None:
    fixture = _Fixture()
    try:
        with fixture.client(
            permissions=(Permission.VIEW_BORROWER, Permission.UPLOAD_DOCUMENT)
        ) as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")
        body = response.text
        assert "No source documents are available for this borrower." in body
        assert "Upload a document" in body
        assert 'href="/intake"' in body

        with fixture.client(permissions=(Permission.VIEW_BORROWER,)) as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")
        body = response.text
        assert "No source documents are available for this borrower." in body
        assert "Upload a document" not in body
    finally:
        fixture.close()


def test_actions_match_permission_matrix() -> None:
    fixture = _Fixture()
    try:
        with fixture.client(permissions=(Permission.VIEW_BORROWER,)) as client:
            body = client.get(f"/borrowers/{fixture.borrower.reference}").text
        assert "Why this decision" in body
        assert "Run simulation" not in body
        assert "Generate AI explanation" not in body
        assert "Log action" not in body

        with fixture.client(
            permissions=(Permission.VIEW_BORROWER, Permission.RUN_SIMULATION)
        ) as client:
            body = client.get(f"/borrowers/{fixture.borrower.reference}").text
        assert "Run simulation" in body
        assert "Generate AI explanation" not in body
        assert "Log action" not in body

        with fixture.client(
            permissions=(
                Permission.VIEW_BORROWER,
                Permission.RUN_SIMULATION,
                Permission.GENERATE_MEMO,
                Permission.LOG_ACTION,
            )
        ) as client:
            body = client.get(f"/borrowers/{fixture.borrower.reference}").text
        assert "Run simulation" in body
        assert "Generate AI explanation" in body
        assert "Log action" in body
        # No case exists for this borrower, so the control is disabled and
        # names the prerequisite rather than claiming the capability is
        # unbuilt: action logging is implemented, it just needs a case.
        assert "No case is open for this borrower yet" in body
        assert "/cases/" not in body

        case = fixture.case()
        with fixture.client(
            permissions=(
                Permission.VIEW_BORROWER,
                Permission.RUN_SIMULATION,
                Permission.GENERATE_MEMO,
                Permission.LOG_ACTION,
            )
        ) as client:
            body = client.get(f"/borrowers/{fixture.borrower.reference}").text
        assert "Log action" in body
        assert f"/cases/{case.reference}#case-actions" in body
        assert "No case is open for this borrower yet" not in body
    finally:
        fixture.close()


def test_grouping_by_family() -> None:
    fixture = _Fixture()
    try:
        fixture.evidence(family="payment", evidence_type="delay", suffix="payment-1")
        fixture.evidence(
            family="payment",
            evidence_type="bounce",
            first_seen=date(2026, 7, 10),
            last_seen=date(2026, 8, 5),
            suffix="payment-2",
        )
        fixture.evidence(family="treasury", evidence_type="drawdown", suffix="treasury-1")

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        payment_index = body.index('scope="rowgroup">Payment<')
        treasury_index = body.index('scope="rowgroup">Treasury<')
        assert payment_index < treasury_index
        payment_section = body[payment_index:treasury_index]
        assert "Delay" in payment_section
        assert "Bounce" in payment_section
        assert "Drawdown" not in payment_section
        assert "Drawdown" in body[treasury_index:]
    finally:
        fixture.close()


__all__ = ["_Fixture"]
