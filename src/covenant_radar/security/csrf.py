"""Session-bound CSRF protection for browser and form requests.

Tokens are signed, short-lived and bound to the current session cookie.  The
middleware also uses the double-submit property: the signed value must be
present in both the CSRF cookie and the submitted form field (or request
header).  A session transition therefore invalidates a token issued before a
login, logout or privilege change.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from html import escape
from http.cookies import CookieError, Morsel, SimpleCookie
from typing import Any
from urllib.parse import parse_qs

from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class CSRFError(ValueError):
    """Raised for invalid or unsafe CSRF configuration."""


class CSRFValidationError(CSRFError):
    """Raised when a submitted CSRF token cannot be accepted."""


_DEFAULT_CSRF_COOKIE_NAME = "covenant_radar_csrf"
_DEFAULT_SESSION_COOKIE_NAME = "covenant_radar_session"
_DEFAULT_TOKEN_TTL_SECONDS = 3_600
_DEFAULT_COOKIE_SECURE = True
_DEFAULT_COOKIE_SAME_SITE = "lax"
_DEFAULT_COOKIE_PATH = "/"
_DEFAULT_MAX_FORM_BODY_BYTES = 2 * 1024 * 1024
_DEFAULT_MULTIPART_SCAN_BYTES = 128 * 1024
_DEFAULT_HTML_INJECTION_LIMIT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CSRFSettings:
    """Configuration for :class:`CSRFMiddleware`."""

    secret: bytes | str
    cookie_name: str = _DEFAULT_CSRF_COOKIE_NAME
    session_cookie_name: str = _DEFAULT_SESSION_COOKIE_NAME
    token_ttl_seconds: int = _DEFAULT_TOKEN_TTL_SECONDS
    cookie_secure: bool = _DEFAULT_COOKIE_SECURE
    cookie_same_site: str = _DEFAULT_COOKIE_SAME_SITE
    cookie_path: str = _DEFAULT_COOKIE_PATH
    max_form_body_bytes: int = _DEFAULT_MAX_FORM_BODY_BYTES
    multipart_token_scan_bytes: int = _DEFAULT_MULTIPART_SCAN_BYTES
    html_injection_limit_bytes: int = _DEFAULT_HTML_INJECTION_LIMIT_BYTES
    state_changing_methods: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
    skip_bearer_requests: bool = True
    field_name: str = "csrf_token"
    header_name: str = "x-csrf-token"

    def __post_init__(self) -> None:
        secret = self.secret.encode("utf-8") if isinstance(self.secret, str) else self.secret
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise CSRFError("CSRF signing secret must contain at least 32 bytes.")
        for name, value in (
            ("CSRF cookie name", self.cookie_name),
            ("session cookie name", self.session_cookie_name),
            ("CSRF field name", self.field_name),
            ("CSRF header name", self.header_name),
        ):
            if (
                not value
                or not value.isascii()
                or any(character.isspace() or character in ";,=" for character in value)
            ):
                raise CSRFError(f"{name} contains an invalid character.")
        if self.token_ttl_seconds <= 0:
            raise CSRFError("CSRF token lifetime must be positive.")
        if self.max_form_body_bytes <= 0 or self.multipart_token_scan_bytes <= 0:
            raise CSRFError("CSRF request limits must be positive.")
        if self.html_injection_limit_bytes <= 0:
            raise CSRFError("CSRF HTML injection limit must be positive.")
        if self.cookie_same_site not in {"lax", "strict", "none"}:
            raise CSRFError("CSRF SameSite must be lax, strict or none.")
        if self.cookie_same_site == "none" and not self.cookie_secure:
            raise CSRFError("SameSite=None CSRF cookies must be Secure.")
        if not self.cookie_path.startswith("/"):
            raise CSRFError("CSRF cookie path must be absolute.")
        methods = frozenset(method.upper() for method in self.state_changing_methods)
        if not methods:
            raise CSRFError("At least one state-changing method is required.")
        object.__setattr__(self, "secret", secret)
        object.__setattr__(self, "state_changing_methods", methods)
        object.__setattr__(self, "cookie_same_site", self.cookie_same_site.lower())
        object.__setattr__(self, "header_name", self.header_name.lower())

    @staticmethod
    def _resolve_cookie_secure(settings: object, security: object) -> bool:
        """Decide the CSRF cookie's `Secure` flag the way the session cookie does.

        `security.secure_cookie` wins when a deployment sets it.  Otherwise the
        flag follows the listening host, matching `SessionSettings` in the
        browser composition root: loopback serves plain HTTP, and a `Secure`
        cookie is simply never returned by the browser there.  Defaulting to
        `True` regardless of host meant the CSRF cookie was set but never sent
        back over http://, so every POST — sign-in included — failed CSRF
        validation the moment the app was reached on anything but localhost.
        """

        explicit = getattr(security, "secure_cookie", None)
        if explicit is not None:
            return bool(explicit)
        web = getattr(settings, "web", None)
        host = str(getattr(web, "host", "") or "").lower()
        if not host:
            return _DEFAULT_COOKIE_SECURE
        return host not in {"127.0.0.1", "localhost", "::1", "testserver"}

    @classmethod
    def from_settings(cls, settings: object) -> CSRFSettings:
        """Build CSRF settings from the application's settings object."""
        security = getattr(settings, "security", settings)
        secret_value = getattr(security, "session_secret", None)
        if secret_value is not None and hasattr(secret_value, "get_secret_value"):
            secret_value = secret_value.get_secret_value()
        if secret_value is None:
            raise CSRFError("A session secret is required to configure CSRF protection.")
        return cls(
            secret=secret_value,
            cookie_name=str(getattr(security, "csrf_cookie_name", _DEFAULT_CSRF_COOKIE_NAME)),
            session_cookie_name=str(
                getattr(security, "session_cookie_name", _DEFAULT_SESSION_COOKIE_NAME)
            ),
            token_ttl_seconds=int(
                getattr(security, "csrf_token_ttl_seconds", _DEFAULT_TOKEN_TTL_SECONDS)
            ),
            cookie_secure=cls._resolve_cookie_secure(settings, security),
            cookie_same_site=str(getattr(security, "same_site", _DEFAULT_COOKIE_SAME_SITE)).lower(),
            cookie_path=str(getattr(security, "cookie_path", _DEFAULT_COOKIE_PATH)),
            max_form_body_bytes=int(
                getattr(security, "csrf_max_form_body_bytes", _DEFAULT_MAX_FORM_BODY_BYTES)
            ),
            multipart_token_scan_bytes=int(
                getattr(
                    security,
                    "csrf_multipart_token_scan_bytes",
                    _DEFAULT_MULTIPART_SCAN_BYTES,
                )
            ),
            html_injection_limit_bytes=int(
                getattr(
                    security,
                    "csrf_html_injection_limit_bytes",
                    _DEFAULT_HTML_INJECTION_LIMIT_BYTES,
                )
            ),
        )

    def cookie_header(self, token: str, *, max_age: int | None = None) -> str:
        """Serialize a browser-readable, secure CSRF cookie."""
        morsel: Morsel[str] = Morsel()
        morsel.set(self.cookie_name, token, token)
        morsel["path"] = self.cookie_path
        morsel["max-age"] = str(max_age if max_age is not None else self.token_ttl_seconds)
        morsel["samesite"] = self.cookie_same_site
        if self.cookie_secure:
            morsel["secure"] = True
        return morsel.OutputString()


