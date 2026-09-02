"""AI adapter layer and provider selection boundary."""

from __future__ import annotations

from typing import Any

import httpx

from covenant_radar.ai.errors import (
    VALID_PROVIDER_NAMES,
    ProviderConfigurationError,
)
from covenant_radar.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMProvider,
    PromptMessage,
)


def create_provider(
    settings: Any,
    *,
    http_client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
) -> LLMProvider:
    """Construct the configured adapter without probing the external service.

    ``settings`` is intentionally duck-typed so this seam does not import the
    configuration package at module import time or couple the port to Pydantic.
    The application startup loader remains responsible for validating the
    complete settings object.
    """

    provider = getattr(settings, "provider", None)
    if provider not in (*VALID_PROVIDER_NAMES, "none"):
        valid = ", ".join((*VALID_PROVIDER_NAMES, "none"))
        raise ProviderConfigurationError(
            f"Unknown language-model provider '{provider}'. Valid providers: {valid}.",
            provider=str(provider),
        )
    if provider == "none":
        raise ProviderConfigurationError(
            "The language-model provider is disabled in configuration.",
            provider="none",
        )
    if provider == "recorded":
        from covenant_radar.ai.providers.recorded import RecordedProvider

        path = getattr(settings, "recorded_responses_path", None)
        return RecordedProvider(responses_path=path)

    endpoint = getattr(settings, "endpoint", None)
    model = getattr(settings, "model", None)
    api_key = getattr(settings, "api_key", None)
    if not isinstance(endpoint, str) or not endpoint:
        raise ProviderConfigurationError(
            f"Provider '{provider}' requires an endpoint.",
            provider=provider,
        )
    if not isinstance(model, str) or not model:
        raise ProviderConfigurationError(
            f"Provider '{provider}' requires a model.",
            provider=provider,
        )
    key = _secret_value(api_key, provider)
    # An injected client carries its own TLS configuration, so the bundle is
    # only forwarded when this seam is the one creating the client.
    ca_bundle = getattr(settings, "ca_bundle", None) if http_client is None else None
    if provider == "tcs":
        from covenant_radar.ai.providers.tcs_genailab import TCSGenAILabProvider

        return TCSGenAILabProvider(
            endpoint=endpoint,
            api_key=key,
            http_client=http_client,
            transport=transport,
            ca_bundle=ca_bundle,
        )
    if provider == "azure_openai":
        from covenant_radar.ai.providers.azure_openai import AzureOpenAIProvider

        return AzureOpenAIProvider(
            endpoint=endpoint,
            api_key=key,
            http_client=http_client,
            transport=transport,
            ca_bundle=ca_bundle,
        )
    from covenant_radar.ai.providers.anthropic import AnthropicProvider

    return AnthropicProvider(
        endpoint=endpoint,
        api_key=key,
        http_client=http_client,
        transport=transport,
        ca_bundle=ca_bundle,
    )


def provider_from_settings(
    settings: Any,
    *,
    http_client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
) -> LLMProvider:
    """Explicit alias for callers that prefer configuration-oriented naming."""

    return create_provider(settings, http_client=http_client, transport=transport)


def _secret_value(value: object, provider: str) -> str:
    getter = getattr(value, "get_secret_value", None)
    secret = getter() if callable(getter) else value
    if not isinstance(secret, str) or not secret:
        raise ProviderConfigurationError(
            f"Provider '{provider}' requires an API key.",
            provider=provider,
        )
    return secret


def __getattr__(name: str) -> Any:
    """Resolve adapter names on demand without widening the import graph."""

    if name in {"TCSGenAIProvider", "TCSGenAILabProvider", "TCSGenAiLabProvider"}:
        from covenant_radar.ai.providers.tcs_genailab import TCSGenAILabProvider

        return TCSGenAILabProvider
    if name in {"AzureOpenAIProvider", "AzureOpenAiProvider"}:
        from covenant_radar.ai.providers.azure_openai import AzureOpenAIProvider

        return AzureOpenAIProvider
    if name == "AnthropicProvider":
        from covenant_radar.ai.providers.anthropic import AnthropicProvider

        return AnthropicProvider
    if name == "RecordedProvider":
        from covenant_radar.ai.providers.recorded import RecordedProvider

        return RecordedProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AnthropicProvider",
    "AzureOpenAIProvider",
    "AzureOpenAiProvider",
    "CompletionRequest",
    "CompletionResponse",
    "LLMProvider",
    "PromptMessage",
    "ProviderConfigurationError",
    "RecordedProvider",
    "TCSGenAIProvider",
    "TCSGenAILabProvider",
    "TCSGenAiLabProvider",
    "VALID_PROVIDER_NAMES",
    "create_provider",
    "provider_from_settings",
]
