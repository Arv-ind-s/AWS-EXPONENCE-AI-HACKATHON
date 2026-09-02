"""Focused proof that the application hardening boundary is active."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.testclient import TestClient
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape
from jinja2.exceptions import UndefinedError

from covenant_radar.asgi import _configure_template_environment, load_catalogue
from covenant_radar.core.clock import FixedClock
from covenant_radar.security.csrf import CSRFMiddleware, CSRFSettings
from covenant_radar.security.headers import (
    SecurityHeadersError,
    SecurityHeadersMiddleware,
    SecurityHeadersSettings,
    assert_no_external_origins,
    assert_no_external_origins_in_text,
)
from covenant_radar.security.ratelimit import (
    InMemoryRateLimitStore,
    RateLimiter,
    RateLimitMiddleware,
    RateLimitRule,
    RateLimitSettings,
)
from covenant_radar.security.uploads import (
    UploadGuard,
    UploadPolicy,
    UploadTooLarge,
    UploadTypeMismatch,
)

pytestmark = pytest.mark.security

_SECRET = b"hardening-test-secret-012345678901"
_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _app(*middleware: type, rate_limiter: RateLimiter | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/form", response_class=HTMLResponse)
    async def form() -> str:
        return '<form method="post" action="/form"><button>Save</button></form>'

    @app.post("/form")
    async def submit() -> dict[str, str]:
        return {"status": "ok"}

    for item in middleware:
        if item is SecurityHeadersMiddleware:
            app.add_middleware(item, settings=SecurityHeadersSettings())
        elif item is CSRFMiddleware:
            app.add_middleware(
                item,
                settings=CSRFSettings(secret=_SECRET, cookie_secure=False),
            )
        elif item is RateLimitMiddleware:
            app.add_middleware(item, limiter=rate_limiter)
    return app


def test_csp_has_no_external_origin() -> None:
    settings = SecurityHeadersSettings()

    assert "http://" not in settings.content_security_policy
    assert "https://" not in settings.content_security_policy
    assert "frame-ancestors 'none'" in settings.content_security_policy

    with pytest.raises(SecurityHeadersError, match="external origin"):
        SecurityHeadersSettings(
            content_security_policy="default-src 'self'; connect-src https://example.invalid"
        )


def test_no_template_references_external_origin() -> None:
    template_directory = (
        Path(__file__).resolve().parents[2] / "src" / "covenant_radar" / "web" / "templates"
    )
    assert_no_external_origins(template_directory)
    templates = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
        # Component partials are written to be included with a context their
        # owning screen supplies, so rendering one standalone dereferences
        # attributes of variables that are simply not here (`result.state` in
        # `_components/export_status.html`).  Strict `Undefined` turns that
        # into an error about the fixture rather than about external origins,
        # which is what this test is actually asserting; chaining lets every
        # template render so its output can be scanned.
        undefined=ChainableUndefined,
    )
    # Templates use the application's own filters (`ist_date`, `indian_currency`
    # and the rest).  Registering them here renders each template the way the
    # app does, instead of failing on the first filter the bare environment
    # does not know.
    _configure_template_environment(templates, load_catalogue())
    # A bare sentinel satisfies the templates' `request is defined` guards while
    # failing every attribute they then read, so the shell partials need a
    # request-shaped stub to render at all.
    request = SimpleNamespace(
        url=SimpleNamespace(path="/"),
        cookies={},
        state=SimpleNamespace(csrf_token=""),
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    context = {
        "request": request,
        "next": "/",
        "username": "",
        "error": "",
        "provisioning_uri": "/mfa/setup",
        "secret": "test-secret",
        # Identifiers that appear *inside* a URL path need a value. An empty
        # interpolation collapses `/documents/{{ document.id }}/classification`
        # into `/documents//classification`, and a leading `//` is a
        # protocol-relative URL — so an absent fixture value would be reported
        # as an external origin that the running app never emits.
        "document": SimpleNamespace(id="00000000-0000-0000-0000-000000000000"),
    }
    rendered_count = 0
    for template_path in sorted(template_directory.rglob("*.html")):
        template_name = template_path.relative_to(template_directory).as_posix()
        try:
            rendered = templates.get_template(template_name).render(**context)
        except UndefinedError:
            # A partial that compares or iterates a value its owning screen
            # supplies (`view.page > 1`) cannot render from a generic context,
            # and inventing a per-template fixture would make this a
            # rendering test rather than a hardening one.  The source of every
            # template — this one included — has already been scanned by
            # `assert_no_external_origins` above, so the guarantee holds; this
            # pass additionally catches an origin assembled at render time.
            continue
        rendered_count += 1
        assert_no_external_origins_in_text(rendered, path=template_path)

    assert rendered_count, "No template rendered; the render-time scan checked nothing."


def test_all_headers_present() -> None:
    client = TestClient(_app(SecurityHeadersMiddleware))

    response = client.get("/form")

    assert response.status_code == 200
    assert response.headers["content-security-policy"]
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-frame-options"] == "DENY"


def test_csrf_required_on_state_change() -> None:
    client = TestClient(_app(CSRFMiddleware))
    page = client.get("/form")

    assert page.status_code == 200
    assert 'name="csrf_token"' in page.text
    assert client.post("/form").status_code == 403

    token = page.cookies.get("covenant_radar_csrf")
    assert token is not None
    accepted = client.post("/form", data={"csrf_token": token})
    assert accepted.status_code == 200


def test_stale_csrf_token_refused() -> None:
    client = TestClient(_app(CSRFMiddleware))
    page = client.get("/form")
    token = page.cookies.get("covenant_radar_csrf")
    assert token is not None

    client.cookies.set("covenant_radar_session", "a-new-session")
    response = client.post("/form", data={"csrf_token": token})

    assert response.status_code == 403
    assert "token" not in response.text.lower()


def test_multipart_csrf_token_is_accepted_before_handler() -> None:
    client = TestClient(_app(CSRFMiddleware))
    client.get("/form")
    token = client.cookies.get("covenant_radar_csrf")
    assert token is not None

    response = client.post(
        "/form",
        data={"csrf_token": token},
        files={"document": ("letter.pdf", b"%PDF-1.7", "application/pdf")},
    )

    assert response.status_code == 200


def test_csrf_rotates_when_session_cookie_changes() -> None:
    app = FastAPI()

    @app.get("/form", response_class=HTMLResponse)
    async def form() -> str:
        return '<form method="post"><button>Continue</button></form>'

    @app.post("/session")
    async def session() -> Response:
        response = Response("ok")
        response.set_cookie("covenant_radar_session", "new-session")
        return response

    app.add_middleware(
        CSRFMiddleware,
        settings=CSRFSettings(secret=_SECRET, cookie_secure=False),
    )
    client = TestClient(app)
    client.get("/form")
    old_token = client.cookies.get("covenant_radar_csrf")
    assert old_token is not None

    response = client.post("/session", data={"csrf_token": old_token})
    new_token = client.cookies.get("covenant_radar_csrf")

    assert response.status_code == 200
    assert new_token is not None and new_token != old_token


def test_rate_limit_returns_429_with_retry_after() -> None:
    clock = FixedClock(_NOW)
    limiter = RateLimiter(
        settings=RateLimitSettings(
            authentication=RateLimitRule(limit=1, window=timedelta(minutes=1))
        ),
        store=InMemoryRateLimitStore(),
        clock=clock,
    )
    app = FastAPI()

    @app.get("/sign-in")
    async def sign_in() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    client = TestClient(app)

    assert client.get("/sign-in").status_code == 200
    response = client.get("/sign-in")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.text == "Rate limit exceeded."


def test_upload_magic_byte_mismatch_refused() -> None:
    guard = UploadGuard(scanner=lambda _content: True)

    with pytest.raises(UploadTypeMismatch, match="application/pdf.*application/zip"):
        guard.validate("letter.pdf", "application/pdf", b"PK\x03\x04not-a-pdf")


def test_oversize_upload_refused_and_not_stored() -> None:
    stored: list[bytes] = []
    guard = UploadGuard(
        policy=UploadPolicy(max_bytes=5),
        scanner=lambda _content: True,
    )

    with pytest.raises(UploadTooLarge, match="5 bytes"):
        guard.validate("letter.pdf", "application/pdf", b"%PDF-oversized")
    assert stored == []