class CSRFTokenManager:
    """Issue and verify signed tokens bound to a browser session."""

    _SALT = "covenant-radar/csrf/v1"

    def __init__(self, settings: CSRFSettings) -> None:
        self.settings = settings
        self._serializer = URLSafeTimedSerializer(
            settings.secret,
            salt=self._SALT,
            signer_kwargs={"digest_method": hashlib.sha256},
        )

    def issue(self, session_cookie: str | None) -> str:
        """Issue a new token for the supplied session envelope."""
        import secrets

        return self._serializer.dumps(
            {
                "v": 1,
                "binding": self._binding(session_cookie),
                "nonce": secrets.token_urlsafe(32),
            }
        )

    def validate(self, token: str | None, session_cookie: str | None) -> bool:
        """Return false for every malformed, expired or cross-session token."""
        if not isinstance(token, str) or not token or len(token) > 4096:
            return False
        try:
            payload = self._serializer.loads(token, max_age=self.settings.token_ttl_seconds)
        except BadData:
            return False
        if not isinstance(payload, dict) or set(payload) != {"v", "binding", "nonce"}:
            return False
        binding = payload.get("binding")
        nonce = payload.get("nonce")
        if payload.get("v") != 1 or not isinstance(binding, str) or not isinstance(nonce, str):
            return False
        return hmac.compare_digest(binding, self._binding(session_cookie)) and bool(nonce)

    def get_or_issue(
        self, cookie_token: str | None, session_cookie: str | None
    ) -> tuple[str, bool]:
        """Return the current valid token and whether the cookie must rotate."""
        if self.validate(cookie_token, session_cookie):
            return cookie_token or "", False
        return self.issue(session_cookie), True

    generate = issue
    verify = validate

    def _binding(self, session_cookie: str | None) -> str:
        if not session_cookie:
            return "anonymous"
        return hashlib.sha256(session_cookie.encode("utf-8")).hexdigest()


