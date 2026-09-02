"""Azure OpenAI chat-completions provider adapter."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from covenant_radar.ai.providers.base import (
    BaseHttpProvider,
    normalise_openai_payload,
    openai_body,
)
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse

_DEFAULT_API_VERSION = "2024-10-21"


class AzureOpenAIProvider(BaseHttpProvider):
    """Adapter for Azure's deployment-scoped OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        api_version: str = _DEFAULT_API_VERSION,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
        ca_bundle: Path | str | None = None,
    ) -> None:
        if not api_version or len(api_version) > 64 or any(char.isspace() for char in api_version):
            raise ValueError("Azure OpenAI api_version must be a bounded non-whitespace value.")
        super().__init__(
            provider_name="azure_openai",
            endpoint=endpoint,
            api_key=api_key,
            http_client=http_client,
            transport=transport,
            verify=verify,
            ca_bundle=ca_bundle,
        )
        self.api_version = api_version

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        url = self._completion_url(request.model)
        payload, latency_ms = self._post_json(
            request,
            url=url,
            headers={
                "Accept": "application/json",
                "api-key": self._api_key,
                "Content-Type": "application/json",
            },
            body=openai_body(request, include_model=False),
        )
        return normalise_openai_payload(payload, latency_ms=latency_ms)

    def _completion_url(self, deployment: str) -> str:
        parsed = urlsplit(self.endpoint)
        path = parsed.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path += f"/openai/deployments/{quote(deployment, safe='')}/chat/completions"
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("api-version", self.api_version)
        return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


AzureOpenAiProvider = AzureOpenAIProvider


__all__ = ["AzureOpenAIProvider", "AzureOpenAiProvider"]
