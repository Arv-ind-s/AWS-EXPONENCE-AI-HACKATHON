"""Integration coverage for T-112 dispositions and labelled exports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    Covenant,
    CovenantVersion,
    Disposition,
    Facility,
    Forecast,
    ForecastRun,
    Portfolio,
    UserPortfolioScope,
)
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.dispositions import DispositionService
from covenant_radar.services.labelled_export import LabelledExportService
from covenant_radar.web.routes.dispositions import create_dispositions_router

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []

    def record(
        self,
        event_type: str,
        _subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append(
            (
                event_type,
                {
                    **payload,
                    "actor": actor,
                    "request_id": request_id,
                },
            )
        )
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.principal = Principal.user(
            uuid4(),
            (Permission.RECORD_DISPOSITION, Permission.VIEW_BORROWER),
        )
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="t112-user",
                email="t112-user@example.com",
                full_name="T112 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t112-user",
            )
        )
        self.portfolio = self._portfolio("T112")
        self.borrower = self._borrower("B-T112", self.portfolio, "Meridian Auto Components")
        self.session.add(
            UserPortfolioScope(
                user_id=self.principal.id,
                portfolio_id=self.portfolio.id,
                include_descendants=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t112-scope",
            )
        )
        self.facility = Facility(
            id=new_id(),
            reference="F-T112",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t112-facility",
        )
        self.session.add(self.facility)
        self.session.flush()
        self.covenant = Covenant(
            id=new_id(),
            reference="CV-T112",
            facility_id=self.facility.id,
            name="Leverage ratio",
            covenant_class="financial",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t112-covenant",
        )
        self.session.add(self.covenant)
        self.session.flush()
        self.version = CovenantVersion(
            id=new_id(),
            covenant_id=self.covenant.id,
            version_no=1,
            threshold=Decimal("3.25"),
            direction="max",
            unit="x",
            frequency="quarterly",
            test_basis="standalone",
            effective_from=date(2025, 1, 1),
            status="live",
            registered_by_id=self.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t112-version",
        )
        self.session.add(self.version)
        self.session.flush()
        self.run = ForecastRun(
            id=new_id(),
            as_of_date=date(2026, 8, 31),
            started_at=_NOW,
            finished_at=_NOW,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t112-run",
        )
        self.session.add(self.run)
        self.session.flush()
        self.forecast = self._forecast("rq-t112-forecast", 30)
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.service = DispositionService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t112-service",
            scope_resolver=lambda _principal: self.scope,
        )
        self.export_service = LabelledExportService(
            self.session,
            scope_resolver=lambda _principal: self.scope,
        )

    def _portfolio(self, code: str) -> Portfolio:
        portfolio = Portfolio.create(
            code=code,
            name=f"Portfolio {code}",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t112-portfolio-{code.lower()}",
        )
        self.session.add(portfolio)
        self.session.flush()
        return portfolio

    def _borrower(self, reference: str, portfolio: Portfolio, legal_name: str) -> Borrower:
        borrower = Borrower(
            id=new_id(),
            reference=reference,
            legal_name=legal_name,
            cin_enc="encrypted-cin-value",
            pan_enc="encrypted-pan-value",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t112-borrower-{reference.lower()}",
        )
        self.session.add(borrower)
        self.session.flush()
        return borrower

    def _forecast(self, request_id: str, horizon_days: int) -> Forecast:
        forecast = Forecast(
            id=new_id(),
            run_id=self.run.id,
            covenant_version_id=self.version.id,
            horizon_days=horizon_days,
            probability=Decimal("0.6500"),
            confidence=Decimal("0.9000"),
            below_confidence_floor=False,
            projected_cross_date=date(2026, 10, 1),
            direction="max",
            data_as_of=date(2026, 8, 31),
            staleness_days=0,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=request_id,
        )
        self.session.add(forecast)
        self.session.flush()
        return forecast

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def fixture() -> _Fixture:
    value = _Fixture()
    try:
        yield value
    finally:
        value.close()


def test_dismissal_requires_reason_code(fixture: _Fixture) -> None:
    with pytest.raises(ValidationError, match="reason code is required"):
        fixture.service.record_disposition(
            fixture.principal,
            ("forecast", fixture.forecast.id),
            outcome="dismissed",
            scope=fixture.scope,
        )

    assert fixture.session.scalar(select(func.count(Disposition.id))) == 0
    assert fixture.audit.events == []


def test_change_retains_sequence(fixture: _Fixture) -> None:
    first = fixture.service.record_disposition(
        fixture.principal,
        ("forecast", fixture.forecast.id),
        outcome="monitoring",
        reason_code="monitoring_only",
        note="Continue observation.",
        scope=fixture.scope,
    )
    second = fixture.service.record_disposition(
        fixture.principal,
        ("forecast", fixture.forecast.id),
        outcome="acted",
        reason_code="action_taken",
        scope=fixture.scope,
    )

    history = fixture.service.list_dispositions(
        fixture.principal,
        ("forecast", fixture.forecast.id),
        scope=fixture.scope,
    )
    assert tuple(item.id for item in history) == (first.id, second.id)
    assert tuple(event[1]["workflow_event"] for event in fixture.audit.events) == (
        "disposition_recorded",
        "disposition_recorded",
    )


def test_export_has_no_personal_value(fixture: _Fixture) -> None:
    fixture.service.record_disposition(
        fixture.principal,
        ("forecast", fixture.forecast.id),
        outcome="dismissed",
        reason_code="false_positive",
        note="Discussed with Ananya Sharma; PAN ABCDE1234F.",
        scope=fixture.scope,
    )

    exported = fixture.export_service.export(fixture.principal, scope=fixture.scope)
    serialised = exported.content.decode("utf-8")
    assert "Meridian Auto Components" not in serialised
    assert "encrypted-cin-value" not in serialised
    assert "encrypted-pan-value" not in serialised
    assert "Ananya Sharma" not in serialised
    assert "reason_code" in exported.rows[0]
    assert "legal_name" not in exported.rows[0]


def test_unlabelled_warnings_present(fixture: _Fixture) -> None:
    unlabelled = fixture._forecast("rq-t112-unlabelled", 60)
    exported = fixture.export_service.export(fixture.principal, scope=fixture.scope)

    rows_by_id = {row["warning_id"]: row for row in exported}
    assert set(rows_by_id) == {str(fixture.forecast.id), str(unlabelled.id)}
    assert rows_by_id[str(unlabelled.id)]["label"] == "unlabelled"
    assert rows_by_id[str(unlabelled.id)]["outcome"] is None


def test_export_scoped(fixture: _Fixture) -> None:
    hidden_portfolio = fixture._portfolio("T112-HIDDEN")
    hidden_borrower = fixture._borrower("B-T112-HIDDEN", hidden_portfolio, "Hidden Borrower")
    hidden_facility = Facility(
        id=new_id(),
        reference="F-T112-HIDDEN",
        borrower_id=hidden_borrower.id,
        facility_type="term_loan",
        sanctioned_limit=Decimal("1000000"),
        currency="INR",
        sanction_date=date(2025, 1, 1),
        effective_from=date(2025, 1, 1),
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t112-hidden-facility",
    )
    fixture.session.add(hidden_facility)
    fixture.session.flush()
    hidden_covenant = Covenant(
        id=new_id(),
        reference="CV-T112-HIDDEN",
        facility_id=hidden_facility.id,
        name="Hidden ratio",
        covenant_class="financial",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t112-hidden-covenant",
    )
    fixture.session.add(hidden_covenant)
    fixture.session.flush()
    hidden_version = CovenantVersion(
        id=new_id(),
        covenant_id=hidden_covenant.id,
        version_no=1,
        threshold=Decimal("3.25"),
        direction="max",
        unit="x",
        frequency="quarterly",
        test_basis="standalone",
        effective_from=date(2025, 1, 1),
        status="live",
        registered_by_id=fixture.principal.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t112-hidden-version",
    )
    fixture.session.add(hidden_version)
    fixture.session.flush()
    hidden_forecast = Forecast(
        id=new_id(),
        run_id=fixture.run.id,
        covenant_version_id=hidden_version.id,
        horizon_days=30,
        probability=Decimal("0.7000"),
        confidence=Decimal("0.9000"),
        below_confidence_floor=False,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t112-hidden-forecast",
    )
    fixture.session.add(hidden_forecast)
    fixture.session.flush()

    exported = fixture.export_service.export(fixture.principal, scope=fixture.scope)
    assert len(exported) == 1
    assert exported.rows[0]["warning_id"] == str(fixture.forecast.id)
    with pytest.raises(NotFound):
        fixture.service.record_disposition(
            fixture.principal,
            ("forecast", hidden_forecast.id),
            outcome="monitoring",
            reason_code="monitoring_only",
            scope=fixture.scope,
        )


def test_control_on_both_surfaces(fixture: _Fixture) -> None:
    app = create_app(
        routers=(create_dispositions_router(fixture.service),),
        principal_resolver=lambda _request: fixture.principal,
    )
    with TestClient(app) as client:
        case_file = client.get(
            f"/dispositions/form/forecast/{fixture.forecast.id}",
            params={"surface": "case-file"},
        )
        memo = client.get(
            f"/dispositions/form/forecast/{fixture.forecast.id}",
            params={"surface": "memo"},
        )
        submitted = client.post(
            "/dispositions",
            data={
                "subject_type": "forecast",
                "subject_id": str(fixture.forecast.id),
                "outcome": "monitoring",
                "reason_code": "monitoring_only",
            },
        )

    assert case_file.status_code == 200
    assert memo.status_code == 200
    assert submitted.status_code == 204
    assert 'id="disposition-control"' in case_file.text
    assert 'data-surface="case-file"' in case_file.text
    assert 'data-surface="memo"' in memo.text
    assert 'name="outcome"' in case_file.text
    assert 'name="reason_code"' in memo.text


__all__ = []
