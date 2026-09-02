"""Covenant Radar ASGI application factory and public operational routes."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from covenant_radar import __version__
from covenant_radar.api.deps import (
    RequestPrincipalResolver,
    install_route_declaration_check,
    public,
)
from covenant_radar.api.errors import (
    domain_error_body,
    http_error_body,
    request_validation_error_handler,
)
from covenant_radar.config.settings import Settings, get_settings
from covenant_radar.core.errors import DomainError
from covenant_radar.i18n import (
    Catalogue,
    CatalogueError,
    assert_catalogue_covers_templates,
    find_template_literals_in_text,
    load_catalogue,
    translator_for,
)
from covenant_radar.i18n.formatting import (
    format_currency,
    format_date,
    format_fy_label,
    format_fy_quarter,
    format_indian_currency,
    format_indian_number,
    format_ist_date,
)
from covenant_radar.observability.logging import (
    configure as configure_logging,
)
from covenant_radar.observability.metrics import RequestMetricsMiddleware
from covenant_radar.security.csrf import install_csrf
from covenant_radar.security.headers import install_security_headers
from covenant_radar.security.ratelimit import install_rate_limiting
from covenant_radar.web.errors import (
    log_unhandled_exception,
    status_for_error,
    support_reference,
    ui_key_for_status,
)
from covenant_radar.web.middleware import RequestContextMiddleware
from covenant_radar.web.preferences import ThemePreferenceMiddleware, ThemeResolver
from covenant_radar.web.routes.system import create_system_router

_PACKAGE_ROOT = Path(__file__).resolve().parent
_DEFAULT_TEMPLATE_ROOT = _PACKAGE_ROOT / "web" / "templates"
_DEFAULT_STATIC_ROOT = _PACKAGE_ROOT / "web" / "static"
_SUPPORTED_LOCALES = frozenset({"en", "hi"})
_SUPPORTED_THEMES = frozenset({"light", "dark"})
_LOCALE_COOKIE = "covenant_radar_locale"
_THEME_COOKIE = "covenant_radar_theme"
_METRICS_TOKEN_ENV = "COVENANT_RADAR_METRICS_TOKEN"


def create_app(
    settings: Settings | None = None,
    *,
    routers: Iterable[APIRouter] = (),
    principal_resolver: object | None = None,
    catalogue: Catalogue | None = None,
    template_directory: Path | str | None = None,
    static_directory: Path | str | None = None,
    audit_writer: object | None = None,
    role_permissions: (
        Callable[[], Mapping[str, Iterable[object]]] | Mapping[str, Iterable[object]] | None
    ) = None,
    theme_resolver: ThemeResolver | None = None,
) -> FastAPI:
    """Create a fully wired FastAPI application.

    Adapters are injected at the boundary.  In particular, the default
    principal resolver is intentionally anonymous: a deployment must provide
    a real session/API-key resolver rather than receiving a development
    credential by accident.
    """
    resolved_settings = settings if settings is not None else get_settings()
    configure_logging()
    template_root = Path(template_directory or _DEFAULT_TEMPLATE_ROOT)
    static_root = Path(static_directory or _DEFAULT_STATIC_ROOT)
    resolved_catalogue = catalogue if catalogue is not None else load_catalogue()
    _assert_shell_templates_are_externalized(template_root, resolved_catalogue)

    app = FastAPI(title="Covenant Radar", version=__version__)
    app.state.settings = resolved_settings
    app.state.catalogue = resolved_catalogue
    app.state.template_directory = template_root
    app.state.principal_resolver = (
        principal_resolver if principal_resolver is not None else RequestPrincipalResolver()
    )
    app.state.audit_writer = audit_writer
    app.state.metrics_enabled = resolved_settings.observability.metrics_enabled
    app.state.metrics_token = os.environ.get(_METRICS_TOKEN_ENV)

    environment = Jinja2Templates(directory=str(template_root)).env
    environment.undefined = StrictUndefined
    _configure_template_environment(environment, resolved_catalogue, resolved_settings)
    app.state.templates = environment
    app.state.template_env = environment

    # These middleware are installed first so RequestContextMiddleware is the
    # outermost layer and binds identity before rate-limit/audit code runs.
    if resolved_settings.security.session_secret is not None:
        install_csrf(app, settings=resolved_settings)
    install_security_headers(app)
    install_rate_limiting(app, settings=resolved_settings, audit=audit_writer)
    if theme_resolver is not None:
        app.add_middleware(ThemePreferenceMiddleware, resolver=theme_resolver)
    app.add_middleware(RequestContextMiddleware, exception_responder=_unhandled_error_handler)
    # Added last so it wraps every other middleware and measures the request
    # as the client experienced it, including the final status an error
    # handler produced (`observability/metrics.py`).
    app.add_middleware(RequestMetricsMiddleware)

    app.mount(
        "/static",
        _RevalidatedStaticFiles(directory=str(static_root), check_dir=True),
        name="static",
    )
    _mark_static_mount_public(app)
    _install_favicon(app, static_root)

    app.include_router(create_system_router())
    for router in routers:
        app.include_router(router)

    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
    install_route_declaration_check(app, role_permissions=role_permissions)
    return app


def _configure_template_environment(
    environment: Any, catalogue: Catalogue, settings: Settings | None = None
) -> None:
    default_translator = translator_for(catalogue)
    environment.globals["_"] = default_translator
    environment.globals["notification_unread_count"] = _notification_unread_count
    # Rendering components outside a running application (security and
    # accessibility audits do this deliberately) has no settings object.
    # Treat that as the conservative disabled state rather than making the
    # audit environment diverge from the application's template filters.
    environment.globals["live_workspace_enabled"] = (
        settings.web.live_workspace_enabled if settings is not None else False
    )
    environment.filters.update(
        {
            "indian_number": format_indian_number,
            "indian_currency": format_indian_currency,
            "currency": format_currency,
            "ist_date": format_ist_date,
            "date_ist": format_date,
            "fy_quarter": format_fy_quarter,
            "fy_label": format_fy_label,
        }
    )


def _notification_unread_count(request: Request, principal: object) -> int:
    """Expose a bounded, recipient-scoped unread count to the application shell."""

    # Templates are also rendered directly in a handful of component and
    # integration tests, where Starlette's lightweight request scope has no
    # attached application.  Treat that exactly like an application without
    # the optional notification service instead of making the shared shell
    # impossible to render in isolation.
    app = request.scope.get("app")
    service = getattr(getattr(app, "state", None), "notification_service", None)
    if service is None or principal is None:
        return 0
    counter = getattr(service, "unread_count", None)
    if not callable(counter):
        return 0
    count_fn = cast(Callable[[object], object], counter)
    value = count_fn(principal)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("notification unread count must be a non-negative integer.")
    return value


def _assert_shell_templates_are_externalized(template_root: Path, catalogue: Catalogue) -> None:
    shell_paths = (
        template_root / "base.html",
        template_root / "screens" / "_404.html",
        template_root / "screens" / "_500.html",
    )
    findings = []
    for path in shell_paths:
        if not path.is_file():
            raise CatalogueError(f"Required shell template is missing: {path}")
        findings.extend(find_template_literals_in_text(path.read_text(encoding="utf-8"), path=path))
    if findings:
        details = ", ".join(f"{item.path}:{item.line} ({item.text})" for item in findings)
        raise CatalogueError(f"Literal user-facing template string(s) are forbidden: {details}.")
    # Check keys in the complete template tree when available.  Existing
    # component templates are allowed to remain data-driven until their owning
    # screen tasks migrate them, but any shell key must be shipped now.
    assert_catalogue_covers_templates(template_root, catalogue)


def _install_favicon(app: FastAPI, static_root: Path) -> None:
    """Answer the browser's unprompted `/favicon.ico` request.

    Every browser asks for this path whether or not a page links an icon, so
    without a route it produced a 404 on every single page load — noise in the
    logs and a failed request in the end-to-end console checks.  The icon is
    the same asset `base.html` links, served from the application's own static
    root with no external origin.
    """

    icon_path = static_root / "favicon.svg"
    if not icon_path.is_file():
        raise RuntimeError(f"The application favicon is missing: {icon_path}")

    @app.get("/favicon.ico", include_in_schema=False)
    @public
    async def favicon() -> Response:
        return FileResponse(
            icon_path,
            media_type="image/svg+xml",
            headers={"cache-control": "public, max-age=86400"},
        )


class _RevalidatedStaticFiles(StaticFiles):
    """`StaticFiles` that always revalidates instead of being cached blind.

    Starlette sends `ETag` and `Last-Modified` but no `Cache-Control`, which
    leaves the browser free to invent a freshness lifetime of its own (the
    usual heuristic is a tenth of the file's age).  Asset URLs here are not
    fingerprinted, so a stylesheet edited today keeps being read from cache
    while the templates that go with it are re-rendered on every request —
    the page is then drawn by a mixture of two versions, which looks like the
    layout itself has broken.

    `no-cache` does not mean "do not store": the response is still cached and
    still answered with a conditional request, so an unchanged file costs one
    304 rather than a re-download.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        # Set on the 304 as well as the 200: a conditional response's headers
        # update the stored entry, so omitting it there would let the browser
        # fall back to guessing a lifetime again on the very next read.
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        return response


def _mark_static_mount_public(app: FastAPI) -> None:
    for route in app.routes:
        if getattr(route, "path", None) != "/static":
            continue
        endpoint = getattr(route, "app", None)
        if callable(endpoint):
            # Mount does not expose an endpoint attribute, while the existing
            # authorization registry intentionally inspects endpoints.  Mark
            # this framework-owned static application explicitly public.
            route.endpoint = public(endpoint)
        return
    raise RuntimeError("Static files mount was not registered.")


async def _domain_error_handler(request: Request, error: DomainError) -> Response:
    status = status_for_error(error)
    if _is_browser_route(request) and _wants_html(request):
        return _render_error(request, status=status)
    return JSONResponse(
        domain_error_body(error, request_id=request.state.request_id),
        status_code=status,
    )


async def _http_error_handler(request: Request, error: StarletteHTTPException) -> Response:
    status = int(error.status_code)
    if status == 401 and _is_browser_route(request):
        return RedirectResponse(
            _sign_in_destination(request),
            status_code=303,
            headers={"X-Request-ID": request.state.request_id},
        )
    if status == 404 or (_is_browser_route(request) and _wants_html(request)):
        if status in {400, 401, 403, 404, 409, 422, 503}:
            return _render_error(request, status=status)
    detail = error.detail if isinstance(error.detail, str) else "Request failed."
    # `error.headers` carries framework-set headers a caller must still see on
    # the reshaped body — most notably `Allow` on a 405 from an unmatched
    # method, which `C-21` requires ("a write attempt on a read-only resource
    # → 405 with the permitted methods").
    headers = dict(error.headers) if error.headers else {}
    headers["X-Request-ID"] = request.state.request_id
    return JSONResponse(
        http_error_body(status=status, detail=detail, request_id=request.state.request_id),
        status_code=status,
        headers=headers,
    )


async def _unhandled_error_handler(request: Request, error: Exception) -> HTMLResponse:
    reference = log_unhandled_exception(request, error)
    return cast(
        HTMLResponse,
        _render_error(request, status=500, support_reference_value=reference),
    )


def _render_error(
    request: Request, *, status: int, support_reference_value: str | None = None
) -> Response:
    template_name = "screens/_404.html" if status == 404 else "screens/_500.html"
    context = _template_context(request)
    context["support_reference"] = support_reference_value or support_reference(
        request.state.request_id
    )
    context["error_status"] = status
    context["error_title_key"] = ui_key_for_status(status)
    template = request.app.state.templates.get_template(template_name)
    return HTMLResponse(template.render(**context), status_code=status)


def _template_context(request: Request) -> dict[str, object]:
    locale = _request_locale(request)
    catalogue: Catalogue = request.app.state.catalogue
    return {
        "request": request,
        "_": translator_for(catalogue, locale=locale),
        "locale": locale,
        "theme": _request_theme(request),
        "principal": getattr(request.state, "principal", None),
        "text_direction": "rtl" if locale in {"ar", "he", "fa", "ur"} else "ltr",
    }


def _request_locale(request: Request) -> str:
    requested = request.cookies.get(_LOCALE_COOKIE, "en").replace("_", "-").lower()
    base = requested.split("-", maxsplit=1)[0]
    return base if base in _SUPPORTED_LOCALES else "en"


def _request_theme(request: Request) -> str:
    state_theme = getattr(request.state, "theme", None)
    if isinstance(state_theme, str) and state_theme in _SUPPORTED_THEMES:
        return state_theme
    requested = request.cookies.get(_THEME_COOKIE, "light").lower()
    return requested if requested in _SUPPORTED_THEMES else "light"


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept.lower()


def _is_browser_route(request: Request) -> bool:
    """Keep browser sessions navigable while preserving API 401 responses."""
    return not request.url.path.startswith("/api/")


def _sign_in_destination(request: Request) -> str:
    path = request.url.path
    query = request.url.query
    destination = f"{path}?{query}" if query else path
    # The sign-in route validates this value again.  Restricting it to a
    # relative path here prevents an error response from becoming an open
    # redirect even when a proxy supplies unusual request headers.
    if not destination.startswith("/") or destination.startswith("//") or "\\" in destination:
        destination = "/"
    from urllib.parse import quote

    return "/sign-in?next=" + quote(destination, safe="/")


app = create_app()
application = app


__all__ = ["app", "application", "create_app"]
