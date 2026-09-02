"""Unit tests for the provider-neutral protocol and adapter guarantees."""

from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from covenant_radar.ai import create_provider
from covenant_radar.ai.errors import ProviderConfigurationError, ProviderUnavailable
from covenant_radar.ai.providers.anthropic import AnthropicProvider
from covenant_radar.ai.providers.azure_openai import AzureOpenAIProvider
from covenant_radar.ai.providers.base import trust_context
from covenant_radar.ai.providers.recorded import RecordedProvider
from covenant_radar.ai.providers.tcs_genailab import TCSGenAILabProvider
from covenant_radar.config.settings import SettingsError, load_settings
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse, LLMProvider


def _self_signed_ca(common_name: str) -> bytes:
    """Build a throwaway PEM CA that certifi cannot already contain."""

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[
            {"role": "system", "content": "Answer briefly."},
            {"role": "user", "content": "What is DSCR?"},
        ],
        model="credit-model",
    )


def test_one_response_shape_across_adapters() -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "model": "claude-returned",
                    "content": [{"type": "text", "text": "anthropic answer"}],
                    "usage": {"input_tokens": 4, "output_tokens": 3},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "model": "gateway-returned",
                "choices": [{"message": {"role": "assistant", "content": "answer"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    providers: list[LLMProvider] = [
        TCSGenAILabProvider(endpoint="https://tcs.example", api_key="key", http_client=client),
        AzureOpenAIProvider(endpoint="https://azure.example", api_key="key", http_client=client),
        AnthropicProvider(endpoint="https://anthropic.example", api_key="key", http_client=client),
        RecordedProvider(
            responses={
                "replay": {
                    "text": "recorded answer",
                    "model": "recorded-model",
                    "input_tokens": 4,
                    "output_tokens": 3,
                    "latency_ms": 1,
                }
            }
        ),
    ]
    recorded_request = CompletionRequest(
        messages=[{"role": "user", "content": "replay"}],
        model="credit-model",
        cassette_key="replay",
    )

    try:
        responses = [provider.complete(_request()) for provider in providers[:3]]
        responses.append(providers[3].complete(recorded_request))
    finally:
        client.close()

    assert all(isinstance(response, CompletionResponse) for response in responses)
    assert [response.text for response in responses] == [
        "answer",
        "answer",
        "anthropic answer",
        "recorded answer",
    ]
    assert all(response.model for response in responses)
    assert all(response.input_tokens == 4 for response in responses)
    assert all(response.output_tokens == 3 for response in responses)
    assert [request.url.path for request in seen_requests] == [
        "/v1/chat/completions",
        "/openai/deployments/credit-model/chat/completions",
        "/v1/messages",
    ]
    assert seen_requests[0].headers["authorization"] == "Bearer key"
    assert seen_requests[1].headers["api-key"] == "key"
    assert seen_requests[2].headers["x-api-key"] == "key"
    assert seen_requests[2].headers["anthropic-version"] == "2023-06-01"


def test_adapter_does_not_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("fixture transport failure", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TCSGenAILabProvider(
        endpoint="https://tcs.example", api_key="key", http_client=client
    )
    try:
        with pytest.raises(ProviderUnavailable):
            provider.complete(_request())
    finally:
        client.close()

    assert calls == 1


def test_unknown_provider_refused_at_startup(tmp_path) -> None:
    config_file = tmp_path / "settings.toml"
    config_file.write_text('[ai]\nprovider = "unknown"\n', encoding="utf-8")

    with pytest.raises(SettingsError) as raised:
        load_settings(config_file, environ={})

    message = str(raised.value)
    assert "unknown" in message
    assert "recorded" in message
    assert "azure_openai" in message

    with pytest.raises(ProviderConfigurationError, match="Valid providers"):
        create_provider(type("UnknownSettings", (), {"provider": "unknown"})())


@pytest.mark.parametrize(
    "provider_class",
    [TCSGenAILabProvider, AzureOpenAIProvider, AnthropicProvider],
)
def test_tls_verification_cannot_be_disabled(provider_class) -> None:
    with pytest.raises(ValueError, match="TLS certificate verification"):
        provider_class(endpoint="https://provider.example", api_key="key", verify=False)


def test_ca_bundle_adds_anchors_without_weakening_verification(tmp_path) -> None:
    """A corporate CA is trusted *as well as* the public roots, never instead."""

    bundle = tmp_path / "corporate-ca.pem"
    bundle.write_bytes(_self_signed_ca("Covenant Radar Test CA"))
    default_anchors = len(httpx.create_ssl_context().get_ca_certs())

    context = trust_context(bundle, provider="tcs")

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    anchors = context.get_ca_certs()
    # httpx's own default is loaded first, so the bundle can only widen trust.
    assert len(anchors) == default_anchors + 1
    assert any(
        ("commonName", "Covenant Radar Test CA") in attribute
        for anchor in anchors
        for attribute in anchor["subject"]
    )


def test_no_ca_bundle_leaves_the_httpx_default_untouched() -> None:
    assert trust_context(None, provider="tcs") is True


def test_unreadable_ca_bundle_is_refused_as_configuration(tmp_path) -> None:
    bundle = tmp_path / "not-a-certificate.pem"
    bundle.write_text("this is not PEM\n", encoding="utf-8")

    with pytest.raises(ProviderConfigurationError, match="CA bundle"):
        TCSGenAILabProvider(endpoint="https://tcs.example", api_key="key", ca_bundle=bundle)


def test_ca_bundle_refused_alongside_an_injected_client(tmp_path) -> None:
    bundle = tmp_path / "corporate-ca.pem"
    bundle.write_bytes(_self_signed_ca("Covenant Radar Test CA"))
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        with pytest.raises(ValueError, match="adapter-created client"):
            TCSGenAILabProvider(
                endpoint="https://tcs.example",
                api_key="key",
                http_client=client,
                ca_bundle=bundle,
            )
    finally:
        client.close()
