"""Capture proof that a model request contains no personal or secret fields."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from covenant_radar.ai.client import CallContext, InMemoryModelCallWriter, ModelClient
from covenant_radar.ai.masking import build_outbound
from covenant_radar.core.clock import FixedClock
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse, LLMProvider

pytestmark = pytest.mark.security

_NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)


class _CaptureProvider:
    provider_name = "capture"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(
            text="masked response",
            model="capture-model",
            input_tokens=10,
            output_tokens=2,
            latency_ms=1,
            raw_payload={"captured": True},
        )


def _run_capture() -> list[str]:
    provider = _CaptureProvider()
    client = ModelClient(
        cast(LLMProvider, provider),
        model="capture-model",
        model_calls=InMemoryModelCallWriter(),
        clock=FixedClock(_NOW),
    )
    prompt = build_outbound(
        {
            "ratio_name": "DSCR",
            "value": Decimal("1.25"),
            "threshold": Decimal("1.20"),
            "headroom": Decimal("4.1667"),
            "evidence": {"type": "financial", "count": 3},
            "probability": Decimal("0.40"),
            "confidence": Decimal("0.90"),
            "crossing_date": "2026-08-31",
            "driver_names": ["May"],
            "clause_text": (
                "May signed the undertaking. PAN ABCDE1234F; "
                "contact may@example.com; credential hackathon-api-key."
            ),
        },
        secret="hackathon-api-key",
        prompt_version="v1",
    )
    client.call(1, prompt, "v1", CallContext(request_id="rq-capture"))
    return [message.content for request in provider.requests for message in request.messages]


def test_full_workload_capture_has_zero_personal_fields() -> None:
    captured = _run_capture()
    body = "\n".join(captured)

    assert "May" not in body
    assert "ABCDE1234F" not in body
    assert "may@example.com" not in body
    assert "ROLE_DRIVER_1" in body
    assert "OPAQUE_ID_1" in body


def test_full_workload_capture_has_zero_secret_material() -> None:
    captured = _run_capture()

    assert all("hackathon-api-key" not in body for body in captured)
    assert all("[REDACTED]" in body for body in captured)