def csrf_token(request: Request) -> str:
    """Return the request token for a template or a response helper."""
    token = getattr(request.state, "csrf_token", None)
    if not isinstance(token, str) or not token:
        raise CSRFError("CSRF middleware has not established a token for this request.")
    return token


def csrf_hidden_input(request: Request, *, field_name: str = "csrf_token") -> str:
    """Return an escaped hidden input for explicitly rendered templates."""
    if not field_name or any(character in field_name for character in "\"'<>\x00"):
        raise CSRFError("CSRF field name contains an invalid character.")
    return (
        f'<input type="hidden" name="{escape(field_name, quote=True)}" '
        f'value="{escape(csrf_token(request), quote=True)}">'
    )


def rotate_csrf_token(request: Request) -> None:
    """Mark the response to rotate CSRF after a privilege transition."""
    state = request.scope.setdefault("state", {})
    state["_covenant_radar_rotate_csrf"] = True


mark_privilege_change = rotate_csrf_token


class CSRFMiddleware:
    """Enforce CSRF on cookie-authenticated state-changing requests."""

    def __init__(
        self,
        app: ASGIApp,
        settings: CSRFSettings | object | None = None,
        *,
        secret: bytes | str | None = None,
    ) -> None:
        self.app = app
        if isinstance(settings, CSRFSettings):
            configured = settings
        elif settings is not None:
            configured = CSRFSettings.from_settings(settings)
        elif secret is not None:
            configured = CSRFSettings(secret=secret)
        else:
            raise CSRFError("CSRF middleware requires a configured signing secret.")
        self.settings = configured
        self.tokens = CSRFTokenManager(configured)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        request_headers = _header_map(scope.get("headers", []))
        request_cookies = _parse_cookies(request_headers.get(b"cookie", b""))
        session_cookie = request_cookies.get(self.settings.session_cookie_name)
        cookie_token = request_cookies.get(self.settings.cookie_name)
        current_token, cookie_needs_set = self.tokens.get_or_issue(cookie_token, session_cookie)
        state["csrf_token"] = current_token
        state["csrf_token_manager"] = self.tokens

        method = str(scope.get("method", "GET")).upper()
        skip_bearer = self.settings.skip_bearer_requests and _is_bearer_request(
            request_headers.get(b"authorization", b""), session_cookie
        )
        replay_receive = receive
        if method in self.settings.state_changing_methods and not skip_bearer:
            submitted = _header_text(
                request_headers.get(self.settings.header_name.encode("latin-1"))
            )
            if submitted is None:
                submitted, replay_receive = await self._read_submitted_token(
                    receive,
                    request_headers.get(b"content-type", b"").decode("latin-1", "ignore"),
                )
            if not self._is_valid_submission(submitted, cookie_token, session_cookie):
                await self._send_rejection(
                    send,
                    current_token=current_token,
                    include_cookie=cookie_needs_set,
                    status_code=403,
                )
                return

        async def send_with_csrf(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return

            headers = list(message.get("headers", []))
            has_session_transition, session_transition = _session_cookie_transition(
                headers, self.settings.session_cookie_name
            )
            rotate = bool(state.pop("_covenant_radar_rotate_csrf", False))
            cookie_to_set: str | None = None
            if has_session_transition:
                cookie_to_set = self.tokens.issue(session_transition)
            elif rotate:
                cookie_to_set = self.tokens.issue(session_cookie)
            elif cookie_needs_set:
                cookie_to_set = current_token

            headers = [
                (name, value)
                for name, value in headers
                if not (
                    name.lower() == b"set-cookie"
                    and _cookie_name_from_set_cookie(value) == self.settings.cookie_name
                )
            ]
            if cookie_to_set is not None:
                headers.append(
                    (b"set-cookie", self.settings.cookie_header(cookie_to_set).encode("latin-1"))
                )
            response_state["token"] = cookie_to_set or current_token

            content_type = _header_text(_find_header(headers, b"content-type")) or ""
            content_encoding = _header_text(_find_header(headers, b"content-encoding"))
            capture_html = content_type.lower().startswith("text/html") and not content_encoding
            if not capture_html:
                await send({**message, "headers": headers})
                return

            response_state["start"] = {**message, "headers": headers}
            response_state["body"] = bytearray()
            response_state["charset"] = _response_charset(headers)
            response_state["streaming"] = False
            response_state["capture"] = True

        response_state: dict[str, Any] = {"capture": False}

        async def flush_injected(chunk: bytes, *, more_body: bool) -> None:
            """Inject into one already-form-aligned chunk and send it on."""

            charset = response_state["charset"]
            try:
                rendered = _inject_form_token(
                    chunk.decode(charset),
                    response_state["token"],
                    self.settings.field_name,
                )
                payload = rendered.encode(charset)
            except (UnicodeDecodeError, LookupError):
                # A body that is not decodable in its declared charset is
                # forwarded byte-for-byte rather than corrupted; it cannot
                # contain a form this middleware could safely rewrite.
                payload = chunk
            await send({"type": "http.response.body", "body": payload, "more_body": more_body})

        async def begin_streaming() -> None:
            """Switch to chunked injection once the body outgrows the buffer.

            The response is too large to hold whole, so `Content-Length` can no
            longer be recomputed and is dropped; the ASGI server frames the rest
            of the body itself.  Injection continues on every later chunk — a
            large page is never served without its tokens.
            """

            start = response_state.pop("start")
            headers = [
                (name, value)
                for name, value in start.get("headers", [])
                if name.lower() != b"content-length"
            ]
            response_state["streaming"] = True
            await send({**start, "headers": headers})

        async def send_capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                await send_with_csrf(message)
                return
            if not response_state.get("capture"):
                await send(message)
                return
            if message["type"] != "http.response.body":
                await send(message)
                return

            more_body = bool(message.get("more_body", False))
            response_body: bytearray = response_state["body"]
            response_body.extend(message.get("body", b""))

            if more_body and len(response_body) > self.settings.html_injection_limit_bytes:
                # Cut on a form boundary so no `<form>` block is ever split
                # across two injection passes, then hand the aligned prefix on
                # and keep the remainder for the next chunk.
                cut = _injectable_prefix_length(response_body)
                if cut > 0:
                    if not response_state.get("streaming"):
                        await begin_streaming()
                    chunk = bytes(response_body[:cut])
                    del response_body[:cut]
                    await flush_injected(chunk, more_body=True)
                return
            if more_body:
                return

            if response_state.get("streaming"):
                response_state["capture"] = False
                await flush_injected(bytes(response_body), more_body=False)
                return

            start = response_state.pop("start")
            response_state["capture"] = False
            raw_body = bytes(response_body)
            charset = response_state["charset"]
            try:
                html = raw_body.decode(charset)
                rendered = _inject_form_token(
                    html,
                    response_state["token"],
                    self.settings.field_name,
                )
                final_body = rendered.encode(charset)
            except (UnicodeDecodeError, LookupError):
                final_body = raw_body
            final_headers = _replace_content_length(start.get("headers", []), len(final_body))
            await send({**start, "headers": final_headers})
            await send({"type": "http.response.body", "body": final_body, "more_body": False})

        await self.app(scope, replay_receive, send_capture)

    def _is_valid_submission(
        self,
        submitted: str | None,
        cookie_token: str | None,
        session_cookie: str | None,
    ) -> bool:
        if submitted is None or cookie_token is None:
            return False
        return _constant_time_equal(submitted, cookie_token) and self.tokens.validate(
            submitted, session_cookie
        )

    async def _read_submitted_token(
        self, receive: Receive, content_type: str
    ) -> tuple[str | None, Receive]:
        lowered = content_type.lower()
        if lowered.startswith("multipart/form-data"):
            messages: list[Message] = []
            scanned = bytearray()
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return None, _replay_receive(messages, receive)
                messages.append(message)
                scanned.extend(message.get("body", b""))
                token = _multipart_token(bytes(scanned), self.settings.field_name)
                if token is not None:
                    return token, _replay_receive(messages, receive)
                if len(scanned) > self.settings.multipart_token_scan_bytes:
                    return None, _replay_receive(messages, receive)
                if not message.get("more_body", False):
                    return None, _replay_receive(messages, receive)

        messages = []
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return None, _replay_receive(messages, receive)
            messages.append(message)
            body.extend(message.get("body", b""))
            if len(body) > self.settings.max_form_body_bytes:
                return None, _replay_receive(messages, receive)
            if not message.get("more_body", False):
                break
        return _urlencoded_token(bytes(body), self.settings.field_name), _replay_receive(
            messages, receive
        )

    async def _send_rejection(
        self,
        send: Send,
        *,
        current_token: str,
        include_cookie: bool,
        status_code: int,
    ) -> None:
        body = b"CSRF validation failed."
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if include_cookie:
            headers.append(
                (b"set-cookie", self.settings.cookie_header(current_token).encode("latin-1"))
            )
        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})


