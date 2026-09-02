"""Integration coverage for T-100 stage-7 prompt and guarded drafting."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from covenant_radar.ai.client import InMemoryModelCallWriter, ModelClient
from covenant_radar.ai.memo import MemoShapeRefusal, build_memo_prompt, draft_memo
from covenant_radar.ai.shapes import CatalogueAction
from covenant_radar.domain.memo import MemoRecord, MemoRecords, RecordReference
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse
from covenant_radar.services.memo import MemoAssemblyService

pytestmark = pytest.mark.integration


class _RecordingProvider:
    provider_name = "fixture"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(
            text=self.reply,
            model="fixture-model",
            input_tokens=20,
            output_tokens=40,
            latency_ms=1,
            raw_payload={"fixture": True},
        )


class _SequenceProvider(_RecordingProvider):
    def __init__(self, replies: list[str]) -> None:
        super().__init__(replies[0])
        self.replies = replies

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        reply = self.replies.pop(0)
        return CompletionResponse(
            text=reply,
            model="fixture-model",
            input_tokens=20,
            output_tokens=40,
            latency_ms=1,
            raw_payload={"fixture": True},
        )


def _slots():
    records = MemoRecords(
        situation=MemoRecord(
            RecordReference("triage", "triage-1"),
            {"situation": "Projected pressure requires review."},
        ),
        covenant_position=MemoRecord(
            RecordReference("forecast", "forecast-1"),
            {
                "ratio_name": "Debt service coverage",
                "value": Decimal("1.25"),
                "threshold": Decimal("1.10"),
                "headroom": Decimal("0.15"),
                "probability": Decimal("0.42"),
                "confidence": Decimal("0.88"),
                "crossing_date": date(2026, 10, 15),
            },
        ),
        drivers=(
            MemoRecord(RecordReference("driver", "driver-1"), {"name": "Cash-flow pressure"}),
        ),
        evidence=(
            MemoRecord(
                RecordReference("evidence", "evidence-1"),
                {"citation": "EV-001", "count": 3},
            ),
        ),
        recommendations=(
            MemoRecord(
                RecordReference("intervention", "intervention-1"),
                {
                    "code": "CREDIT-REDUCE",
                    "role_tag": "credit",
                    "text": "Review and reduce funded exposure.",
                },
            ),
        ),
    )
    return MemoAssemblyService().assemble(records)


def _catalogue():
    return (
        CatalogueAction(
            id="CREDIT-REDUCE",
            role_tag="credit",
            text="Review and reduce funded exposure.",
        ),
    )


def test_masked_prompt_carries_only_whitelisted_fields() -> None:
    prompt = build_memo_prompt(_slots(), _catalogue())

    assert prompt.marker == "covenant-radar/masked/v1"
    assert prompt.version == "v2"
    assert "CREDIT-REDUCE" in prompt.content
    assert "Cash-flow pressure" not in prompt.content
    assert "ROLE_DRIVER_1" in prompt.content
    assert set(prompt.fields).issubset(
        {
            "situation",
            "evidence_counts_text",
            "simulation_options_text",
            "recommended_interventions_text",
            "intervention_text",
            "action_ids",
            "action_roles",
            "drivers",
            "ratio_name",
            "value",
            "threshold",
            "headroom",
            "probability",
            "confidence",
            "crossing_date",
        }
    )


def test_guarded_clean_draft_passes_all_four() -> None:
    provider = _RecordingProvider(
        json.dumps(
            {
                "headline": (
                    "Debt service coverage is projected to reach the action point on 2026-10-15."
                ),
                "summary": (
                    "The recorded value is 1.25 against a threshold of 1.10, with "
                    "headroom of 0.15. "
                    "The projected breach probability is 0.42 at confidence 0.88."
                ),
                "drivers": ["ROLE_DRIVER_1"],
                "actions": [{"id": "CREDIT-REDUCE", "role_tag": "credit"}],
                "recommended_next_step": "Review and reduce funded exposure.",
                "disclaimer": "human credit review is required before action",
            }
        )
    )
    result = draft_memo(
        _slots(),
        _catalogue(),
        ModelClient(provider, model="fixture-model", model_calls=InMemoryModelCallWriter()),
    )

    assert result.shape_report.all_passed is True
    assert result.draft.drafted_by_model is True
    assert result.draft.drivers == ("Cash-flow pressure",)
    assert result.attempts == 1
    assert provider.requests[0].prompt_version == "v2"
    assert provider.requests[0].max_tokens == 1_200


def _reply(*, summary: str) -> str:
    return json.dumps(
        {
            "headline": (
                "Debt service coverage is projected to reach the action point on 2026-10-15."
            ),
            "summary": summary,
            "drivers": ["ROLE_DRIVER_1"],
            "actions": [{"id": "CREDIT-REDUCE", "role_tag": "credit"}],
            "recommended_next_step": "Review and reduce funded exposure.",
            "disclaimer": "human credit review is required before action",
        }
    )


def test_failed_shape_is_regenerated_once() -> None:
    provider = _SequenceProvider(
        [_reply(summary="The fabricated value is 9.99."), _reply(summary="The value is 1.25.")]
    )
    result = draft_memo(
        _slots(),
        _catalogue(),
        ModelClient(provider, model="fixture-model", model_calls=InMemoryModelCallWriter()),
    )

    assert result.attempts == 2
    assert len(provider.requests) == 2
    assert "9.99" in provider.requests[1].messages[-1].content


def test_second_shape_failure_is_refused() -> None:
    provider = _SequenceProvider([_reply(summary="The fabricated value is 9.99.")] * 2)

    with pytest.raises(MemoShapeRefusal) as error:
        draft_memo(
            _slots(),
            _catalogue(),
            ModelClient(provider, model="fixture-model", model_calls=InMemoryModelCallWriter()),
        )

    assert error.value.attempts == 2
    assert error.value.report.all_passed is False
    assert len(provider.requests) == 2
