"""Integration coverage for the T-079 simulator screen and C-11 route."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from covenant_radar.asgi import create_app
from covenant_radar.db.models.forecast import Forecast, Intervention
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.simulator import create_simulator_router
from tests.integration.test_case_file import _AS_OF, _NOW, _Fixture

pytestmark = pytest.mark.integration

_WEIGHTS = {"distance": "1", "velocity": "1", "pressure": "1"}


class _SimulatorFixture(_Fixture):
    def __init__(self) -> None:
        super().__init__()
        self.principal = Principal.user(self.principal.id, (Permission.RUN_SIMULATION,))
        self.forecast = Forecast(
            id=uuid4(),
            run_id=self.run.id,
            covenant_version_id=self.version.id,
            horizon_days=30,
            probability=Decimal("0.50"),
            confidence=Decimal("0.90"),
            below_confidence_floor=False,
            direction="max",
            formula_inputs={
                "candidate_inputs": {
                    "series": [
                        {"date": (_AS_OF - timedelta(days=30)).isoformat(), "value": "2.5"},
                        {"date": _AS_OF.isoformat(), "value": "2.8"},
                    ],
                    "pressure": "0.10",
                },
                "probability": {"weights": _WEIGHTS},
            },
            data_as_of=_AS_OF,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t079-forecast",
        )
        self.session.add(self.forecast)
        self.session.flush()

    def add_intervention(
        self,
        code: str,
        *,
        classes: Iterable[str] = ("financial",),
        amount: str = "-0.10",
        assumption: str | None = None,
    ) -> Intervention:
        row = Intervention(
            id=uuid4(),
            code=code,
            role_tag="credit",
            text=f"Action {code}",
            effect_model="level_shift",
            effect_parameters={
                "amount": amount,
                "_assumptions": [assumption or f"Assumption for {code}."],
            },
            applicable_covenant_classes=list(classes),
            requires_approval=False,
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t079-{code.lower()}",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def client(self) -> TestClient:
        app = create_app(
            routers=(create_simulator_router(self.session),),
            principal_resolver=lambda _request: self.principal,
        )
        return TestClient(app)

    def payload(self, codes: object) -> dict[str, object]:
        return {
            "forecast_id": str(self.forecast.id),
            "intervention_code": codes,
            "parameters": {"weights": _WEIGHTS},
        }


def test_only_applicable_offered() -> None:
    fixture = _SimulatorFixture()
    try:
        fixture.add_intervention("APPLICABLE")
        fixture.add_intervention("INAPPLICABLE", classes=("liquidity",))

        with fixture.client() as client:
            response = client.get(f"/simulator/{fixture.forecast.id}")

        assert response.status_code == 200
        assert 'data-intervention-code="APPLICABLE"' in response.text
        assert 'data-intervention-code="INAPPLICABLE"' not in response.text
    finally:
        fixture.close()


def test_forced_inapplicable_refused_with_reason() -> None:
    fixture = _SimulatorFixture()
    try:
        fixture.add_intervention("INAPPLICABLE", classes=("liquidity",))

        with fixture.client() as client:
            response = client.post(
                "/simulations",
                json=fixture.payload("INAPPLICABLE"),
            )

        assert response.status_code == 422
        assert "not applicable" in response.text
        assert "financial" in response.text
    finally:
        fixture.close()


def test_fifth_option_refused() -> None:
    fixture = _SimulatorFixture()
    try:
        codes = [f"OPTION-{number}" for number in range(1, 6)]
        for code in codes:
            fixture.add_intervention(code)

        with fixture.client() as client:
            response = client.post("/simulations", json=fixture.payload(codes))

        assert response.status_code == 422
        assert "At most 4 interventions" in response.text
    finally:
        fixture.close()


def test_zero_effect_distinct_from_inapplicable() -> None:
    fixture = _SimulatorFixture()
    try:
        fixture.add_intervention(
            "ZERO-EFFECT",
            amount="0",
            assumption="The approved change is zero for this test case.",
        )

        with fixture.client() as client:
            response = client.post(
                "/simulations",
                json=fixture.payload("ZERO-EFFECT"),
            )

        assert response.status_code == 200
        assert "No observable effect" in response.text
        assert "0 days" in response.text
        assert "not applicable" not in response.text
    finally:
        fixture.close()


def test_baseline_always_present() -> None:
    fixture = _SimulatorFixture()
    try:
        fixture.add_intervention("OPTION")

        with fixture.client() as client:
            response = client.post("/simulations", json=fixture.payload("OPTION"))

        assert response.status_code == 200
        body = response.text
        comparison = body.split('id="simulator-comparison"', 1)[1]
        assert "Do nothing (baseline)" in comparison
        assert comparison.index("Do nothing (baseline)") < comparison.index("OPTION")
    finally:
        fixture.close()


def test_assumptions_rendered_inline() -> None:
    fixture = _SimulatorFixture()
    try:
        fixture.add_intervention(
            "ASSUMPTION",
            assumption="The executed amendment is effective before the next test date.",
        )

        with fixture.client() as client:
            response = client.get(f"/simulator/{fixture.forecast.id}")

        assert response.status_code == 200
        assert "The executed amendment is effective before the next test date." in response.text
        assert "<details" not in response.text
    finally:
        fixture.close()


def test_simulation_htmx_request_returns_comparison_region() -> None:
    fixture = _SimulatorFixture()
    try:
        fixture.add_intervention("OPTION")
        with fixture.client() as client:
            response = client.post(
                "/simulations",
                json=fixture.payload("OPTION"),
                headers={
                    "HX-Request": "true",
                    "HX-Target": "simulator-comparison-region",
                },
            )

        assert response.status_code == 200
        assert 'id="simulator-comparison-region"' in response.text
        assert "Do nothing (baseline)" in response.text
        assert "<html" not in response.text
        assert response.headers["vary"] == "HX-Request, HX-Target"
    finally:
        fixture.close()
