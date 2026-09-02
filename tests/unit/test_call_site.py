"""Focused tests for the T-089 guarded model call site."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from covenant_radar.ai.budget import BudgetLimits, CeilingReached
from covenant_radar.ai.client import (
    MASKING_MARKER,
    CallContext,
    InMemoryModelCallWriter,
    MaskedPrompt,
    ModelClient,
)
from covenant_radar.ai.errors import ProviderUnavailable
from covenant_radar.core.clock import FixedClock
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse, LLMProvider

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)


class _Provider:
    provider_name = "recorded"
    model = "fixture-model"

    def __init__(self, responses: Sequence[object]) -> None:
        self.requests: list[CompletionRequest] = []
        self._responses = list(responses)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return cast(CompletionResponse, response)


def _response() -> CompletionResponse:
    return CompletionResponse(
        text="grounded draft",
        model="returned-model",
        input_tokens=8,
        output_tokens=4,
        latency_ms=2,
        raw_payload={"fixture": True},
    )


def _prompt(*, version: str = "v1") -> MaskedPrompt:
    return MaskedPrompt(
        messages=[
            {
                "role": "system",
                "content": f"prompt-version: {version}",
            },
            {"role": "user", "content": "masked ratio value"},
        ],
        version=version,
    )


def _client(
    provider: LLMProvider,
    writer: InMemoryModelCallWriter,
    *,
    budget: BudgetLimits | None = None,
    alerts: object | None = None,
) -> ModelClient:
    return ModelClient(
        provider,
        model="fixture-model",
        budget=budget or BudgetLimits(calls_per_hour=10, calls_per_day=20),
        model_calls=writer,
        alerts=alerts,
        clock=FixedClock(_NOW),
    )


def test_unmasked_prompt_raises_before_network() -> None:
    provider = _Provider([_response()])
    writer = InMemoryModelCallWriter()
    client = _client(provider, writer)

    with pytest.raises(RuntimeError, match="masked"):
        client.call(1, "unmasked clause", "v1", CallContext(request_id="rq-unmasked"))

    assert provider.requests == []
    assert len(writer.records) == 1
    assert writer.records[0].refusal_reason == "unmasked_prompt"


def test_stage_outside_permitted_raises() -> None:
    provider = _Provider([_response()])
    writer = InMemoryModelCallWriter()
    client = _client(provider, writer)

    with pytest.raises(ValueError, match="stage"):
        client.call(2, _prompt(), "v1", CallContext(request_id="rq-stage"))

    assert provider.requests == []
    assert writer.records[0].refusal_reason == "invalid_stage"


def test_version_mismatch_refused() -> None:
    provider = _Provider([_response()])
    writer = InMemoryModelCallWriter()
    client = _client(provider, writer)

    with pytest.raises(ValueError, match="does not match"):
        client.call(1, _prompt(version="v2"), "v1", CallContext(request_id="rq-version"))

    assert provider.requests == []
    assert writer.records[0].refusal_reason == "prompt_version_mismatch"


def test_timeout_retries_once_then_unavailable() -> None:
    provider = _Provider([TimeoutError(), TimeoutError()])
    writer = InMemoryModelCallWriter()
    client = _client(provider, writer)

    with pytest.raises(ProviderUnavailable):
        client.call(7, _prompt(), "v1", CallContext(request_id="rq-timeout"))

    assert len(provider.requests) == 2
    assert len(writer.records) == 2
    assert [record.retry_count for record in writer.records] == [0, 1]
    assert all(record.refusal_reason == "provider_unavailable:timeout" for record in writer.records)


def test_every_path_writes_one_record() -> None:
    provider = _Provider([_response(), _response()])
    writer = InMemoryModelCallWriter()
    alerts: list[dict[str, object]] = []
    client = _client(
        provider, writer, budget=BudgetLimits(calls_per_hour=1, calls_per_day=2), alerts=alerts
    )

    result = client.call(1, _prompt(), "v1", CallContext(request_id="rq-record"))
    assert result.text == "grounded draft"
    assert len(writer.records) == 1
    assert writer.records[0].check_verdict == "not_checked"

    with pytest.raises(CeilingReached):
        client.call(1, _prompt(), "v1", CallContext(request_id="rq-ceiling"))

    assert len(writer.records) == 2
    assert writer.records[1].check_verdict == "ceiling_reached"
    assert alerts[0]["event"] == "model_call_ceiling_reached"
    assert provider.requests and len(provider.requests) == 1


def test_masking_marker_is_stable() -> None:
    assert MASKING_MARKER == "covenant-radar/masked/v1"
    assert _prompt().marker == MASKING_MARKER
