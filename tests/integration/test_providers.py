"""Offline integration tests for provider request mapping and failure safety."""

from __future__ import annotations

import httpx
import pytest

from covenant_radar.ai.errors import ProviderAuthError, ProviderUnavailable
from covenant_radar.ai.providers.tcs_genailab import TCSGenAILabProvider
from covenant_radar.ports.llm import CompletionRequest


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[{"role": "user", "content": "Return the covenant result."}],
        model="credit-model",
    )


def test_auth_failure_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "invalid key"}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TCSGenAILabProvider(
        endpoint="https://tcs.example",
        api_key="super-secret-key",
        http_client=client,
    )
    try:
        with pytest.raises(ProviderAuthError) as raised:
            provider.complete(_request())
    finally:
        client.close()

    assert calls == 1
    assert "super-secret-key" not in str(raised.value)
    assert raised.value.provider == "tcs"


def test_transport_failure_names_provider_not_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed for super-secret-key", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TCSGenAILabProvider(
        endpoint="https://tcs.example",
        api_key="super-secret-key",
        http_client=client,
    )
    try:
        with pytest.raises(ProviderUnavailable) as raised:
            provider.complete(_request())
    finally:
        client.close()

    assert "tcs" in str(raised.value)
    assert "super-secret-key" not in str(raised.value)
    assert raised.value.provider == "tcs"


def test_non_conforming_payload_passed_through_with_note() -> None:
    malformed = {"model": 42, "unexpected": {"answer": True}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=malformed, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TCSGenAILabProvider(
        endpoint="https://tcs.example", api_key="key", http_client=client
    )
    try:
        response = provider.complete(_request())
    finally:
        client.close()

    assert response.raw_payload == malformed
    assert response.text is None
    assert response.model is None
    assert response.normalization_note is not None
    assert "choices" in response.normalization_note
