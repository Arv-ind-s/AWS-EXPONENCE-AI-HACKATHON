"""API-key bearer authentication and per-key rate limiting.

Two independent pieces plug into the standing infrastructure without
changing it:

``ApiKeyAuthenticator`` implements `api/deps.py`'s ``ApiKeyAuthenticator``
protocol (a plain ``credential -> Principal | None`` callable). It is handed
to ``RequestPrincipalResolver(api_keys=...)`` exactly like a session store is
handed to that resolver's ``sessions`` argument. Revocation and expiry are
read fresh on every call — there is no cache to go stale, so a revoked key
stops authenticating immediately (`R-32.b`, `test_revocation_immediate`).

``ApiKeyRateLimitMiddleware`` enforces each key's own configured
``rate_limit_per_min``, independent of `security/ratelimit.py`'s coarser,
IP-keyed ``api`` category. It is deliberately a *separate* ASGI middleware
rather than a check inside the authenticator above: `web/middleware.py`'s
``RequestContextMiddleware`` resolves the principal eagerly, outside
FastAPI's own exception-handling middleware, so an exception raised from
inside the authenticator is turned into a generic 500 rather than the
documented ``429`` with ``Retry-After``. Emitting the ``429`` as a raw ASGI
response here — the same technique `security/ratelimit.py`'s own
``RateLimitMiddleware`` already uses for the IP-keyed limit — sidesteps that
ordering entirely. A deployment installs it with
``app.add_middleware(ApiKeyRateLimitMiddleware, lookup=...)`` (or the
``install_api_key_rate_limiting`` convenience below) after building the app,
exactly as ``ratelimit.py``'s own ``install_rate_limiting`` is applied.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, update
from starlette.types import ASGIApp, Receive, Scope, Send

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id
from covenant_radar.db.models.identity import ApiKey
from covenant_radar.db.session import SessionFactory
from covenant_radar.security.permissions import coerce_permission
from covenant_radar.security.ratelimit import (
    RATE_LIMIT_EXCEEDED_EVENT,
    InMemoryRateLimitStore,
    RateLimitAuditWriter,
    RateLimitDecision,
    RateLimitRule,
    RateLimitStore,
)
from covenant_radar.security.rbac import Principal

_LOGGER = logging.getLogger(__name__)
_MAX_CREDENTIAL_LENGTH = 256
_RATE_LIMIT_KEY_PREFIX = "api_key"


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """The persistence-neutral fields an authentication decision needs."""

    id: UUID
    scopes: tuple[str, ...]
    rate_limit_per_min: int
    revoked_at: datetime | None
    expires_at: datetime | None


class ApiKeyLookup(Protocol):
    """Persistence port shared by the authenticator and the rate limiter."""

    def by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        """Return the live record for a key's SHA-256 digest, or ``None``."""

    def touch_last_used(self, key_id: UUID, at: datetime) -> None:
        """Record that a key just authenticated a request."""


