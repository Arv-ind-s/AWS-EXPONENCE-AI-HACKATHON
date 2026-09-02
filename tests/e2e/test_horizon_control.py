"""Browser-facing contracts for T-077's horizon control.

The repository's browser harness is intentionally offline at this phase.  The
checks below assert the rendered DOM and the shipped vanilla module's
interaction contract; the full Playwright recording remains a release-gate
test once the browser fixture is enabled.
"""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from covenant_radar.api.v1.routers.forecast import create_forecast_router
from covenant_radar.asgi import create_app
from covenant_radar.web.routes.borrower import create_borrower_router
from tests.integration.test_case_file import _Fixture
from tests.integration.test_forecast_panel import _forecast, _path

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HORIZON_JS = PROJECT_ROOT / "src" / "covenant_radar" / "web" / "static" / "js" / "horizon.js"
HORIZON_CSS = PROJECT_ROOT / "src" / "covenant_radar" / "web" / "static" / "css" / "horizon.css"


class _LinkParser(HTMLParser):
    """Collect fallback horizon links without relying on a browser runtime."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stops: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        stop = values.get("data-horizon-stop")
        href = values.get("href")
        if stop is not None and href is not None:
            self.stops.append((stop, href))


def _body() -> str:
    fixture = _Fixture()
    try:
        _forecast(fixture, 30)
        _forecast(fixture, 60, crossing_date=date(2026, 10, 29), crossing_day=60)
        _forecast(fixture, 90, crossing_date=date(2026, 10, 29), crossing_day=60)
        _path(fixture)
        app = create_app(
            routers=(
                create_borrower_router(fixture.session),
                create_forecast_router(fixture.session),
            ),
            principal_resolver=lambda _request: fixture.principal,
        )
        with TestClient(app) as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")
        assert response.status_code == 200
        return response.text
    finally:
        fixture.close()


def test_tick_appears_with_date_at_crossing() -> None:
    body = _body()
    source = HORIZON_JS.read_text(encoding="utf-8")

    assert 'data-crossing-date="29 Oct 2026"' in body
    assert "Crossing:" in body
    assert 'class: "trajectory__crossing-tick"' in source
    assert "crossingDayFor" in source


def test_keyboard_model_complete() -> None:
    body = _body()
    source = HORIZON_JS.read_text(encoding="utf-8")

    assert 'type="range"' in body
    assert 'aria-controls="forecast-trajectory-' in body
    for key in ("ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "Home", "End"):
        assert f'event.key === "{key}"' in source
    assert "event.shiftKey ? 7 : 1" in source
    assert "data-horizon-stop" in body


def test_reduced_motion_uses_stops() -> None:
    body = _body()
    source = HORIZON_JS.read_text(encoding="utf-8")
    stylesheet = HORIZON_CSS.read_text(encoding="utf-8")

    assert 'data-horizon-mode="stops"' in body
    assert "prefers-reduced-motion: reduce" in source
    assert "prefers-reduced-motion: reduce" in stylesheet
    assert 'data-horizon-mode="interactive"' in stylesheet
    assert "data-horizon-mode = stops" not in source


def test_no_javascript_stops_are_links() -> None:
    body = _body()
    parser = _LinkParser()
    parser.feed(body)
    parser.close()

    assert [stop for stop, _href in parser.stops] == ["0", "30", "60", "90"]
    assert all(href.startswith(f"/borrowers/B-T075?day={stop}") for stop, href in parser.stops)
    # The application shell now contains menu and theme buttons; the
    # no-JavaScript horizon choices themselves must remain ordinary links.
    assert len(parser.stops) == 4
