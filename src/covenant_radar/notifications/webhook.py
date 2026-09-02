"""Signed webhook delivery for the notification pipeline.

The adapter accepts only the fully rendered, scope-filtered
``OutboundMessage`` produced by ``NotificationService``.  It does not load
application records, follow redirects, or expose response bodies.  Delivery
attempts and dead-letter state remain the responsibility of the notification
service, which stores them in the durable notification row.

The wire format is intentionally small and deterministic::

    {
      "data": { ... },
      "event_id": "...",
      "occurred_at": "2026-08-31T09:00:00+00:00",
      "subject": {"id": "...", "type": "borrower"},
      "type": "band_change",
      "version": 1
    }

The signature is HMAC-SHA256 over ``<unix_timestamp>.<raw_body>`` and is sent
as ``v1=<hex digest>`` in ``X-Covenant-Radar-Signature``.  The event ID is
stable for a message across retries, allowing receivers to deduplicate
redeliveries safely.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import RLock
from types import MappingProxyType
from typing import Final, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from covenant_radar.config.settings import NotificationsSettings
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.ports.notifier import (
    DeliveryResult,
    DeliveryStatus,
    NotificationChannel,
    OutboundMessage,
)

_MAX_ENDPOINT_URL_LENGTH: Final[int] = 2_048
_MAX_TIMEOUT_SECONDS: Final[float] = 120.0
_MAX_REPLAY_WINDOW_SECONDS: Final[int] = 86_400
_MAX_RETRY_AFTER_SECONDS: Final[int] = 3_600
_MAX_PAYLOAD_BYTES: Final[int] = 1_048_576
_MAX_ERROR_LENGTH: Final[int] = 2_000
_MAX_PROVIDER_ID_LENGTH: Final[int] = 200
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
_DEFAULT_REPLAY_WINDOW_SECONDS: Final[int] = 300
_USER_AGENT: Final[str] = "covenant-radar-webhook/1"
_SIGNATURE_HEADER: Final[str] = "X-Covenant-Radar-Signature"
_TIMESTAMP_HEADER: Final[str] = "X-Covenant-Radar-Timestamp"
_EVENT_ID_HEADER: Final[str] = "X-Covenant-Radar-Event-ID"
_EVENT_TYPE_HEADER: Final[str] = "X-Covenant-Radar-Event-Type"
_SIGNATURE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v1=[0-9a-f]{64}$")

# Webhooks carry references and derived values only.  Matching field names
# recursively makes an accidental personal-data slot fail closed before any
# request is sent.  Corporate identifiers such as CINs and opaque UUIDs are
# references and remain permitted.
_PERSONAL_FIELD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[_\-.])(?:aadhaar|account_number|address|bank_account|contact_name|"
    r"customer_name|date_of_birth|director|director_name|dob|email|first_name|full_name|"
    r"last_name|middle_name|mobile|national_id|passport|pan|personal|personal_phone|phone|"
    r"person|person_name|promoter|promoter_name|signatory|signatory_name|tax_id|telephone|"
    r"username)(?:$|[_\-.])",
    re.IGNORECASE,
)


class WebhookTransport(Protocol):
    """The HTTP client surface used by :class:`WebhookNotifier`."""

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        """Submit one webhook request."""
        ...


EndpointResolver = Mapping[UUID, object] | Callable[[UUID], object | None]


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    """One recipient's validated webhook destination."""

    url: str
    endpoint_id: str = "default"
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _validate_url(self.url))
        endpoint_id = _bounded_text(self.endpoint_id, "endpoint_id", 100)
        if not isinstance(self.enabled, bool):
            raise TypeError("WebhookEndpoint.enabled must be a boolean.")
        object.__setattr__(self, "endpoint_id", endpoint_id)