_MULTIPART_TOKEN = re.compile(
    rb"(?is)Content-Disposition:\s*form-data;[^\r\n]*"
    rb"name=([\"'])([^\"']+)\1[^\r\n]*\r?\n"
    rb"(?:[^\r\n]*\r?\n)*?\r?\n([^\r\n]*)"
)


def _urlencoded_token(body: bytes, field_name: str) -> str | None:
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError:
        return None
    values = parsed.get(field_name, [])
    return values[0] if len(values) == 1 and values[0] else None


def _multipart_token(body: bytes, field_name: str) -> str | None:
    values: list[str] = []
    for match in _MULTIPART_TOKEN.finditer(body):
        try:
            name = match.group(2).decode("utf-8")
            value = match.group(3).decode("utf-8")
        except UnicodeDecodeError:
            continue
        if name == field_name:
            values.append(value)
    return values[0] if len(values) == 1 and values[0] else None


def _inject_form_token(html: str, token: str, field_name: str) -> str:
    from covenant_radar.security.headers import add_csrf_inputs

    return add_csrf_inputs(html, token, field_name=field_name)


_FORM_CLOSE_BYTES = re.compile(rb"(?i)</form\s*>")
_FORM_OPEN_BYTES = re.compile(rb"(?i)<form\b")
#: Bytes held back when a buffer ends outside any form, so a `<form` tag split
#: across two ASGI chunks is never cut in half.
_FORM_TAIL_GUARD = 16


