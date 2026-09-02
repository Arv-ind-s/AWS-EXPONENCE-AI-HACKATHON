"""Shared mechanics for the non-retrying language-model adapters."""

from __future__ import annotations

import json
import ssl
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from covenant_radar.ai.errors import (
    VALID_PROVIDER_NAMES,
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderRequestRejected,
    ProviderUnavailable,
)
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse

_MAX_ENDPOINT_LENGTH = 2048
_DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class BaseHttpProvider(ABC):
    """One strict HTTP implementation shared by all live providers.

    The client is injectable for offline tests, but a client created by the
    adapter always has certificate verification enabled.  Retry policy is
    deliberately absent; it belongs to the later single call site.
    """

    provider_name: str

    def __init__(
        self,
        *,
        provider_name: str,
        endpoint: str,
        api_key: str,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
        ca_bundle: Path | str | None = None,
    ) -> None:
        if verify is not True:
            raise ValueError("TLS certificate verification cannot be disabled.")
        if not isinstance(api_key, str) or not api_key:
            raise ProviderConfigurationError(
                "An API key is required for a live language-model provider.",
                provider=provider_name,
            )
        if http_client is not None and transport is not None:
            raise ValueError("Provide either http_client or transport, not both.")
        if http_client is not None and ca_bundle is not None:
            raise ValueError("A ca_bundle applies only to an adapter-created client.")

        self.provider_name = provider_name
        self.endpoint = validate_endpoint(endpoint)
        self._api_key = api_key
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.Client(
            verify=trust_context(ca_bundle, provider=provider_name),
            timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
            # Provider endpoints are explicit deployment configuration.  Do
            # not silently route their credentials through an inherited
            # HTTP(S)_PROXY value (developer shells and service managers
            # commonly leave stale local proxy settings behind).  Corporate
            # TLS interception remains supported through ``ca_bundle``.
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        """Close an adapter-owned HTTP client; injected clients remain caller-owned."""

        if self._owns_client:
            self._http_client.close()

    def __enter__(self) -> BaseHttpProvider:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _post_json(
        self,
        request: CompletionRequest,
        *,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
    ) -> tuple[object, int]:
        started = time.perf_counter()
        try:
            response = self._http_client.request(
                "POST",
                url,
                headers=dict(headers),
                json=dict(body),
                timeout=(
                    request.timeout_seconds
                    if request.timeout_seconds is not None
                    else _DEFAULT_HTTP_TIMEOUT_SECONDS
                ),
            )
        except httpx.TimeoutException as error:
            raise ProviderUnavailable(self.provider_name, reason="timeout") from error
        except httpx.RequestError as error:
            raise ProviderUnavailable(self.provider_name) from error

        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        if response.status_code in {401, 403}:
            raise ProviderAuthError(self.provider_name)
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise ProviderUnavailable(
                self.provider_name,
                reason=f"http status {response.status_code}",
            )
        if response.status_code >= 400:
            raise ProviderRequestRejected(
                self.provider_name,
                status_code=response.status_code,
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            return response.text, latency_ms
        try:
            return response.json(), latency_ms
        except (TypeError, ValueError, json.JSONDecodeError):
            return response.text, latency_ms

    @abstractmethod
    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Complete one request exactly once."""


def trust_context(ca_bundle: Path | str | None, *, provider: str) -> ssl.SSLContext | bool:
    """Return the TLS trust configuration for an adapter-created client.

    Without a bundle this is plain ``True``, so httpx applies its own default
    exactly as before.  With one, the default context is built first and the
    named PEM file is loaded *in addition* to it: an organisation's internal
    CA becomes trusted without the public roots being dropped, and hostname
    checking and certificate verification both stay on.  There is
    deliberately no path here that produces an unverified context.
    """

    if ca_bundle is None:
        return True
    path = Path(ca_bundle)
    context = httpx.create_ssl_context()
    try:
        context.load_verify_locations(cafile=str(path))
    except (OSError, ssl.SSLError) as error:
        raise ProviderConfigurationError(
            f"The configured CA bundle could not be loaded: {path}.",
            provider=provider,
        ) from error
    return context


def validate_endpoint(endpoint: str) -> str:
    """Validate and return an HTTPS endpoint without credentials or fragments."""

    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > _MAX_ENDPOINT_LENGTH:
        raise ProviderConfigurationError("Provider endpoint must be a bounded absolute HTTPS URL.")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ProviderConfigurationError("Provider endpoint is not a valid URL.") from error
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ProviderConfigurationError(
            "Provider endpoint must use HTTPS and contain no credentials, query or fragment."
        )
    return endpoint.rstrip("/")


def append_path(endpoint: str, suffix: str) -> str:
    """Append a provider path while preserving an explicitly configured path."""

    parsed = urlsplit(endpoint)
    current_path = parsed.path.rstrip("/")
    suffix = "/" + suffix.strip("/")
    if not current_path.endswith(suffix):
        current_path += suffix
    return urlunsplit((parsed.scheme, parsed.netloc, current_path, parsed.query, ""))


def openai_messages(request: CompletionRequest) -> list[dict[str, str]]:
    """Convert immutable port messages to the OpenAI-compatible wire shape."""

    return [{"role": message.role, "content": message.content} for message in request.messages]


def openai_body(request: CompletionRequest, *, include_model: bool = True) -> dict[str, object]:
    """Build the common OpenAI-compatible request body."""

    body: dict[str, object] = {
        "messages": openai_messages(request),
        "max_tokens": request.max_tokens,
    }
    if include_model:
        body["model"] = request.model
    if request.temperature is not None:
        body["temperature"] = request.temperature
    return body


def normalise_openai_payload(
    payload: object,
    *,
    latency_ms: int,
    from_cassette: bool = False,
) -> CompletionResponse:
    """Extract standard OpenAI-compatible fields without rejecting bad shapes."""

    notes: list[str] = []
    text: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    if not isinstance(payload, Mapping):
        notes.append("response payload is not an object")
    else:
        model = _optional_string(payload.get("model"), "model", notes)
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                message = first.get("message")
                if isinstance(message, Mapping):
                    text = _content_text(message.get("content"), notes)
                else:
                    notes.append("choices[0].message is missing or not an object")
            else:
                notes.append("choices[0] is missing or not an object")
        else:
            notes.append("choices is missing or empty")

        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            input_tokens = _optional_non_negative_int(usage.get("prompt_tokens"), notes)
            output_tokens = _optional_non_negative_int(usage.get("completion_tokens"), notes)
        elif usage is not None:
            notes.append("usage is not an object")

    return CompletionResponse(
        text=text,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        raw_payload=payload,
        normalization_note="; ".join(notes) if notes else None,
        from_cassette=from_cassette,
    )


def _content_text(value: object, notes: list[str]) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text_parts: list[str] = []
        for item in value:
            if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
                notes.append("message.content contains a non-text part")
                return None
            text_parts.append(item["text"])
        return "".join(text_parts)
    notes.append("message.content is missing or not text")
    return None


def _optional_string(value: object, name: str, notes: list[str]) -> str | None:
    if value is None:
        notes.append(f"{name} is missing")
        return None
    if not isinstance(value, str) or not value:
        notes.append(f"{name} is not a non-empty string")
        return None
    return value


def _optional_non_negative_int(value: object, notes: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        notes.append("usage token count is not a non-negative integer")
        return None
    return value


def require_provider_name(provider: str) -> None:
    if provider not in VALID_PROVIDER_NAMES:
        valid = ", ".join(VALID_PROVIDER_NAMES)
        raise ProviderConfigurationError(
            f"Unknown language-model provider '{provider}'. Valid providers: {valid}.",
            provider=provider,
        )


def secret_value(value: Any) -> str:
    """Read a Pydantic SecretStr or a plain test/configuration string safely."""

    getter = getattr(value, "get_secret_value", None)
    result = getter() if callable(getter) else value
    if not isinstance(result, str) or not result:
        raise ProviderConfigurationError("A live provider API key is required.")
    return result


__all__ = [
    "BaseHttpProvider",
    "append_path",
    "normalise_openai_payload",
    "openai_body",
    "openai_messages",
    "require_provider_name",
    "secret_value",
    "trust_context",
    "validate_endpoint",
]
