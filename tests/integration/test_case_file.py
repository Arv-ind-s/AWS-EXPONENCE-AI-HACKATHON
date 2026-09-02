"""Integration coverage for the T-075 borrower case file."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    Borrower,
    Covenant,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
    Facility,
    Forecast,
    ForecastRun,
    Portfolio,
    TriageEntry,
    UserPortfolioScope,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.borrower import create_borrower_router

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_AS_OF = date(2026, 8, 30)
_BORROWER_NAME = "Meridian Auto Components Private Limited"


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.principal = Principal.user(uuid4(), (Permission.VIEW_BORROWER,))

        self.portfolio = Portfolio.create(
            code="CASE",
            name="Case portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-portfolio",
        )
        self.borrower = Borrower(
            id=uuid4(),
            reference="B-T075",
            legal_name=_BORROWER_NAME,
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-borrower",
        )
        self.session.add_all(
            [
                self.portfolio,
                self.borrower,
                UserPortfolioScope(
                    user_id=self.principal.id,
                    portfolio_id=self.portfolio.id,
                    include_descendants=True,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-t075-scope",
                ),
            ]
        )
        self.session.flush()

        self.facility = Facility(
            id=uuid4(),
            reference="F-T075",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("624000000.0000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-facility",
        )
        self.covenant = Covenant(
            id=uuid4(),
            reference="CV-T075",
            facility_id=self.facility.id,
            name="Total Debt / Tangible Net Worth",
            covenant_class="financial",
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-covenant",
        )
        self.version = CovenantVersion(
            id=uuid4(),
            covenant_id=self.covenant.id,
            version_no=1,
            threshold=Decimal("3.25"),
            direction="max",
            unit="x",
            frequency="quarterly",
            test_basis="standalone",
            effective_from=date(2025, 1, 1),
            status="live",
            tested_at_least_once=True,
            registered_by_id=self.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-version",
        )
        self.run = ForecastRun(
            id=uuid4(),
            as_of_date=_AS_OF,
            started_at=_NOW - timedelta(hours=1),
            finished_at=_NOW,
            covenant_count=1,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-run",
        )
        self.session.add_all([self.facility, self.covenant, self.version, self.run])
        self.session.flush()

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def client(self) -> TestClient:
        app = create_app(
            routers=(create_borrower_router(self.session),),
            principal_resolver=lambda _request: self.principal,
        )
        return TestClient(app)

    def triage(self, *, exposure: Decimal | None = Decimal("624000000")) -> TriageEntry:
        row = TriageEntry(
            id=uuid4(),
            run_id=self.run.id,
            borrower_id=self.borrower.id,
            worst_covenant_version_id=self.version.id,
            worst_horizon=90,
            probability=Decimal("0.5825"),
            confidence=Decimal("0.80"),
            exposure=exposure,
            urgency=Decimal("1"),
            band="watch",
            rank=1,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-triage",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def forecast(self) -> Forecast:
        row = Forecast(
            id=uuid4(),
            run_id=self.run.id,
            covenant_version_id=self.version.id,
            horizon_days=90,
            probability=Decimal("0.5825"),
            confidence=Decimal("0.90"),
            below_confidence_floor=False,
            projected_cross_date=date(2026, 11, 29),
            direction="max",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-forecast",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def test(
        self,
        *,
        verdict: str = "warning",
        value: Decimal | None = Decimal("2.80"),
        headroom: Decimal | None = Decimal("13.8462"),
        reason: str | None = None,
        inputs: dict[str, object] | None = None,
    ) -> CovenantTest:
        row = CovenantTest(
            id=uuid4(),
            covenant_version_id=self.version.id,
            as_of_date=_AS_OF,
            value=value,
            threshold_used=Decimal("3.25"),
            headroom_pct=headroom,
            verdict=verdict,
            inputs=inputs,
            not_computable_reason=reason,
            computed_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-test",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def schedule(self) -> CovenantSchedule:
        row = CovenantSchedule(
            id=uuid4(),
            covenant_version_id=self.version.id,
            due_date=date(2026, 12, 31),
            state="pending",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-schedule",
        )
        self.session.add(row)
        self.session.flush()
        return row


def test_header_holds_exactly_four_facts() -> None:
    fixture = _Fixture()
    try:
        fixture.triage()
        fixture.forecast()
        fixture.test()
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        assert response.status_code == 200
        assert response.text.count('class="case-header__fact"') == 4
        for label in ("Borrower", "Exposure", "Worst covenant", "Dated risk"):
            assert label in response.text
    finally:
        fixture.close()


def test_covenant_rows_show_value_threshold_headroom_verdict() -> None:
    fixture = _Fixture()
    try:
        fixture.triage()
        fixture.forecast()
        fixture.test()
        fixture.schedule()
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        assert "2.8x" in body
        assert "3.25x" in body
        assert "13.8462%" in body
        assert "Warning" in body
        assert "31 Dec 2026" in body
        assert "↑" in body
    finally:
        fixture.close()


def test_stale_row_states_last_period() -> None:
    fixture = _Fixture()
    try:
        fixture.triage()
        fixture.test(
            verdict="stale",
            value=None,
            headroom=None,
            inputs={
                "period_label": "Q3 FY26",
                "reason_context": {"last_complete_period": "Q2 FY26"},
                "confidence_reduction_pct": "20",
            },
        )
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert "Stale" in body
        assert "Last complete period: Q2 FY26" in body
        assert "Confidence reduction: 20%" in body
        row = body.split('id="covenant-row-', 1)[1].split("</tr>", 1)[0]
        assert "Value unavailable" in row
        assert "Headroom unavailable" in row
    finally:
        fixture.close()


def test_not_computable_row_shows_reason() -> None:
    fixture = _Fixture()
    try:
        fixture.test(
            verdict="not_computable",
            value=None,
            headroom=None,
            reason="required financial period is missing",
        )
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert "Not computable" in body
        assert "required financial period is missing" in body
        assert "None" not in body
    finally:
        fixture.close()


def test_view_computes_no_figure() -> None:
    fixture = _Fixture()
    try:
        fixture.triage(exposure=Decimal("624000000"))
        fixture.test(value=Decimal("2.80"), headroom=Decimal("13.8462"))
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        assert response.status_code == 200
        assert "₹62.4 crore" in response.text
        assert "2.8x" in response.text
        assert "13.8462%" in response.text
    finally:
        fixture.close()


def test_unknown_borrower_404() -> None:
    fixture = _Fixture()
    try:
        with fixture.client() as client:
            response = client.get("/borrowers/B-DOES-NOT-EXIST")
        assert response.status_code == 404
    finally:
        fixture.close()


def test_out_of_scope_borrower_404() -> None:
    fixture = _Fixture()
    try:
        hidden_portfolio = Portfolio.create(
            code="HIDDEN",
            name="Hidden portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-hidden-portfolio",
        )
        hidden = Borrower(
            id=uuid4(),
            reference="B-T075-HIDDEN",
            legal_name="Hidden Borrower",
            portfolio_id=hidden_portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t075-hidden-borrower",
        )
        fixture.session.add_all([hidden_portfolio, hidden])
        fixture.session.flush()
        with fixture.client() as client:
            response = client.get(f"/borrowers/{hidden.reference}")
        assert response.status_code == 404
    finally:
        fixture.close()


__all__ = ["_Fixture"]