def _injectable_prefix_length(buffer: bytes | bytearray) -> int:
    """Length of the longest prefix of `buffer` that holds only whole forms.

    Token injection rewrites complete `<form>…</form>` blocks, so a streamed
    body may only be cut where no form is open.  Returning ``0`` means the
    buffer currently ends inside a form and the caller must keep accumulating.
    """

    view = bytes(buffer)
    last_close = 0
    for match in _FORM_CLOSE_BYTES.finditer(view):
        last_close = match.end()
    pending_open = _FORM_OPEN_BYTES.search(view, last_close)
    if pending_open is not None:
        return pending_open.start()
    return max(last_close, len(view) - _FORM_TAIL_GUARD, 0)


def _parse_cookies(header: bytes) -> dict[str, str]:
    if not header:
        return {}
    parsed = SimpleCookie()
    try:
        parsed.load(header.decode("latin-1"))
    except (CookieError, UnicodeDecodeError):
        return {}
    return {key: morsel.value for key, morsel in parsed.items()}


def _header_map(headers: list[tuple[bytes, bytes]]) -> dict[bytes, bytes]:
    return {name.lower(): value for name, value in headers}


def _find_header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    name = name.lower()
    for header_name, value in headers:
        if header_name.lower() == name:
            return value
    return None


def _header_text(value: bytes | None) -> str | None:
    if value is None:
        return None
    return value.decode("latin-1", "ignore")


