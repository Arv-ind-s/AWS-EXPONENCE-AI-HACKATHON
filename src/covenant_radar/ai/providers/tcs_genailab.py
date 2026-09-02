"""TCS GenAI Lab OpenAI-compatible provider adapter."""

from __future__ import annotations

from pathlib import Path

import httpx

from covenant_radar.ai.providers.base import (
    BaseHttpProvider,
    append_path,
    normalise_openai_payload,
    openai_body,
)
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse


class TCSGenAILabProvider(BaseHttpProvider):
    """Adapter for the default TCS GenAI Lab OpenAI-compatible gateway."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
        ca_bundle: Path | str | None = None,
    ) -> None:
        super().__init__(
            provider_name="tcs",
            endpoint=endpoint,
            api_key=api_key,
            http_client=http_client,
            transport=transport,
            verify=verify,
            ca_bundle=ca_bundle,
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload, latency_ms = self._post_json(
            request,
            url=append_path(self.endpoint, "v1/chat/completions"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            body=openai_body(request),
        )
        return normalise_openai_payload(payload, latency_ms=latency_ms)


TcsGenAiLabProvider = TCSGenAILabProvider
TCSGenAiLabProvider = TCSGenAILabProvider
TCSGenAIProvider = TCSGenAILabProvider


__all__ = [
    "TCSGenAIProvider",
    "TCSGenAILabProvider",
    "TCSGenAiLabProvider",
]