class WebhookEndpointRegistry:
    """Thread-safe recipient endpoint registry used by workers and tests.

    Endpoint configuration is deployment-owned.  Removing an endpoint is
    deliberately observable: queued notifications resolve to no target and
    are dead-lettered with the reason, rather than being silently discarded.
    """

    def __init__(
        self,
        endpoints: Mapping[UUID | str, WebhookEndpoint | str] | None = None,
    ) -> None:
        self._endpoints: dict[UUID, WebhookEndpoint] = {}
        self._lock = RLock()
        for recipient_id, endpoint in (endpoints or {}).items():
            self.register(recipient_id, endpoint)

    def register(
        self,
        recipient_id: UUID | str,
        endpoint: WebhookEndpoint | str,
    ) -> WebhookEndpoint:
        """Add or replace a recipient endpoint after validating it."""

        normalized_id = _uuid(recipient_id, "recipient_id")
        resolved = _endpoint(endpoint)
        with self._lock:
            self._endpoints[normalized_id] = resolved
        return resolved

    add = register
    set = register

    def remove(self, recipient_id: UUID | str) -> bool:
        """Remove an endpoint and report whether one existed."""

        normalized_id = _uuid(recipient_id, "recipient_id")
        with self._lock:
            return self._endpoints.pop(normalized_id, None) is not None

    delete = remove

    def resolve(self, recipient_id: UUID) -> WebhookEndpoint | None:
        """Return a snapshot of the current endpoint, if configured."""

        if not isinstance(recipient_id, UUID):
            raise TypeError("recipient_id must be a UUID.")
        with self._lock:
            return self._endpoints.get(recipient_id)

    def snapshot(self) -> Mapping[UUID, WebhookEndpoint]:
        """Return an immutable endpoint snapshot for diagnostics."""

        with self._lock:
            return MappingProxyType(dict(self._endpoints))

    def __call__(self, recipient_id: UUID) -> WebhookEndpoint | None:
        return self.resolve(recipient_id)


@dataclass(frozen=True, slots=True)
class WebhookCapability:
    """Configuration state safe to show on an administrator screen."""

    configured: bool
    detail: str