def _is_bearer_request(authorization: bytes, session_cookie: str | None) -> bool:
    return not session_cookie and authorization.lower().startswith(b"bearer ")


def _constant_time_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left, right)
    except (TypeError, ValueError):
        return False


def _cookie_name_from_set_cookie(header: bytes) -> str | None:
    name, separator, _ = header.partition(b"=")
    if not separator:
        return None
    return name.decode("latin-1", "ignore").strip()


def _session_cookie_transition(
    headers: list[tuple[bytes, bytes]], cookie_name: str
) -> tuple[bool, str | None]:
    found = False
    for name, value in headers:
        if name.lower() != b"set-cookie" or _cookie_name_from_set_cookie(value) != cookie_name:
            continue
        found = True
        parsed = SimpleCookie()
        try:
            parsed.load(value.decode("latin-1"))
        except (CookieError, UnicodeDecodeError):
            return True, None
        morsel = parsed.get(cookie_name)
        if morsel is None:
            return True, None
        if morsel["max-age"] == "0" or morsel["expires"].startswith("Thu, 01 Jan 1970"):
            return True, None
        return True, morsel.value or None
    return found, None


def _response_charset(headers: list[tuple[bytes, bytes]]) -> str:
    content_type = _header_text(_find_header(headers, b"content-type")) or ""
    match = re.search(r"(?i)(?:^|;)\s*charset=([\w.-]+)", content_type)
    return match.group(1) if match else "utf-8"


def _replace_content_length(
    headers: list[tuple[bytes, bytes]], length: int
) -> list[tuple[bytes, bytes]]:
    filtered = [(name, value) for name, value in headers if name.lower() != b"content-length"]
    return [*filtered, (b"content-length", str(length).encode("ascii"))]


def _replay_receive(messages: list[Message], receive: Receive) -> Receive:
    pending = list(messages)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay


def install_csrf(
    app: Any,
    settings: CSRFSettings | object | None = None,
    *,
    secret: bytes | str | None = None,
) -> None:
    """Install CSRF middleware on a Starlette/FastAPI application."""
    app.add_middleware(CSRFMiddleware, settings=settings, secret=secret)


CSRFProtectionMiddleware = CSRFMiddleware
CSRFTokenService = CSRFTokenManager


__all__ = [
    "CSRFError",
    "CSRFSettings",
    "CSRFProtectionMiddleware",
    "CSRFTokenManager",
    "CSRFTokenService",
    "CSRFValidationError",
    "CSRFMiddleware",
    "csrf_hidden_input",
    "csrf_token",
    "install_csrf",
    "mark_privilege_change",
    "rotate_csrf_token",
]
