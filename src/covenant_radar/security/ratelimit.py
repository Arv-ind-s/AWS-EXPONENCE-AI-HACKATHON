"""Configurable request rate limiting with an injectable storage port."""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from threading import RLock
from typing import Any, Protocol

from starlette.types import ASGIApp, Receive, Scope, Send

from covenant_radar.core.clock import Clock, SystemClock

_LOGGER = logging.getLogger(__name__)
RATE_LIMIT_EXCEEDED_EVENT = "rate_limit_exceeded"


class RateLimitConfigurationError(ValueError):
    """Raised when a rate-limit policy is unsafe or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """A fixed-window request limit."""

    limit: int
    window: timedelta

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise RateLimitConfigurationError("Rate-limit count must be positive.")
        if self.window <= timedelta(0):
            raise RateLimitConfigurationError("Rate-limit window must be positive.")

    @classmethod
    def per_minute(cls, limit: int) -> RateLimitRule:
        """Build a rule whose window is one minute."""
        return cls(limit=limit, window=timedelta(minutes=1))


@dataclass(frozen=True, slots=True)
class RateLimitSettings:
    """The three product rate-limit classes and their path mapping."""

    authentication: RateLimitRule = RateLimitRule(limit=10, window=timedelta(minutes=1))
    password_reset: RateLimitRule = RateLimitRule(limit=5, window=timedelta(minutes=15))
    api: RateLimitRule = RateLimitRule(limit=120, window=timedelta(minutes=1))
    api_prefix: str = "/api/"
    password_prefixes: tuple[str, ...] = ("/password/", "/forgot-password", "/reset-password")
    authentication_prefixes: tuple[str, ...] = (
        "/sign-in",
        "/sign-out",
        "/mfa/",
        "/sso/",
    )

    def __post_init__(self) -> None:
        if not self.api_prefix.startswith("/"):
            raise RateLimitConfigurationError("API rate-limit prefix must be an absolute path.")
        for prefix in (*self.password_prefixes, *self.authentication_prefixes):
            if not prefix.startswith("/"):
                raise RateLimitConfigurationError("Rate-limit path prefixes must be absolute.")

    @classmethod
    def from_settings(cls, settings: object) -> RateLimitSettings:
        """Build policy values from security settings, retaining safe defaults."""
        security = getattr(settings, "security", settings)
        defaults = RateLimitSettings()

        def rule(
            *,
            limit_name: str,
            window_name: str,
            fallback: RateLimitRule,
        ) -> RateLimitRule:
            limit = int(getattr(security, limit_name, fallback.limit))
            window_seconds = int(
                getattr(security, window_name, int(fallback.window.total_seconds()))
            )
            return RateLimitRule(limit=limit, window=timedelta(seconds=window_seconds))

        return cls(
            authentication=rule(
                limit_name="authentication_rate_limit",
                window_name="authentication_rate_window_seconds",
                fallback=defaults.authentication,
            ),
            password_reset=rule(
                limit_name="password_reset_rate_limit",
                window_name="password_reset_rate_window_seconds",
                fallback=defaults.password_reset,
            ),
            api=rule(
                limit_name="api_rate_limit",
                window_name="api_rate_window_seconds",
                fallback=defaults.api,
            ),
            api_prefix=str(getattr(security, "api_rate_limit_prefix", defaults.api_prefix)),
        )


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The result of one atomic rate-limit check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    reset_at: datetime

    @property
    def retry_after(self) -> int:
        """Short alias used by HTTP response adapters."""
        return self.retry_after_seconds


class RateLimitStore(Protocol):
    """Storage port for an atomic per-key fixed-window check."""

    def consume(self, key: str, rule: RateLimitRule, now: datetime) -> RateLimitDecision:
        """Record a request and return the resulting decision."""


class RateLimitAuditWriter(Protocol):
    """Minimal audit port used when a request is refused."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an audit event."""


class InMemoryRateLimitStore:
    """Thread-safe single-process store with bounded key cardinality."""

    def __init__(self, *, max_keys: int = 10_000) -> None:
        if max_keys <= 0:
            raise RateLimitConfigurationError("Rate-limit store key capacity must be positive.")
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[datetime]] = OrderedDict()
        self._lock = RLock()

    def consume(self, key: str, rule: RateLimitRule, now: datetime) -> RateLimitDecision:
        instant = _utc(now)
        cutoff = instant - rule.window
        with self._lock:
            timestamps = self._hits.get(key)
            if timestamps is None:
                if len(self._hits) >= self.max_keys:
                    self._hits.popitem(last=False)
                timestamps = deque()
                self._hits[key] = timestamps
            else:
                self._hits.move_to_end(key)
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= rule.limit:
                reset_at = timestamps[0] + rule.window
                return RateLimitDecision(
                    allowed=False,
                    limit=rule.limit,
                    remaining=0,
                    retry_after_seconds=max(1, ceil((reset_at - instant).total_seconds())),
                    reset_at=reset_at,
                )
            timestamps.append(instant)
            reset_at = instant + rule.window
            return RateLimitDecision(
                allowed=True,
                limit=rule.limit,
                remaining=rule.limit - len(timestamps),
                retry_after_seconds=0,
                reset_at=reset_at,
            )

    def key_count(self) -> int:
        """Return the current number of tracked keys for operational tests."""
        with self._lock:
            return len(self._hits)


