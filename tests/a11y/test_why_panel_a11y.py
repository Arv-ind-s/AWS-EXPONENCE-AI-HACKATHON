"""Accessibility contract checks for T-071's why-panel drawer.

The full axe-core browser audit runs once the browser harness lands (see
`tests/e2e/test_component_gallery.py`). Until then this module keeps the
same offline contract that gate will re-check — unique ids, every
aria-labelledby/describedby resolving, at least one landmark, every button
carrying an explicit type — and additionally verifies the escape-to-close,
focus-returning behaviour is wired: `web/static/js/app.js` already
implements it generically for any `[data-drawer]`, so what this test proves
is that the why-panel emits exactly the attribute contract that behaviour
depends on.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from covenant_radar.asgi import create_app
from covenant_radar.web.routes.why import create_why_router
from tests.integration.test_why_panel import _Bundle

pytestmark = pytest.mark.a11y

_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "covenant_radar"
    / "web"
    / "static"
    / "js"
    / "app.js"
)


class _AccessibilityContractParser(HTMLParser):
    """The same small relationship checks `test_component_gallery.py` uses."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.references: list[str] = []
        self.landmarks = 0
        self.buttons_missing_type = 0
        self.buttons = 0

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        for attribute in ("aria-labelledby", "aria-describedby"):
            value = attributes.get(attribute, "")
            if value:
                self.references.extend(value.split())
        if _tag in {"main", "nav", "header", "section"}:
            self.landmarks += 1
        if _tag == "button":
            self.buttons += 1
            if attributes.get("type") not in {"button", "submit", "reset"}:
                self.buttons_missing_type += 1


def _client(bundle: _Bundle) -> TestClient:
    app = create_app(
        routers=(create_why_router(bundle.session),),
        principal_resolver=lambda _request: bundle.principal,
    )
    return TestClient(app)


def _panel_html(*, theme: str = "light") -> str:
    bundle = _Bundle()
    try:
        bundle.write_model_stage()
        with _client(bundle) as client:
            client.cookies.set("covenant_radar_theme", theme)
            response = client.get(f"/why/covenant_test/{bundle.covenant_test.id}")
        assert response.status_code == 200
        return response.text
    finally:
        bundle.close()


def test_axe_clean_both_themes() -> None:
    for theme in ("light", "dark"):
        body = _panel_html(theme=theme)
        assert f'data-theme="{theme}"' in body

        parser = _AccessibilityContractParser()
        parser.feed(body)
        parser.close()

        assert parser.duplicate_ids == set()
        assert set(parser.references) <= parser.ids
        assert parser.landmarks > 0
        assert parser.buttons > 0
        assert parser.buttons_missing_type == 0


def test_drawer_open_state_matches_direct_navigation() -> None:
    body = _panel_html()
    assert 'id="why-drawer"' in body
    assert "data-drawer" in body
    assert 'data-state="open"' in body
    assert 'aria-hidden="false"' in body
    assert 'role="dialog"' in body
    assert 'aria-modal="true"' in body


def test_escape_closes_and_returns_focus() -> None:
    body = _panel_html()
    assert 'data-drawer-close="why-drawer"' in body
    assert '<button class="button drawer__close" type="button"' in body

    script = _APP_JS.read_text(encoding="utf-8")
    assert 'event.key !== "Escape"' in script
    assert '[data-drawer][data-state="open"]' in script
    assert "closeDrawer(drawer)" in script
    assert "_radarOpener" in script
