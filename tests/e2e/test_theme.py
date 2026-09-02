"""Browser-facing contracts for T-082's theme resolution and persistence.

`ThemePreferenceMiddleware` (`web/preferences.py`) resolves the theme before
any template renders, so — as with the rest of this codebase's "e2e" layer
(see `tests/e2e/test_component_gallery.py`) — the no-flash guarantee is
proven at the HTTP boundary: the very first response byte already carries
the resolved `data-theme`, with no client-side script ever changing it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.preferences import (
    THEME_COOKIE,
    ThemePreferenceMiddleware,
    create_preferences_router,
    theme_for_request,
)
from covenant_radar.web.routes.queue import create_queue_router

pytestmark = pytest.mark.e2e

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_PRINT_CSS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "covenant_radar"
    / "web"
    / "static"
    / "css"
    / "print.css"
)


class _Fixture:
    """A signed-in caller wired exactly as `create_production_app` wires one:
    the queue screen behind `ThemePreferenceMiddleware`, resolving through a
    DB-backed `theme_resolver`, plus the preferences router that persists it.
    """

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.principal = Principal.user(uuid4(), (Permission.VIEW_QUEUE,))
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="caller",
                email="caller@example.com",
                full_name="Caller",
                auth_source="local",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-user-caller",
            )
        )
        self.session.flush()

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _theme_resolver(self, principal: object) -> str | None:
        principal_id = getattr(principal, "id", None)
        if not isinstance(principal_id, UUID):
            return None
        user = self.session.get(AppUser, principal_id)
        return user.theme if user is not None and user.is_active else None

    def client(self) -> TestClient:
        app = create_app(
            routers=(
                create_queue_router(self.session),
                create_preferences_router(self.session),
            ),
            principal_resolver=lambda _request: self.principal,
            theme_resolver=self._theme_resolver,
        )
        return TestClient(app)

    def stored_theme(self) -> str:
        user = self.session.get(AppUser, self.principal.id)
        assert user is not None
        return user.theme


def test_no_flash_of_wrong_theme() -> None:
    """The first response already carries the resolved theme, unconditionally.

    A signed-in caller's stored preference decides `data-theme` on the very
    first byte served, with zero cookie in play — there is no earlier,
    wrong-themed paint for a script to correct after the fact. The response
    also advertises the `Sec-CH-Prefers-Color-Scheme` client hint so a
    supporting browser's first-ever, cookie-less visit resolves the theme
    before painting anything at all, rather than only from the second visit
    onward.
    """
    fixture = _Fixture()
    try:
        user = fixture.session.get(AppUser, fixture.principal.id)
        assert user is not None
        user.theme = "dark"
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get("/")

        assert response.status_code == 200
        assert 'data-theme="dark"' in response.text
        assert not response.cookies.get(THEME_COOKIE)
        assert response.headers["accept-ch"] == "Sec-CH-Prefers-Color-Scheme"
        assert response.headers["critical-ch"] == "Sec-CH-Prefers-Color-Scheme"
        assert "Sec-CH-Prefers-Color-Scheme" in response.headers["vary"]
    finally:
        fixture.close()


def _hidden_form_fields(html: str, *, action: str) -> dict[str, str]:
    """Return every hidden input's value from the named form, by field name.

    Reading exactly what the rendered `<form>` carries — rather than
    hand-assembling a payload — is what makes a submission built from this
    representative of a script-free browser: whatever hidden fields the
    server chose to render (a CSRF token included, when that middleware is
    configured) travel along automatically.
    """
    form_match = re.search(
        rf'<form method="post" action="{re.escape(action)}"[^>]*>(.*?)</form>',
        html,
        flags=re.DOTALL,
    )
    assert form_match is not None, f"no <form> posting to {action!r} was rendered"
    return dict(
        re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form_match.group(1))
    )


def _anonymous_shell_app() -> FastAPI:
    """The pre-authentication surface `ThemePreferenceMiddleware` protects.

    A signed-in caller's `AppUser.theme` column is never null, so the client
    hint only ever decides anything before a principal exists — the sign-in
    screen and any other public route. This isolates exactly that case,
    the same way `tests/security/test_hardening.py` isolates one middleware
    at a time rather than routing every check through the full application.
    """
    app = FastAPI()

    @app.get("/sign-in", response_class=HTMLResponse)
    async def sign_in(request: Request) -> str:
        return f'<html lang="en" data-theme="{theme_for_request(request)}"></html>'

    app.add_middleware(ThemePreferenceMiddleware, resolver=lambda _principal: None)
    return app


def test_no_stored_preference_uses_the_system_hint() -> None:
    """No cookie, no signed-in user: the resolved theme follows the client hint."""
    client = TestClient(_anonymous_shell_app())

    hinted = client.get("/sign-in", headers={"Sec-CH-Prefers-Color-Scheme": "dark"})
    unhinted = client.get("/sign-in")

    assert 'data-theme="dark"' in hinted.text
    assert 'data-theme="light"' in unhinted.text
    assert hinted.headers["accept-ch"] == "Sec-CH-Prefers-Color-Scheme"
    assert hinted.headers["critical-ch"] == "Sec-CH-Prefers-Color-Scheme"


def test_toggle_persists_across_sessions() -> None:
    """A theme change survives past the browser that made it.

    The toggle is posted from one client (one cookie jar), and a second,
    cookie-less client standing in for a different browser session for the
    same signed-in user renders the new theme purely from the database —
    proving the preference is a stored user attribute, not a cookie trick.
    """
    fixture = _Fixture()
    try:
        with fixture.client() as first_session:
            page = first_session.get("/")
            fields = _hidden_form_fields(page.text, action="/preferences/theme")

            response = first_session.post(
                "/preferences/theme",
                data={**fields, "theme": "dark"},
                follow_redirects=False,
            )
            assert response.status_code == 303

        assert fixture.stored_theme() == "dark"

        with fixture.client() as second_session:
            response = second_session.get("/")
            assert 'data-theme="dark"' in response.text
    finally:
        fixture.close()


def test_toggle_works_without_javascript() -> None:
    """The toggle is a plain form post — no script required to change theme.

    `TestClient` never executes JavaScript, so submitting exactly the fields
    a browser would send for the rendered `<form>` — its hidden `theme` and
    `next` values plus the CSRF field the security middleware injects — and
    getting a persisted, re-rendered result already demonstrates the
    no-JavaScript path end to end.
    """
    fixture = _Fixture()
    try:
        with fixture.client() as client:
            page = client.get("/")
            assert 'action="/preferences/theme"' in page.text

            submission = _hidden_form_fields(page.text, action="/preferences/theme")
            assert submission["theme"] == "dark"
            assert submission["next"] == "/"

            redirect = client.post("/preferences/theme", data=submission, follow_redirects=False)
            assert redirect.status_code == 303
            assert redirect.cookies.get(THEME_COOKIE) == "dark"

            after = client.get("/")
            assert 'data-theme="dark"' in after.text
    finally:
        fixture.close()


def test_print_render_legible_monochrome() -> None:
    """Print collapses every colour role to the physical page, both themes.

    Rather than a third, hand-picked print palette (which the token-literal
    gate would refuse outside `tokens.css` anyway), print media re-points
    every colour role at the CSS system-colour keywords for the page —
    proven here directly against the stylesheet contract, since headless
    print rendering has no browser available in this offline environment.
    """
    css = _PRINT_CSS.read_text(encoding="utf-8")

    root_rule = re.search(r':root,\s*\[data-theme="dark"\]\s*\{([^}]*)\}', css, flags=re.DOTALL)
    assert root_rule is not None
    overrides = root_rule.group(1)

    for role in ("--paper", "--surface-raised"):
        assert re.search(rf"{re.escape(role)}\s*:\s*Canvas\s*;", overrides)
    ink_roles = (
        "--ink",
        "--ink-muted",
        "--hairline",
        "--focus",
        "--headroom",
        "--watch",
        "--breach",
    )
    for role in ink_roles:
        assert re.search(rf"{re.escape(role)}\s*:\s*CanvasText\s*;", overrides)
    for role in ("--headroom-bg", "--watch-bg", "--breach-bg"):
        assert re.search(rf"{re.escape(role)}\s*:\s*Canvas\s*;", overrides)

    # No inked chrome, and every printed link's target survives on paper.
    assert '[data-shell-header]' in css and "display: none;" in css
    assert 'a[href]:not([href^="#"])::after' in css
    assert 'attr(href)' in css