class RateLimiter:
    """Apply named policies against a replaceable storage implementation."""

    def __init__(
        self,
        *,
        settings: RateLimitSettings | None = None,
        store: RateLimitStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings or RateLimitSettings()
        self.store = store or InMemoryRateLimitStore()
        self.clock = clock or SystemClock()

    def rule_for(self, category: str) -> RateLimitRule:
        """Resolve a policy category or raise a configuration error."""
        rules = {
            "authentication": self.settings.authentication,
            "password_reset": self.settings.password_reset,
            "api": self.settings.api,
        }
        try:
            return rules[category]
        except KeyError as error:
            raise RateLimitConfigurationError(
                f"Unknown rate-limit category: {category!r}."
            ) from error

    def check(
        self,
        category: str,
        key: str,
        *,
        now: datetime | None = None,
    ) -> RateLimitDecision:
        """Atomically account for one request under *category* and *key*."""
        if not key:
            raise ValueError("A non-empty rate-limit key is required.")
        instant = _utc(now or self.clock.now())
        return self.store.consume(f"{category}:{key}", self.rule_for(category), instant)

    def category_for_path(self, path: str) -> str | None:
        """Return the sensitive request class for an application path."""
        if path.startswith(self.settings.api_prefix):
            return "api"
        if any(path.startswith(prefix) for prefix in self.settings.password_prefixes):
            return "password_reset"
        if any(path.startswith(prefix) for prefix in self.settings.authentication_prefixes):
            return "authentication"
        return None


class RateLimitMiddleware:
    """Reject requests over policy before the route handler executes."""

    def __init__(
        self,
        app: ASGIApp,
        settings: RateLimitSettings | object | None = None,
        *,
        limiter: RateLimiter | None = None,
        audit: RateLimitAuditWriter | None = None,
        clock: Clock | None = None,
        key_resolver: Callable[[Scope], str] | None = None,
    ) -> None:
        self.app = app
        if limiter is not None:
            self.limiter = limiter
        else:
            configured = (
                settings
                if isinstance(settings, RateLimitSettings)
                else RateLimitSettings.from_settings(settings)
                if settings is not None
                else RateLimitSettings()
            )
            self.limiter = RateLimiter(settings=configured, clock=clock)
        self.audit = audit
        self.key_resolver = key_resolver or _client_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        category = self.limiter.category_for_path(str(scope.get("path", "/")))
        if category is not None:
            decision = self.limiter.check(category, self.key_resolver(scope))
            if not decision.allowed:
                self._audit_refusal(scope, category, decision)
                await _send_rate_limit_response(send, decision)
                return
        await self.app(scope, receive, send)

    def _audit_refusal(self, scope: Scope, category: str, decision: RateLimitDecision) -> None:
        audit = self.audit
        if audit is None:
            state_audit = scope.get("state", {}).get("audit_writer")
            if state_audit is not None and hasattr(state_audit, "record"):
                audit = state_audit
        if audit is None:
            application_state = getattr(self.app, "state", None)
            configured_audit = getattr(application_state, "audit_writer", None)
            if configured_audit is not None and hasattr(configured_audit, "record"):
                audit = configured_audit
        if audit is None:
            _LOGGER.warning(
                "Rate limit exceeded: category=%s path=%s",
                category,
                scope.get("path", "/"),
            )
            return
        state = scope.get("state", {})
        request_id = str(state.get("request_id", "unknown"))
        payload = {
            "category": category,
            "route": str(scope.get("path", "/")),
            "limit": decision.limit,
            "retry_after_seconds": decision.retry_after_seconds,
        }
        try:
            audit.record(
                RATE_LIMIT_EXCEEDED_EVENT,
                ("route", str(scope.get("path", "/"))),
                payload,
                actor=None,
                request_id=request_id,
            )
        except Exception:
            _LOGGER.exception("Rate-limit refusal audit failed for %s", scope.get("path", "/"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Rate-limit timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _client_key(scope: Scope) -> str:
    client = scope.get("client")
    if isinstance(client, tuple | list) and client and isinstance(client[0], str):
        return client[0]
    return "unknown-client"


async def _send_rate_limit_response(send: Send, decision: RateLimitDecision) -> None:
    body = b"Rate limit exceeded."
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


def install_rate_limiting(
    app: Any,
    settings: RateLimitSettings | object | None = None,
    *,
    limiter: RateLimiter | None = None,
    audit: RateLimitAuditWriter | None = None,
    clock: Clock | None = None,
) -> None:
    """Install rate limiting on a Starlette/FastAPI application."""
    app.add_middleware(
        RateLimitMiddleware,
        settings=settings,
        limiter=limiter,
        audit=audit,
        clock=clock,
    )


__all__ = [
    "InMemoryRateLimitStore",
    "RATE_LIMIT_EXCEEDED_EVENT",
    "RateLimitAuditWriter",
    "RateLimitConfigurationError",
    "RateLimitDecision",
    "RateLimitMiddleware",
    "RateLimitRule",
    "RateLimitSettings",
    "RateLimitStore",
    "RateLimiter",
    "install_rate_limiting",
]
