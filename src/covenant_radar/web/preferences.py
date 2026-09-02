"""Server-rendered user preferences, including the persisted colour theme."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from covenant_radar.api.deps import requires
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import ValidationError
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

THEME_COOKIE = "covenant_radar_theme"
THEMES = frozenset({"light", "dark"})
#: Every screen already reads this cookie to pick its catalogue; until now
#: nothing in the browser could set it, so the Hindi catalogue shipped with the
#: application was unreachable to a user.
LOCALE_COOKIE = "covenant_radar_locale"
LOCALES = ("en", "hi")
_MAX_FORM_BYTES = 16 * 1024
_WRITE = requires(Permission.VIEW_QUEUE)
_WRITE_DEP = Depends(_WRITE)

# The client hint this middleware asks the browser for. Advertising it via
# `Accept-CH` opts every subsequent same-origin navigation into sending the
# header; pairing it with `Critical-CH` additionally makes a supporting
# browser retry the very first, hint-less navigation before painting
# anything, so a first-time visitor with no cookie still gets a theme
# resolved server-side rather than a flash between two paints.
_COLOR_SCHEME_HINT = "Sec-CH-Prefers-Color-Scheme"

type ThemeResolver = Callable[[Principal | None], str | None]


class ThemePreferenceMiddleware:
    """Resolve a persisted preference before a template is rendered."""

    def __init__(self, app: ASGIApp, *, resolver: ThemeResolver) -> None:
        self.app = app
        self.resolver = resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        request = Request(scope, receive=receive)
        principal = state.get("principal")
        resolved = self.resolver(principal if isinstance(principal, Principal) else None)
        state["theme"] = _theme_or_default(
            resolved
            or request.cookies.get(THEME_COOKIE)
            or request.headers.get("sec-ch-prefers-color-scheme")
        )

        async def send_with_client_hint(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return
            await send({**message, "headers": _with_hint_headers(message.get("headers", []))})

        await self.app(scope, receive, send_with_client_hint)


def create_preferences_router(session: Session) -> APIRouter:
    """Build the small preference transition surface used by the shell."""

    if not is_database_session(session):
        raise TypeError("create_preferences_router requires a SQLAlchemy Session.")
    router = APIRouter(tags=["preferences-web"])

    @router.post("/preferences/theme", name="set_theme")
    async def set_theme(request: Request, principal: Principal = _WRITE_DEP) -> Response:
        values = await _form_values(request)
        theme = values.get("theme", "")
        if theme not in THEMES:
            raise ValidationError("Theme must be light or dark.", field="theme")
        user = session.get(AppUser, principal.id)
        if user is None or not user.is_active:
            raise ValidationError("The signed-in user is unavailable.", field="theme")
        now = datetime.now(UTC)
        user.theme = theme
        user.updated_at = now
        user.updated_by_id = principal.id
        user.request_id = get_request_id() or new_request_id()
        response = RedirectResponse(_safe_destination(values.get("next", "/")), status_code=303)
        response.set_cookie(
            THEME_COOKIE,
            theme,
            httponly=False,
            secure=not _is_local_request(request),
            samesite="lax",
            path="/",
        )
        return response

    @router.post("/preferences/locale", name="set_locale")
    async def set_locale(request: Request, principal: Principal = _WRITE_DEP) -> Response:
        del principal  # The cookie is per-browser; the permission gates the route.
        values = await _form_values(request)
        locale = values.get("locale", "").lower()
        if locale not in LOCALES:
            raise ValidationError(
                f"Locale must be one of {', '.join(LOCALES)}.", field="locale"
            )
        response = RedirectResponse(_safe_destination(values.get("next", "/")), status_code=303)
        response.set_cookie(
            LOCALE_COOKIE,
            locale,
            httponly=False,
            secure=not _is_local_request(request),
            samesite="lax",
            path="/",
        )
        return response

    return router


def locale_for_request(request: Request) -> str:
    """Return the requested catalogue locale, defaulting to English."""

    value = str(request.cookies.get(LOCALE_COOKIE, "")).lower()
    return value if value in LOCALES else LOCALES[0]


def theme_for_request(request: Request) -> str:
    """Return the middleware-resolved theme for route template contexts."""

    return _theme_or_default(
        getattr(request.state, "theme", None) or request.cookies.get(THEME_COOKIE)
    )


def _theme_or_default(value: object) -> str:
    if isinstance(value, str) and value.lower() in THEMES:
        return value.lower()
    return "light"


def _with_hint_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Advertise the colour-scheme client hint, merging it into any Vary."""
    kept = [
        (name, value)
        for name, value in headers
        if name.lower() not in (b"accept-ch", b"critical-ch", b"vary")
    ]
    existing_vary = [
        value.decode("latin-1") for name, value in headers if name.lower() == b"vary"
    ]
    vary_parts = [
        part.strip() for value in existing_vary for part in value.split(",") if part.strip()
    ]
    if _COLOR_SCHEME_HINT not in vary_parts:
        vary_parts.append(_COLOR_SCHEME_HINT)
    hint = _COLOR_SCHEME_HINT.encode("latin-1")
    return [
        *kept,
        (b"accept-ch", hint),
        (b"critical-ch", hint),
        (b"vary", ", ".join(vary_parts).encode("latin-1")),
    ]


async def _form_values(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted preference form is too large.", field="theme")
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError(
            "The submitted preference is not valid UTF-8.", field="theme"
        ) from error
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _safe_destination(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value if "\\" not in value else "/"


def _is_local_request(request: Request) -> bool:
    host = request.url.hostname or ""
    return host.lower() in {"127.0.0.1", "localhost", "::1", "testserver"}


__all__ = [
    "LOCALES",
    "LOCALE_COOKIE",
    "THEME_COOKIE",
    "ThemePreferenceMiddleware",
    "ThemeResolver",
    "create_preferences_router",
    "locale_for_request",
    "theme_for_request",
]