class SqlAlchemyApiKeyLookup:
    """A short-lived-session-per-call implementation of :class:`ApiKeyLookup`.

    Each call opens and closes its own session rather than sharing one across
    concurrent requests: unlike a service's unit-of-work-scoped session, this
    lookup is invoked from request-resolution code that runs before any
    per-request session exists.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        session = self._session_factory()
        try:
            row = session.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash)
            ).scalar_one_or_none()
            if row is None:
                return None
            return ApiKeyRecord(
                id=row.id,
                scopes=tuple(row.scopes),
                rate_limit_per_min=row.rate_limit_per_min,
                revoked_at=row.revoked_at,
                expires_at=row.expires_at,
            )
        finally:
            session.close()

    def touch_last_used(self, key_id: UUID, at: datetime) -> None:
        session = self._session_factory()
        try:
            session.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=at))
            session.commit()
        finally:
            session.close()


class ApiKeyAuthenticator:
    """Resolve a bearer credential to a scoped API-key :class:`Principal`."""

    def __init__(self, lookup: ApiKeyLookup, *, clock: Clock | None = None) -> None:
        self._lookup = lookup
        self._clock = clock or SystemClock()

    def __call__(self, credential: str) -> Principal | None:
        record = _resolve_record(self._lookup, credential)
        if record is None:
            return None
        now = _utc(self._clock.now())
        if not _record_is_usable(record, now):
            return None
        self._lookup.touch_last_used(record.id, now)
        return Principal.api_key(record.id, record.scopes)


class ApiKeyRateLimitMiddleware:
    """Enforce each API key's own ``rate_limit_per_min`` before it authenticates.

    Only requests carrying a ``Bearer`` credential that hashes to a *known*
    key are accounted against that key's budget; every other request passes
    through untouched, including one with an unknown or malformed bearer
    value — that case fails 401 downstream via the ordinary authenticator,
    which is cheap enough not to need its own limiter here.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        lookup: ApiKeyLookup,
        store: RateLimitStore | None = None,
        clock: Clock | None = None,
        audit: RateLimitAuditWriter | None = None,
    ) -> None:
        self.app = app
        self.lookup = lookup
        self.store = store or InMemoryRateLimitStore()
        self.clock = clock or SystemClock()
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        credential = _bearer_credential(scope)
        if credential is None:
            await self.app(scope, receive, send)
            return
        record = _resolve_record(self.lookup, credential)
        now = _utc(self.clock.now())
        if record is None or not _record_is_usable(record, now):
            await self.app(scope, receive, send)
            return
        decision = self.store.consume(
            f"{_RATE_LIMIT_KEY_PREFIX}:{record.id}",
            RateLimitRule.per_minute(record.rate_limit_per_min),
            now,
        )
        if not decision.allowed:
            self._audit_refusal(scope, record.id, decision)
            await _send_rate_limit_response(send, decision)
            return
        await self.app(scope, receive, send)

    def _audit_refusal(self, scope: Scope, key_id: UUID, decision: RateLimitDecision) -> None:
        audit = self.audit
        if audit is None:
            state = scope.get("state", {})
            candidate = state.get("audit_writer") if isinstance(state, Mapping) else None
            if candidate is not None and hasattr(candidate, "record"):
                audit = candidate
        if audit is None:
            _LOGGER.warning("API key rate limit exceeded: api_key_id=%s", key_id)
            return
        request_id = get_request_id() or "unknown"
        try:
            audit.record(
                RATE_LIMIT_EXCEEDED_EVENT,
                ("api_key", key_id),
                {
                    "category": "api_key",
                    "api_key_id": str(key_id),
                    "route": str(scope.get("path", "/")),
                    "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
                actor=None,
                request_id=request_id,
            )
        except Exception:
            _LOGGER.exception("API key rate-limit refusal audit failed for %s", key_id)


def install_api_key_rate_limiting(
    app: Any,
    *,
    lookup: ApiKeyLookup,
    store: RateLimitStore | None = None,
    clock: Clock | None = None,
    audit: RateLimitAuditWriter | None = None,
) -> None:
    """Install :class:`ApiKeyRateLimitMiddleware` on an existing application.

    Called after ``covenant_radar.asgi.create_app`` returns, the same way a
    deployment supplies its real ``routers`` and ``principal_resolver`` from
    outside that factory.
    """
    app.add_middleware(
        ApiKeyRateLimitMiddleware,
        lookup=lookup,
        store=store,
        clock=clock,
        audit=audit,
    )


def _resolve_record(lookup: ApiKeyLookup, credential: str) -> ApiKeyRecord | None:
    if not isinstance(credential, str) or not credential:
        return None
    if len(credential) > _MAX_CREDENTIAL_LENGTH:
        return None
    key_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    return lookup.by_hash(key_hash)


def _bearer_credential(scope: Scope) -> str | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
    for name, value in headers:
        if name == b"authorization":
            try:
                text: str = value.decode("latin-1")
            except UnicodeDecodeError:
                return None
            scheme, separator, credential = text.partition(" ")
            if separator == " " and scheme.lower() == "bearer" and credential:
                return credential
            return None
    return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("API-key timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _record_is_usable(record: ApiKeyRecord, now: datetime) -> bool:
    """Return whether a persisted record is safe to authenticate or limit.

    The database schema and :class:`ApiKeyService` validate these fields at
    write time, but authentication must also defend against legacy rows,
    manual database edits, and partially migrated data.  A malformed record
    is treated exactly like an unknown credential: it cannot gain access and
    it cannot consume a rate-limit bucket.  In particular, checking revocation
    and expiry here keeps the rate limiter from masking the required immediate
    ``401`` response for a disabled credential.
    """
    if not isinstance(record, ApiKeyRecord) or not isinstance(record.id, UUID):
        return False
    if (
        isinstance(record.rate_limit_per_min, bool)
        or not isinstance(record.rate_limit_per_min, int)
        or record.rate_limit_per_min <= 0
    ):
        return False
    if not record.scopes:
        return False
    try:
        if any(not isinstance(scope, str) for scope in record.scopes):
            return False
        tuple(coerce_permission(scope) for scope in record.scopes)
    except (TypeError, ValueError):
        return False
    if record.revoked_at is not None:
        return False
    if record.expires_at is not None:
        try:
            return now < _utc(record.expires_at)
        except (AttributeError, TypeError, ValueError):
            return False
    return True


async def _send_rate_limit_response(send: Send, decision: RateLimitDecision) -> None:
    body = b"API key rate limit exceeded."
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"cache-control", b"no-store"),
        (b"retry-after", str(decision.retry_after_seconds).encode("ascii")),
        (b"x-ratelimit-limit", str(decision.limit).encode("ascii")),
        (b"x-ratelimit-remaining", b"0"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 429, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


__all__ = [
    "ApiKeyAuthenticator",
    "ApiKeyLookup",
    "ApiKeyRateLimitMiddleware",
    "ApiKeyRecord",
    "SqlAlchemyApiKeyLookup",
    "install_api_key_rate_limiting",
]
