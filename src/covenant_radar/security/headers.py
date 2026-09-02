"""HTTP security headers and the local-origin content policy.

The middleware in this module is deliberately a small ASGI middleware rather
than a framework-specific dependency.  That keeps the policy active for
HTML, API and error responses alike and makes it usable by the eventual
application factory without coupling the security boundary to a route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityHeadersError(ValueError):
    """Raised when a security policy would weaken the application's boundary."""


@dataclass(frozen=True, slots=True)
class SecurityHeadersSettings:
    """Validated browser-security header settings.

    The default policy intentionally has no network origin other than the
    application itself.  Inline JavaScript and inline styles are not enabled;
    a nonce/hash-based policy can be supplied by a deployment when required.
    """

    content_security_policy: str = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-hashes' 'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "worker-src 'self'; "
        "manifest-src 'self'; "
        "media-src 'self'; "
        "frame-src 'none'"
    )
    hsts_max_age: int = 31_536_000
    hsts_include_subdomains: bool = True
    hsts_preload: bool = False
    referrer_policy: str = "strict-origin-when-cross-origin"

    def __post_init__(self) -> None:
        if self.hsts_max_age < 0:
            raise SecurityHeadersError("HSTS max-age must not be negative.")
        if not self.referrer_policy:
            raise SecurityHeadersError("Referrer-Policy must not be empty.")
        validate_content_security_policy(self.content_security_policy)

    @classmethod
    def from_settings(cls, settings: object) -> SecurityHeadersSettings:
        """Build headers from a security-settings object with safe defaults.

        T-019 predates the final settings fields, so this adapter accepts both
        the current Pydantic object and a future object that exposes the
        hardening values.  Unknown or absent values never weaken the defaults.
        """
        source = getattr(settings, "security", settings)
        defaults = SecurityHeadersSettings()
        return cls(
            content_security_policy=str(
                getattr(source, "content_security_policy", defaults.content_security_policy)
            ),
            hsts_max_age=int(getattr(source, "hsts_max_age", defaults.hsts_max_age)),
            hsts_include_subdomains=bool(
                getattr(source, "hsts_include_subdomains", defaults.hsts_include_subdomains)
            ),
            hsts_preload=bool(getattr(source, "hsts_preload", defaults.hsts_preload)),
            referrer_policy=str(getattr(source, "referrer_policy", defaults.referrer_policy)),
        )

    @property
    def strict_transport_security(self) -> str:
        """Return the canonical HSTS header value."""
        directives = [f"max-age={self.hsts_max_age}"]
        if self.hsts_include_subdomains:
            directives.append("includeSubDomains")
        if self.hsts_preload:
            directives.append("preload")
        return "; ".join(directives)

    def as_headers(self) -> dict[str, str]:
        """Return the complete set of headers owned by this policy."""
        return {
            "content-security-policy": self.content_security_policy,
            "strict-transport-security": self.strict_transport_security,
            "x-content-type-options": "nosniff",
            "referrer-policy": self.referrer_policy,
            "x-frame-options": "DENY",
        }


_EXTERNAL_ORIGIN = re.compile(r"(?i)(?:https?:)?//[^\s\"'<>;()]+")
_EXTERNAL_SCHEME = re.compile(r"(?i)(?:https?|ftp|ws|wss):")
_FORM_BLOCK = re.compile(r"(?is)(<form\b[^>]*>)(.*?)(</form\s*>)")
_STATE_CHANGING_METHOD = re.compile(r"(?i)\bmethod\s*=\s*(['\"]?)(post|put|patch|delete)\1")

_REQUIRED_DIRECTIVES: Final[dict[str, tuple[str, ...]]] = {
    "default-src": ("'self'",),
    "base-uri": ("'self'",),
    "object-src": ("'none'",),
    "frame-ancestors": ("'none'",),
    "form-action": ("'self'",),
}


def validate_content_security_policy(policy: str) -> None:
    """Reject policies with external origins or unsafe required directives."""
    if not isinstance(policy, str) or not policy.strip():
        raise SecurityHeadersError("Content-Security-Policy must not be empty.")
    if _EXTERNAL_ORIGIN.search(policy) or _EXTERNAL_SCHEME.search(policy):
        raise SecurityHeadersError("Content-Security-Policy must not contain an external origin.")
    lowered = policy.lower()
    if "'unsafe-inline'" in lowered or "'unsafe-eval'" in lowered:
        raise SecurityHeadersError(
            "Content-Security-Policy must not enable unsafe inline code or evaluation."
        )

    directives: dict[str, tuple[str, ...]] = {}
    for raw_directive in policy.split(";"):
        parts = tuple(part for part in raw_directive.strip().split() if part)
        if not parts:
            continue
        name = parts[0].lower()
        if name in directives:
            raise SecurityHeadersError(f"Content-Security-Policy repeats directive {name!r}.")
        directives[name] = parts[1:]
    for name, required_sources in _REQUIRED_DIRECTIVES.items():
        values = directives.get(name)
        if values is None or any(source not in values for source in required_sources):
            raise SecurityHeadersError(
                f"Content-Security-Policy must include {name} with {required_sources[0]}."
            )


