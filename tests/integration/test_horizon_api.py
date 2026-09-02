"""Integration coverage for T-077's scoped stored-path API (C-03)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from covenant_radar.api.v1.routers.forecast import create_forecast_router
from covenant_radar.asgi import create_app
from covenant_radar.db.models.forecast import ForecastDriver, ForecastPath
from covenant_radar.web.routes.borrower import create_borrower_router
from tests.integration.test_case_file import _NOW, _Fixture
from tests.integration.test_forecast_panel import _forecast

pytestmark = pytest.mark.integration


def _app(fixture: _Fixture) -> FastAPI:
    app = create_app(
        routers=(
            create_borrower_router(fixture.session),
            create_forecast_router(fixture.session),
        ),
        principal_resolver=lambda _request: fixture.principal,
    )
    return app


def _path(fixture: _Fixture, *, maximum_day: int = 90) -> None:
    for day_offset in range(maximum_day + 1):
        fixture.session.add(
            ForecastPath(
                id=uuid4(),
                run_id=fixture.run.id,
                covenant_version_id=fixture.version.id,
                day_offset=day_offset,
                projected_value=Decimal("2.80") + Decimal(day_offset) / Decimal("100"),
                headroom_pct=Decimal("13.8462") - Decimal(day_offset) / Decimal("10"),
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t077-api-path",
            )
        )
    fixture.session.flush()


def test_payload_has_every_field() -> None:
    fixture = _Fixture()
    try:
        forecast = _forecast(fixture, 30, crossing_date=date(2026, 9, 29), crossing_day=30)
        fixture.session.add(
            ForecastDriver(
                id=uuid4(),
                forecast_id=forecast.id,
                name="trend",
                share=Decimal("0.7500"),
                is_other=False,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t077-api-driver",
            )
        )
        _path(fixture)

        with TestClient(_app(fixture)) as client:
            response = client.get(f"/api/v1/forecasts/{fixture.covenant.reference}/path?day=30")

        payload = response.json()
        assert response.status_code == 200
        assert set(payload) == {
            "day",
            "projected_value",
            "headroom_pct",
            "probability",
            "confidence",
            "below_confidence_floor",
            "crossing_date",
            "drivers",
        }
        assert payload["day"] == 30
        assert payload["projected_value"] == "3.10000000"
        assert payload["probability"] == "0.5825"
        assert payload["crossing_date"] == "2026-09-29"
        assert payload["drivers"][0]["name"] == "trend"
    finally:
        fixture.close()


def test_day_out_of_range_422() -> None:
    fixture = _Fixture()
    try:
        _forecast(fixture, 90)
        _path(fixture)

        with TestClient(_app(fixture)) as client:
            response = client.get(f"/api/v1/forecasts/{fixture.covenant.reference}/path?day=91")

        assert response.status_code == 422
        assert "0 and 90" in response.text
    finally:
        fixture.close()


def test_unknown_covenant_404() -> None:
    fixture = _Fixture()
    try:
        with TestClient(_app(fixture)) as client:
            response = client.get("/api/v1/forecasts/CV-UNKNOWN/path?day=0")

        assert response.status_code == 404
    finally:
        fixture.close()


def test_no_write_occurs_during_request() -> None:
    fixture = _Fixture()
    statements: list[str] = []

    def record_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lstrip().split(maxsplit=1)[0].upper())

    event.listen(fixture.engine, "before_cursor_execute", record_statement)
    try:
        _forecast(fixture, 90)
        _path(fixture)
        statements.clear()

        with TestClient(_app(fixture)) as client:
            response = client.get(f"/api/v1/forecasts/{fixture.covenant.reference}/path?day=30")

        assert response.status_code == 200
        writes = {
            statement for statement in statements if statement in {"INSERT", "UPDATE", "DELETE"}
        }
        assert not writes
    finally:
        event.remove(fixture.engine, "before_cursor_execute", record_statement)
        fixture.close()


def test_suppressed_forecast_returns_text_not_number() -> None:
    fixture = _Fixture()
    try:
        _forecast(
            fixture,
            30,
            probability=Decimal("0.8125"),
            confidence=Decimal("0.4000"),
            below_floor=True,
        )
        _path(fixture)

        with TestClient(_app(fixture)) as client:
            response = client.get(f"/api/v1/forecasts/{fixture.covenant.reference}/path?day=30")

        payload = response.json()
        assert response.status_code == 200
        assert payload["below_confidence_floor"] is True
        assert payload["probability"] is None
        assert payload["confidence"] == "0.4000"
        assert "0.8125" not in response.text
    finally:
        fixture.close()
