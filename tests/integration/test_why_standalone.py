"""Integration coverage for T-072's three why-panel representations."""

from __future__ import annotations

from html.parser import HTMLParser
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from covenant_radar.api.v1.routers.explain import create_explain_router
from covenant_radar.asgi import create_app
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.why import create_why_router
from tests.integration.test_why_panel import _Bundle

pytestmark = pytest.mark.integration


class _DrawerText(HTMLParser):
    """Extract visible text under the drawer without a parser dependency."""

    _VOID_ELEMENTS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._VOID_ELEMENTS:
            return
        inside_drawer = bool(self._stack and self._stack[-1][1]) or any(
            name == "data-drawer" for name, _value in attrs
        )
        self._stack.append((tag, inside_drawer))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1][1]:
            normalized = " ".join(data.split())
            if normalized:
                self.parts.append(normalized)


def _drawer_text(document: str) -> str:
    parser = _DrawerText()
    parser.feed(document)
    parser.close()
    return " ".join(parser.parts)


def _client(bundle: _Bundle, *, principal: Principal | None = None) -> TestClient:
    app = create_app(
        routers=(
            create_why_router(bundle.session),
            create_explain_router(bundle.session),
        ),
        principal_resolver=lambda _request: principal or bundle.principal,
    )
    return TestClient(app)


def test_full_page_matches_drawer_content() -> None:
    bundle = _Bundle()
    try:
        path = f"/why/covenant_test/{bundle.covenant_test.id}"
        with _client(bundle) as client:
            page = client.get(path)
            fragment = client.get(path, headers={"HX-Request": "true"})

        assert page.status_code == 200
        assert fragment.status_code == 200
        assert "<html" in page.text
        assert "<html" not in fragment.text
        assert _drawer_text(page.text) == _drawer_text(fragment.text)
        assert page.headers["vary"] == "HX-Request"
        assert fragment.headers["vary"] == "HX-Request"
    finally:
        bundle.close()


def test_no_javascript_renders_everything() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle) as client:
            response = client.get(f"/why/covenant_test/{bundle.covenant_test.id}")

        assert response.status_code == 200
        assert response.text.count('data-stage="') == 7
        assert all(name in response.text for name in ("Intake", "Covenant Engine", "Memo"))
        assert "This stage has not run." in response.text
    finally:
        bundle.close()


def test_api_matches_page() -> None:
    bundle = _Bundle()
    try:
        path = f"/why/covenant_test/{bundle.covenant_test.id}"
        with _client(bundle) as client:
            page = client.get(path)
            api = client.get(f"/api/v1/explain/covenant_test/{bundle.covenant_test.id}")

        assert page.status_code == 200
        assert api.status_code == 200
        payload = api.json()
        assert payload["subject_type"] == "covenant_test"
        assert payload["subject_id"] == str(bundle.covenant_test.id)
        assert [stage["name"] for stage in payload["stages"]] == [
            "Intake",
            "Covenant Engine",
            "Evidence Ledger",
            "Forecast",
            "Intervention",
            "Triage",
            "Memo",
        ]
        assert payload["stages"][1]["thresholds_compared"] == [
            {
                "name": "covenant_threshold",
                "value": "2.5",
                "observed": "3.0",
                "side": "above",
            }
        ]
        for stage in payload["stages"]:
            assert stage["name"] in page.text
            if not stage["not_run"]:
                assert stage["rule_or_prompt_version"] in page.text
    finally:
        bundle.close()


def test_scope_enforced_on_both_surfaces() -> None:
    bundle = _Bundle()
    try:
        hidden_portfolio = Portfolio.create(
            code="WHY-T072-HIDDEN",
            name="T072 hidden portfolio",
            created_at=bundle.portfolio.created_at,
            updated_at=bundle.portfolio.updated_at,
            request_id="rq-t072-hidden-portfolio",
        )
        # Constructing through the model keeps this test independent of the
        # scope resolver's internal joins; the hidden borrower is never added
        # to the authenticated user's portfolio grant.
        hidden_borrower = Borrower(
            id=uuid4(),
            reference="B-T072-HIDDEN",
            legal_name="Hidden T072 borrower",
            portfolio_id=hidden_portfolio.id,
            created_at=bundle.portfolio.created_at,
            updated_at=bundle.portfolio.updated_at,
            request_id="rq-t072-hidden-borrower",
        )
        bundle.session.add_all([hidden_portfolio, hidden_borrower])
        bundle.session.flush()

        with _client(bundle) as client:
            page = client.get(f"/why/borrower/{hidden_borrower.id}")
            api = client.get(f"/api/v1/explain/borrower/{hidden_borrower.id}")

        assert page.status_code == 404
        assert api.status_code == 404
    finally:
        bundle.close()
