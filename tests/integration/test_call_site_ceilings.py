"""Offline integration tests for T-089 rate and budget ceilings."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from covenant_radar.ai.budget import BudgetLimits, CeilingReached
from covenant_radar.ai.client import CallContext, InMemoryModelCallWriter, MaskedPrompt, ModelClient
from covenant_radar.core.clock import FixedClock
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)


class _Provider:
    provider_name = "recorded"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        return CompletionResponse(
            text="fixture",
            model="fixture-model",
            input_tokens=1,
            output_tokens=1,
            latency_ms=0,
            raw_payload={},
        )


def _prompt() -> MaskedPrompt:
    return MaskedPrompt(content="prompt-version: v1\nmasked content", version="v1")


def test_hourly_ceiling_blocks_and_alerts() -> None:
    provider = _Provider()
    writer = InMemoryModelCallWriter()
    alerts: list[dict[str, object]] = []
    client = ModelClient(
        provider,
        model="fixture-model",
        budget=BudgetLimits(calls_per_hour=1, calls_per_day=2),
        model_calls=writer,
        alerts=alerts,
        clock=FixedClock(_NOW),
    )

    client.call(7, _prompt(), "v1", CallContext(request_id="rq-hour-1"))
    with pytest.raises(CeilingReached) as raised:
        client.call(7, _prompt(), "v1", CallContext(request_id="rq-hour-2"))

    assert raised.value.dimension == "hourly"
    assert raised.value.retry_at == datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
    assert provider.calls == 1
    assert alerts[0]["dimension"] == "hourly"
    assert writer.records[-1].refusal_reason == "ceiling_reached:hourly"


def test_budget_ceiling_blocks_and_alerts() -> None:
    provider = _Provider()
    writer = InMemoryModelCallWriter()
    alerts: list[dict[str, object]] = []
    client = ModelClient(
        provider,
        model="fixture-model",
        budget=BudgetLimits(calls_per_hour=10, calls_per_day=20, monthly_budget="1"),
        model_calls=writer,
        alerts=alerts,
        clock=FixedClock(_NOW),
    )
    context = CallContext(request_id="rq-budget-1", estimated_cost=1)

    client.call(1, _prompt(), "v1", context)
    with pytest.raises(CeilingReached) as raised:
        client.call(1, _prompt(), "v1", CallContext(request_id="rq-budget-2", estimated_cost=1))

    assert raised.value.dimension == "budget"
    assert provider.calls == 1
    assert alerts[0]["dimension"] == "budget"
    assert writer.records[-1].refusal_reason == "ceiling_reached:budget"
