"""Performance contract for one stored horizon step."""

from __future__ import annotations

from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from covenant_radar.api.v1.routers.forecast import create_forecast_router
from covenant_radar.asgi import create_app
from tests.integration.test_case_file import _Fixture
from tests.integration.test_forecast_panel import _forecast, _path

pytestmark = pytest.mark.perf


def test_step_within_budget() -> None:
    fixture = _Fixture()
    try:
        _forecast(fixture, 90)
        _path(fixture)
        app = create_app(
            routers=(create_forecast_router(fixture.session),),
            principal_resolver=lambda _request: fixture.principal,
        )
        with TestClient(app) as client:
            endpoint = f"/api/v1/forecasts/{fixture.covenant.reference}/path?day=30"
            client.get(endpoint)
            durations = []
            for _ in range(5):
                started = perf_counter()
                response = client.get(endpoint)
                durations.append(perf_counter() - started)
                assert response.status_code == 200

        assert max(durations) <= 0.100, f"horizon step exceeded 100 ms: {max(durations):.4f}s"
    finally:
        fixture.close()