class SecurityHeadersMiddleware:
    """Add the enforced browser-security headers to every HTTP response."""

    def __init__(
        self,
        app: ASGIApp,
        settings: SecurityHeadersSettings | object | None = None,
    ) -> None:
        self.app = app
        if settings is None:
            self.settings = SecurityHeadersSettings()
        elif isinstance(settings, SecurityHeadersSettings):
            self.settings = settings
        else:
            self.settings = SecurityHeadersSettings.from_settings(settings)
        self._headers = tuple(
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in self.settings.as_headers().items()
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return
            owned_names = {name for name, _ in self._headers}
            existing = [
                (name, value)
                for name, value in message.get("headers", [])
                if name.lower() not in owned_names
            ]
            await send({**message, "headers": [*existing, *self._headers]})

        await self.app(scope, receive, send_with_headers)


@dataclass(frozen=True, slots=True)
class TemplateOrigin:
    """One external-origin reference found in a template source file."""

    path: Path
    line: int
    reference: str


def find_external_origins(template_directory: Path | str) -> tuple[TemplateOrigin, ...]:
    """Find literal external origins in HTML, CSS and JavaScript templates."""
    directory = Path(template_directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Template directory does not exist: {directory}")

    findings: list[TemplateOrigin] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".html", ".htm", ".css", ".js"}:
            continue
        findings.extend(find_external_origins_in_text(path.read_text(encoding="utf-8"), path=path))
    return tuple(findings)


def find_external_origins_in_text(
    content: str, *, path: Path | str = "<rendered-template>"
) -> tuple[TemplateOrigin, ...]:
    """Find external origins in already-rendered template content."""
    template_path = Path(path)
    return tuple(
        TemplateOrigin(
            path=template_path,
            line=content.count("\n", 0, match.start()) + 1,
            reference=match.group(0),
        )
        for match in _EXTERNAL_ORIGIN.finditer(content)
    )


def assert_no_external_origins(template_directory: Path | str) -> None:
    """Raise an actionable error when a local template references a network origin."""
    findings = find_external_origins(template_directory)
    if not findings:
        return
    details = ", ".join(
        f"{finding.path}:{finding.line} ({finding.reference})" for finding in findings
    )
    raise SecurityHeadersError(f"External template origin(s) are forbidden: {details}.")


def assert_no_external_origins_in_text(
    content: str, *, path: Path | str = "<rendered-template>"
) -> None:
    """Raise when rendered content contains a literal network origin."""
    findings = find_external_origins_in_text(content, path=path)
    if not findings:
        return
    details = ", ".join(
        f"{finding.path}:{finding.line} ({finding.reference})" for finding in findings
    )
    raise SecurityHeadersError(f"External rendered origin(s) are forbidden: {details}.")


def add_csrf_inputs(html: str, token: str, *, field_name: str = "csrf_token") -> str:
    """Add a hidden token to state-changing HTML forms that lack one.

    This only inserts markup at a form boundary. It does not rewrite arbitrary
    HTML and therefore cannot turn text or attribute values into executable
    markup. Templates may also render their own token field; those forms are
    left unchanged.
    """
    if not token:
        raise ValueError("A non-empty CSRF token is required.")
    safe_token = escape(token, quote=True)
    field_pattern = re.compile(
        rf"(?is)\bname\s*=\s*(?:(['\"]){re.escape(field_name)}\1|"
        rf"{re.escape(field_name)})(?=\s|=|>)"
    )

    field = f'<input type="hidden" name="{escape(field_name, quote=True)}" value="{safe_token}">\n'

    def replace_form_block(match: re.Match[str]) -> str:
        opening_tag, contents, closing_tag = match.groups()
        if not _STATE_CHANGING_METHOD.search(opening_tag):
            return match.group(0)
        if field_pattern.search(opening_tag) or field_pattern.search(contents):
            return match.group(0)
        return opening_tag + "\n" + field + contents + closing_tag

    rendered = _FORM_BLOCK.sub(replace_form_block, html)
    return rendered


def install_security_headers(
    app: Any,
    settings: SecurityHeadersSettings | object | None = None,
) -> None:
    """Install the middleware on a Starlette/FastAPI application."""
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)


__all__ = [
    "SecurityHeadersError",
    "SecurityHeadersMiddleware",
    "SecurityHeadersSettings",
    "TemplateOrigin",
    "add_csrf_inputs",
    "assert_no_external_origins",
    "assert_no_external_origins_in_text",
    "find_external_origins",
    "find_external_origins_in_text",
    "install_security_headers",
    "validate_content_security_policy",
]
