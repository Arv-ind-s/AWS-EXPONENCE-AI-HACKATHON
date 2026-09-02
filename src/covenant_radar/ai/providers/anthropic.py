"""Anthropic Messages API provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import httpx

from covenant_radar.ai.providers.base import BaseHttpProvider, append_path
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse


class AnthropicProvider(BaseHttpProvider):
    """Adapter for Anthropic's native Messages API."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        anthropic_version: str = "2023-06-01",
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
        ca_bundle: Path | str | None = None,
    ) -> None:
        if (
            not anthropic_version
            or len(anthropic_version) > 64
            or any(char.isspace() for char in anthropic_version)
        ):
            raise ValueError("Anthropic version must be a bounded non-whitespace value.")
        super().__init__(
            provider_name="anthropic",
            endpoint=endpoint,
            api_key=api_key,
            http_client=http_client,
            transport=transport,
            verify=verify,
            ca_bundle=ca_bundle,
        )
        self.anthropic_version = anthropic_version

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        system_messages = [
            message.content for message in request.messages if message.role == "system"
        ]
        body: dict[str, object] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
                if message.role != "system"
            ],
        }
        if system_messages:
            body["system"] = "\n\n".join(system_messages)
        if request.temperature is not None:
            body["temperature"] = request.temperature

        payload, latency_ms = self._post_json(
            request,
            url=append_path(self.endpoint, "v1/messages"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "anthropic-version": self.anthropic_version,
                "x-api-key": self._api_key,
            },
            body=body,
        )
        return _normalise_anthropic_payload(payload, latency_ms=latency_ms)


def _normalise_anthropic_payload(
    payload: object,
    *,
    latency_ms: int,
) -> CompletionResponse:
    notes: list[str] = []
    text: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    if not isinstance(payload, Mapping):
        notes.append("response payload is not an object")
    else:
        model_value = payload.get("model")
        if isinstance(model_value, str) and model_value:
            model = model_value
        else:
            notes.append("model is missing or not a non-empty string")

        content = payload.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, Mapping) or item.get("type") != "text":
                    notes.append("content contains a non-text part")
                    parts = []
                    break
                value = item.get("text")
                if not isinstance(value, str):
                    notes.append("content text is not a string")
                    parts = []
                    break
                parts.append(value)
            else:
                text = "".join(parts)
        else:
            notes.append("content is missing or not a list")

        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            input_tokens = _token_count(usage.get("input_tokens"), notes)
            output_tokens = _token_count(usage.get("output_tokens"), notes)
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
    )


def _token_count(value: object, notes: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        notes.append("usage token count is not a non-negative integer")
        return None
    return value


__all__ = ["AnthropicProvider"]