class WebhookNotifier:
    """C-54 webhook adapter with signing and bounded HTTP delivery."""

    def __init__(
        self,
        settings: NotificationsSettings | None = None,
        *,
        signing_secret: str | bytes | object | None = None,
        endpoints: EndpointResolver | None = None,
        endpoint_resolver: EndpointResolver | None = None,
        endpoint: WebhookEndpoint | str | None = None,
        webhook_url: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        replay_window_seconds: int = _DEFAULT_REPLAY_WINDOW_SECONDS,
        clock: Clock | None = None,
        http_client: WebhookTransport | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if settings is not None and not isinstance(settings, NotificationsSettings):
            raise TypeError("settings must be NotificationsSettings.")
        if endpoints is not None and endpoint_resolver is not None:
            raise ValueError("Specify webhook endpoints only once.")
        resolved_endpoints = endpoint_resolver if endpoint_resolver is not None else endpoints
        if endpoint is not None and webhook_url is not None:
            raise ValueError("Specify a webhook endpoint only once.")
        if webhook_url is not None:
            endpoint = webhook_url
        if endpoint is not None:
            if resolved_endpoints is not None:
                raise ValueError("Specify webhook endpoints only once.")
            static_endpoint = _endpoint(endpoint)

            def resolve_static_endpoint(_recipient_id: UUID) -> WebhookEndpoint:
                return static_endpoint

            resolved_endpoints = resolve_static_endpoint
        if resolved_endpoints is not None and not (
            isinstance(resolved_endpoints, Mapping) or callable(resolved_endpoints)
        ):
            raise TypeError("endpoints must be a mapping or callable.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be greater than 0 and at most 120.")
        if (
            isinstance(replay_window_seconds, bool)
            or not isinstance(replay_window_seconds, int)
            or not 1 <= replay_window_seconds <= _MAX_REPLAY_WINDOW_SECONDS
        ):
            raise ValueError("replay_window_seconds must be between 1 and 86400.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("clock must expose now().")
        if http_client is not None and not callable(getattr(http_client, "post", None)):
            raise TypeError("http_client must expose post(url, **kwargs).")
        if http_client is not None and transport is not None:
            raise ValueError("transport cannot be supplied with http_client.")

        source_secret = (
            signing_secret
            if signing_secret is not None
            else (settings.webhook_signing_secret if settings is not None else None)
        )
        self._secret = _secret_bytes(source_secret)
        self._enabled = settings.webhooks_enabled if settings is not None else True
        self._endpoints = resolved_endpoints
        self.timeout_seconds = float(timeout_seconds)
        self.replay_window_seconds = replay_window_seconds
        self.clock = clock or SystemClock()
        self._owns_client = http_client is None
        if http_client is not None:
            self._http_client = http_client
        else:
            self._http_client = cast(
                WebhookTransport,
                httpx.Client(
                    transport=transport,
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    verify=True,
                    trust_env=False,
                ),
            )

    @property
    def capability(self) -> WebhookCapability:
        """Return an administrator-safe configuration diagnostic."""

        if not self._enabled:
            return WebhookCapability(False, "Webhook delivery is disabled.")
        if self._secret is None:
            return WebhookCapability(
                False,
                "Webhook signing secret is not configured; set "
                "notifications.webhook_signing_secret.",
            )
        if self._endpoints is None:
            return WebhookCapability(False, "No webhook endpoint resolver is configured.")
        return WebhookCapability(True, "configured")

    @property
    def configuration(self) -> WebhookCapability:
        """Compatibility alias for the administrator-facing capability."""

        return self.capability

    @property
    def is_configured(self) -> bool:
        """Whether this adapter can attempt delivery."""

        return self.capability.configured

    @property
    def configuration_notice(self) -> str:
        """Explain a degraded configuration without exposing the secret."""

        return self.capability.detail

    def send(self, message: OutboundMessage) -> DeliveryResult:
        """Send one signed webhook or return an explicit delivery outcome."""

        if not isinstance(message, OutboundMessage):
            raise TypeError("WebhookNotifier.send requires an OutboundMessage.")
        if message.channel is not NotificationChannel.WEBHOOK:
            raise ValueError("WebhookNotifier can send only webhook-channel messages.")
        capability = self.capability
        if not capability.configured:
            return DeliveryResult(DeliveryStatus.DEAD_LETTERED, error=capability.detail)

        endpoint = self._resolve_endpoint(message.recipient_id)
        if endpoint is None or not endpoint.enabled:
            return DeliveryResult(
                DeliveryStatus.DEAD_LETTERED,
                error="webhook endpoint was removed or is disabled",
            )
        try:
            payload = validate_webhook_payload(message.payload)
            body, event_id = _envelope(message, payload)
        except (TypeError, ValueError) as error:
            return DeliveryResult(
                DeliveryStatus.DEAD_LETTERED,
                error=f"webhook payload refused: {_safe_error(error)}",
            )

        now = _aware_utc(self.clock.now(), "clock.now()")
        timestamp = str(int(now.timestamp()))
        signature = sign_payload(body, timestamp, self._secret)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            _TIMESTAMP_HEADER: timestamp,
            _SIGNATURE_HEADER: signature,
            _EVENT_ID_HEADER: event_id,
            _EVENT_TYPE_HEADER: message.template,
            "Idempotency-Key": event_id,
        }
        try:
            response = self._http_client.post(
                endpoint.url,
                content=body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            return DeliveryResult(DeliveryStatus.RETRY, error="webhook request timed out")
        except httpx.RequestError as error:
            return DeliveryResult(
                DeliveryStatus.RETRY,
                error=f"webhook transport failed: {type(error).__name__}",
            )
        except Exception as error:
            return DeliveryResult(
                DeliveryStatus.RETRY,
                error=f"webhook adapter failed: {type(error).__name__}",
            )
        return _response_result(response, event_id, now)

    deliver = send
    send_webhook = send

    def close(self) -> None:
        """Close an internally-created HTTP client."""

        if self._owns_client:
            close = getattr(self._http_client, "close", None)
            if callable(close):
                close()

    def verify(
        self,
        body: bytes | str,
        timestamp: int | str,
        signature: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Verify a webhook using this adapter's configured replay window."""

        instant = _aware_utc(self.clock.now() if now is None else now, "now")
        return verify_signature(
            body,
            timestamp,
            signature,
            self._secret,
            now=instant,
            tolerance_seconds=self.replay_window_seconds,
        )

    def __enter__(self) -> WebhookNotifier:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _resolve_endpoint(self, recipient_id: UUID) -> WebhookEndpoint | None:
        resolver = self._endpoints
        if resolver is None:
            return None
        try:
            raw = resolver(recipient_id) if callable(resolver) else resolver.get(recipient_id)
        except (KeyError, TypeError, ValueError):
            return None
        if raw is None and isinstance(resolver, Mapping):
            try:
                raw = cast(Mapping[object, object], resolver).get(str(recipient_id))
            except (KeyError, TypeError, ValueError):
                return None
        try:
            return _endpoint(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None


def validate_webhook_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Validate and copy the non-personal structured webhook data.

    A webhook payload is not a general-purpose record serializer.  Its keys
    must be safe field names and its values must be JSON primitives,
    containers, or null.  Unknown personal-class field names fail closed.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("webhook payload must be a mapping")
    result = _safe_mapping(payload, path="payload")
    encoded = _canonical_json(result)
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValueError("webhook payload exceeds the 1 MiB limit")
    return result


def sign_payload(
    body: bytes | str,
    timestamp: int | str,
    secret: str | bytes | object,
) -> str:
    """Return the versioned HMAC signature for one raw request body."""

    body_bytes = _body_bytes(body)
    timestamp_text = _timestamp_text(timestamp)
    key = _secret_bytes(secret)
    if key is None:
        raise ValueError("webhook signing secret is required")
    signed = timestamp_text.encode("ascii") + b"." + body_bytes
    digest = hmac.new(key, signed, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def verify_signature(
    body: bytes | str,
    timestamp: int | str,
    signature: str,
    secret: str | bytes | object,
    *,
    now: datetime,
    tolerance_seconds: int = _DEFAULT_REPLAY_WINDOW_SECONDS,
) -> bool:
    """Verify a receiver's signature and reject stale or future requests."""

    if not isinstance(signature, str) or _SIGNATURE_PATTERN.fullmatch(signature) is None:
        return False
    if (
        isinstance(tolerance_seconds, bool)
        or not isinstance(tolerance_seconds, int)
        or not 1 <= tolerance_seconds <= _MAX_REPLAY_WINDOW_SECONDS
    ):
        raise ValueError("tolerance_seconds must be between 1 and 86400")
    try:
        timestamp_text = _timestamp_text(timestamp)
        instant = _aware_utc(now, "now")
        sent_at = datetime.fromtimestamp(int(timestamp_text), tz=UTC)
        if abs((instant - sent_at).total_seconds()) > tolerance_seconds:
            return False
        expected = sign_payload(body, timestamp_text, secret)
    except (OverflowError, TypeError, ValueError):
        return False
    return hmac.compare_digest(signature, expected)


def _envelope(message: OutboundMessage, payload: Mapping[str, object]) -> tuple[bytes, str]:
    subject = None
    if message.subject_type is not None and message.subject_id is not None:
        subject = {"id": str(message.subject_id), "type": message.subject_type}
    identity = {
        "data": payload,
        "recipient_id": str(message.recipient_id),
        "subject": subject,
        "subject_text": message.subject,
        "type": message.template,
    }
    event_id = hashlib.sha256(_canonical_json(identity)).hexdigest()
    event = {
        "data": payload,
        "event_id": event_id,
        "occurred_at": (
            message.scheduled_for.astimezone(UTC).isoformat()
            if message.scheduled_for is not None
            else None
        ),
        "subject": subject,
        "type": message.template,
        "version": 1,
    }
    body = _canonical_json(event)
    if len(body) > _MAX_PAYLOAD_BYTES:
        raise ValueError("webhook envelope exceeds the 1 MiB limit")
    return body, event_id


def _response_result(
    response: httpx.Response,
    event_id: str,
    now: datetime,
) -> DeliveryResult:
    status = response.status_code
    if 200 <= status < 300:
        provider_id = response.headers.get("X-Request-ID") or response.headers.get("X-Request-Id")
        if provider_id is None:
            provider_id = event_id
        return DeliveryResult(DeliveryStatus.SENT, provider_message_id=_provider_id(provider_id))
    if status in {408, 425, 429} or 500 <= status <= 599:
        retry_after = _retry_after(response.headers.get("Retry-After"), now)
        return DeliveryResult(
            DeliveryStatus.RETRY,
            error=f"webhook endpoint returned HTTP {status}",
            retry_after_seconds=retry_after,
        )
    if 300 <= status < 400:
        detail = "webhook endpoint returned a redirect; redirects are not followed"
    else:
        detail = f"webhook endpoint returned HTTP {status}"
    return DeliveryResult(DeliveryStatus.DEAD_LETTERED, error=detail)


def _retry_after(value: str | None, now: datetime) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        seconds = int(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
            if retry_at.tzinfo is None:
                return None
            seconds = max(0, int((retry_at.astimezone(UTC) - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds < 0:
        return 0
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _safe_mapping(value: Mapping[str, object], *, path: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 100:
            raise ValueError(f"{path} contains an invalid field name")
        if any(ord(character) < 32 or ord(character) == 127 for character in key):
            raise ValueError(f"{path}.{key} contains a control character")
        if _PERSONAL_FIELD_PATTERN.search(key):
            raise ValueError(f"{path}.{key} is a personal-class field")
        result[key] = _safe_value(item, path=f"{path}.{key}")
    return result


def _safe_value(value: object, *, path: str) -> object:
    if value is None or isinstance(value, str | int | bool | float):
        if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
            raise ValueError(f"{path} contains a non-finite number")
        if isinstance(value, str) and len(value) > _MAX_PAYLOAD_BYTES:
            raise ValueError(f"{path} is too long")
        return value
    if isinstance(value, Mapping):
        return _safe_mapping(value, path=path)
    if isinstance(value, list | tuple):
        return [_safe_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} contains unsupported value {type(value).__name__}")


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("webhook payload is not valid JSON") from error


def _endpoint(value: WebhookEndpoint | str | object) -> WebhookEndpoint:
    if isinstance(value, WebhookEndpoint):
        return value
    if isinstance(value, str):
        return WebhookEndpoint(value)
    raise TypeError("webhook endpoint must be a WebhookEndpoint or URL string")


def _validate_url(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("webhook URL must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_ENDPOINT_URL_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError("webhook URL is invalid")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("webhook URL must be an HTTP(S) origin without credentials or query data")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65_535:
            raise ValueError("webhook URL port is invalid")
    except ValueError as error:
        raise ValueError("webhook URL port is invalid") from error
    if not parsed.hostname:
        raise ValueError("webhook URL hostname is required")
    return normalized


def _secret_bytes(value: object) -> bytes | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if callable(getter) else value
    if isinstance(raw, bytes):
        result = raw
    elif isinstance(raw, str):
        result = raw.encode("utf-8")
    else:
        raise TypeError("webhook signing secret must be text or bytes")
    if not result or len(result) > 4_096:
        raise ValueError("webhook signing secret must be non-empty and bounded")
    return result


def _body_bytes(value: bytes | str) -> bytes:
    if isinstance(value, bytes):
        result = value
    elif isinstance(value, str):
        result = value.encode("utf-8")
    else:
        raise TypeError("webhook body must be bytes or text")
    if len(result) > _MAX_PAYLOAD_BYTES:
        raise ValueError("webhook body exceeds the 1 MiB limit")
    return result


def _timestamp_text(value: int | str) -> str:
    if isinstance(value, bool):
        raise TypeError("webhook timestamp must be an integer")
    if isinstance(value, int):
        normalized = str(value)
    elif isinstance(value, str) and value.isdigit():
        normalized = value
    else:
        raise ValueError("webhook timestamp must be Unix seconds")
    timestamp = int(normalized)
    if timestamp < 0 or timestamp > 9_999_999_999:
        raise ValueError("webhook timestamp is outside the supported range")
    return normalized


def _aware_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _uuid(value: UUID | str, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid UUID") from error


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{field_name} must be bounded non-blank text")
    normalized = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains a control character")
    return normalized


def _provider_id(value: object) -> str:
    if not isinstance(value, str):
        return "webhook-accepted"
    normalized = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character for character in value
    ).strip()
    return normalized[:_MAX_PROVIDER_ID_LENGTH] or "webhook-accepted"


def _safe_error(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:_MAX_ERROR_LENGTH] or "webhook delivery failed"


WebhookAdapter = WebhookNotifier


__all__ = [
    "WebhookAdapter",
    "WebhookCapability",
    "WebhookEndpoint",
    "WebhookEndpointRegistry",
    "WebhookNotifier",
    "sign_payload",
    "validate_webhook_payload",
    "verify_signature",
]
