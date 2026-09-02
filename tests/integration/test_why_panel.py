"""Integration coverage for T-071's why-panel drawer route."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastRun
from covenant_radar.db.models.identity import AppUser, UserPortfolioScope
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.trace import TraceRepository, TraceSubject
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.evaluate import PeriodFacts
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.domain.ratios.compute import RatioResult
from covenant_radar.domain.trace import stage_record
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.engine import EngineService
from covenant_radar.services.registry import RegistryService
from covenant_radar.web.routes.why import create_why_router

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


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


class _Bundle:
    """A scoped borrower with a live covenant, a real stage-2 covenant test
    and a real forecast row, wired for the why-panel route under test."""

    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()

        self.principal = Principal.user(
            uuid4(),
            (Permission.VIEW_BORROWER, Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT),
        )
        self.portfolio = Portfolio.create(
            code="WHY-ROOT",
            name="Why-panel root portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t071-portfolio",
        )
        self.session.add(self.portfolio)
        self.session.flush()
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="t071-user",
                email="t071-user@example.com",
                full_name="T071 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t071-user",
            )
        )
        self.session.add(
            UserPortfolioScope(
                user_id=self.principal.id,
                portfolio_id=self.portfolio.id,
                include_descendants=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t071-user-scope",
            )
        )
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])

        self.borrower = Borrower(
            reference="B-T071",
            legal_name="T071 Why-Panel Borrower Private Limited",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t071-borrower",
        )
        self.session.add(self.borrower)
        self.session.flush()
        self.facility = Facility(
            reference="F-T071",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            outstanding=Decimal("700"),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t071-facility",
        )
        self.session.add(self.facility)
        self.session.flush()

        registry = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t071-registry",
            maker_checker_enabled=False,
        )
        terms = CovenantVersionTerms(
            definition_ref="leverage_ratio",
            custom_formula=None,
            threshold=Decimal("2.5"),
            direction="max",
            unit="x",
            frequency="quarterly",
            test_basis="standalone",
            effective_from=date(2025, 1, 1),
        )
        registered = registry.register(
            self.principal,
            facility_id=self.facility.id,
            reference="CV-T071",
            name="Leverage ratio",
            covenant_class="financial",
            terms=terms,
            scope=self.scope,
        )
        self.covenant_version = registered.version

        engine = EngineService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t071-engine",
            scope_resolver=lambda _principal: self.scope,
        )
        self.covenant_test = engine.test(
            self.principal,
            covenant_version_id=self.covenant_version.id,
            period=PeriodFacts(
                period_id=new_id(),
                period_label="FY26Q4",
                as_of_date=date(2026, 1, 15),
            ),
            ratio=RatioResult(
                code="leverage_ratio",
                value=Decimal("3.0"),
                computable=True,
                reason=None,
                inputs_used={
                    "total_debt": Decimal("750"),
                    "tangible_net_worth": Decimal("250"),
                },
                band_breached=False,
            ),
            scope=self.scope,
        )

        self.forecast_run = ForecastRun(
            as_of_date=date(2026, 1, 15),
            started_at=_NOW,
            finished_at=_NOW,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t071-forecast-run",
        )
        self.session.add(self.forecast_run)
        self.session.flush()
        self.forecast = Forecast(
            run_id=self.forecast_run.id,
            covenant_version_id=self.covenant_version.id,
            horizon_days=30,
            probability=None,
            confidence=Decimal("0.20"),
            below_confidence_floor=True,
            direction="max",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t071-forecast",
        )
        self.session.add(self.forecast)
        self.session.flush()

        self.traces = TraceRepository(self.session, clock=FixedClock(_NOW))

    def write_model_stage(self) -> None:
        """Attach a synthetic model-decided trace row under the borrower.

        No real stage writes a model decision yet (`T-070`'s reader must
        still present one uniformly with a code stage), so this builds one
        by hand using the exact shared shape `domain.trace.stage_record`
        enforces — the same shape a future model-writing stage would use.
        """
        record = stage_record(
            1,
            "model",
            {"clause_text": "Borrower shall maintain minimum leverage ratio of 2.5x."},
            {
                "response_text": "The clause proposes a leverage ratio covenant at 2.5x, "
                "tested quarterly on a standalone basis.",
                "code_verdict": "verified",
            },
            "intake-prompt-v3",
            [],
            Decimal("0.91"),
            [],
        )
        self.traces.write(
            TraceSubject("borrower", self.borrower.id),
            record,
            occurred_at=_NOW,
        )

    def write_suppressed_forecast_stage(self) -> None:
        record = stage_record(
            4,
            "code",
            {
                "confidence_factors": [
                    {
                        "name": "data_completeness",
                        "value": Decimal("0.20"),
                        "description": "Only two of eight required quarters are on file.",
                    },
                    {
                        "name": "evidence_support",
                        "value": Decimal("0.80"),
                        "description": "Evidence persistence is well established.",
                    },
                ]
            },
            {
                "probability_suppressed": True,
                "below_confidence_floor": True,
                "reason": "Confidence fell below the floor required to show a probability.",
            },
            "forecast-rule-v1",
            [
                {
                    "name": "T2.confidence_floor",
                    "value": Decimal("0.50"),
                    "observed": Decimal("0.20"),
                    "side": "below",
                }
            ],
            Decimal("0.20"),
            [{"type": "forecast", "id": str(self.forecast.id)}],
        )
        self.traces.write(
            TraceSubject("forecast", self.forecast.id),
            record,
            occurred_at=_NOW,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def _client(bundle: _Bundle, *, principal: Principal | None = None) -> TestClient:
    app = create_app(
        routers=(create_why_router(bundle.session),),
        principal_resolver=lambda _request: principal or bundle.principal,
    )
    return TestClient(app)


def test_every_stage_section_present_in_order() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle) as client:
            response = client.get(f"/why/covenant_test/{bundle.covenant_test.id}")

        assert response.status_code == 200
        body = response.text
        expected_names = (
            "Intake",
            "Covenant Engine",
            "Evidence Ledger",
            "Forecast",
            "Intervention",
            "Triage",
            "Memo",
        )
        positions = [body.index(name) for name in expected_names]
        assert positions == sorted(positions)
        assert 'data-state="code"' in body
    finally:
        bundle.close()


def test_not_run_section_shows_only_that() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle) as client:
            response = client.get(f"/why/borrower/{bundle.borrower.id}")

        assert response.status_code == 200
        body = response.text
        assert body.count("This stage has not run.") == 7
        assert "What it received" not in body
        assert "What it produced" not in body
        assert "Thresholds compared" not in body
    finally:
        bundle.close()


def test_why_panel_offers_the_grounded_ai_explanation_path() -> None:
    bundle = _Bundle()
    try:
        principal = Principal.user(
            bundle.principal.id,
            (Permission.VIEW_BORROWER, Permission.GENERATE_MEMO),
        )
        with _client(bundle, principal=principal) as client:
            response = client.get(f"/why/borrower/{bundle.borrower.id}")

        assert response.status_code == 200
        assert "No AI explanation has been generated yet" in response.text
        assert "Generate AI explanation" in response.text
        assert f'href="/borrowers/{bundle.borrower.reference}#case-memo"' in response.text
    finally:
        bundle.close()


def test_threshold_table_shows_value_observed_and_side() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle) as client:
            response = client.get(f"/why/covenant_test/{bundle.covenant_test.id}")

        assert response.status_code == 200
        body = response.text
        assert '<table class="why-threshold-table">' in body
        assert '<th scope="col">Threshold</th>' in body
        assert '<th scope="col">Value</th>' in body
        assert '<th scope="col">Observed</th>' in body
        assert '<th scope="col">Side</th>' in body
        assert "Above" in body
    finally:
        bundle.close()


def test_model_stage_shows_prompt_version_and_verdict() -> None:
    bundle = _Bundle()
    try:
        bundle.write_model_stage()
        with _client(bundle) as client:
            response = client.get(f"/why/borrower/{bundle.borrower.id}")

        assert response.status_code == 200
        body = response.text
        assert "Prompt version" in body
        assert "intake-prompt-v3" in body
        assert "verified" in body
        assert (
            "The clause proposes a leverage ratio covenant at 2.5x, "
            "tested quarterly on a standalone basis." in body
        )
        assert 'class="why-model-text"' in body
    finally:
        bundle.close()


def test_suppressed_forecast_explained_with_limiting_factor() -> None:
    bundle = _Bundle()
    try:
        bundle.write_suppressed_forecast_stage()
        with _client(bundle) as client:
            response = client.get(f"/why/forecast/{bundle.forecast.id}")

        assert response.status_code == 200
        body = response.text
        assert "This forecast is suppressed." in body
        assert "Confidence fell below the floor required to show a probability." in body
        assert "Limiting confidence factor: data_completeness." in body
        assert "Decision summary" in body
        assert "deterministic rule" in body
        assert "Reason this result was produced" in body
        assert f'href="/api/v1/forecasts/{bundle.forecast.id}"' in body
    finally:
        bundle.close()


def test_out_of_scope_subject_404() -> None:
    bundle = _Bundle()
    try:
        hidden_portfolio = Portfolio.create(
            code="WHY-HIDDEN",
            name="Why-panel hidden portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t071-hidden-portfolio",
        )
        hidden_borrower = Borrower(
            id=uuid4(),
            reference="B-T071-HIDDEN",
            legal_name="Hidden Why-Panel Borrower",
            portfolio_id=hidden_portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t071-hidden-borrower",
        )
        bundle.session.add_all([hidden_portfolio, hidden_borrower])
        bundle.session.flush()

        with _client(bundle) as client:
            response = client.get(f"/why/borrower/{hidden_borrower.id}")

        assert response.status_code == 404
    finally:
        bundle.close()


def test_unknown_subject_type_is_malformed() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle) as client:
            response = client.get(f"/why/not-a-subject/{uuid4()}")

        assert response.status_code == 400
    finally:
        bundle.close()


def test_malformed_subject_id_is_malformed() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle) as client:
            response = client.get("/why/borrower/not-a-uuid")

        assert response.status_code == 400
    finally:
        bundle.close()


def test_principal_lacking_permission_is_refused() -> None:
    bundle = _Bundle()
    try:
        unprivileged = Principal.user(uuid4(), ())
        with _client(bundle, principal=unprivileged) as client:
            response = client.get(f"/why/borrower/{bundle.borrower.id}")
        assert response.status_code == 403
    finally:
        bundle.close()
