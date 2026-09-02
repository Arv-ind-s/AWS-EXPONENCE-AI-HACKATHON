"""Integration tests for T-094: stage-1 orchestration through the real
guarded call site (`ai/client.py`), the real masking layer (`ai/masking.py`)
and the real shipped prompt template — not the parsing function in
isolation, so a wiring mistake between the call site, the prompt and the
parser fails here even if the unit tests still pass.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from covenant_radar.ai.client import InMemoryModelCallWriter, ModelClient
from covenant_radar.ai.errors import ProviderUnavailable
from covenant_radar.ai.intake import propose_candidates
from covenant_radar.domain.intake.candidates import CandidateLine, ClauseCandidate
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse, LLMProvider

pytestmark = pytest.mark.integration

_REPLY_PAYLOAD: dict[str, object] = {
    "definition": "dscr",
    "custom_formula": None,
    "threshold": "1.25x",
    "direction": "above",
    "unit": "ratio",
    "currency": None,
    "frequency": "quarterly",
    "effective_from": "2026-04-01",
    "effective_to": None,
    "exceptions": [],
    "cure_period_days": 30,
    "source_quote": "DSCR shall not fall below 1.25 times.",
}
_REPLY_TEXT = json.dumps(_REPLY_PAYLOAD)


def _candidate(index: int, text: str) -> ClauseCandidate:
    line = CandidateLine(page_number=1, start_offset=0, end_offset=len(text), text=text)
    return ClauseCandidate(
        start_page=1,
        start_offset=0,
        end_page=1,
        end_offset=len(text),
        text=text,
        matched_rules=(f"ratio:test-{index}",),
        lines=(line,),
    )


class _RecordingProvider:
    """Returns the same valid stage-1 reply for every call, and remembers
    every request it was sent so a test can assert on call count and on
    what was actually transmitted."""

    provider_name = "fixture"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(
            text=_REPLY_TEXT,
            model="fixture-model",
            input_tokens=12,
            output_tokens=8,
            latency_ms=1,
            raw_payload={"fixture": True},
        )


class _UnavailableProvider:
    """A provider that is always down, with no cassette to fall back to."""

    provider_name = "fixture"

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderUnavailable("fixture", reason="transport failure")


def _client(provider: LLMProvider) -> ModelClient:
    return ModelClient(provider, model="fixture-model", model_calls=InMemoryModelCallWriter())


def test_one_call_per_candidate() -> None:
    provider = _RecordingProvider()
    candidates = (
        _candidate(1, "DSCR shall not fall below 1.25 times."),
        _candidate(2, "Current ratio shall be maintained above 1.20."),
        _candidate(3, "Leverage ratio shall not exceed 3.00x."),
    )

    proposals = propose_candidates(candidates, _client(provider))

    assert len(provider.requests) == 3
    assert len(proposals) == 3
    assert tuple(proposal.candidate for proposal in proposals) == candidates
    # Each call carries only its own candidate's clause text.
    assert "DSCR" in provider.requests[0].messages[-1].content
    assert "Leverage" in provider.requests[2].messages[-1].content


def test_raw_reply_retained() -> None:
    provider = _RecordingProvider()
    candidate = _candidate(1, "DSCR shall not fall below 1.25 times.")

    (proposal,) = propose_candidates((candidate,), _client(provider))

    assert proposal.parseable is True
    assert proposal.raw_reply == _REPLY_TEXT
    assert proposal.threshold == Decimal("1.25")
    assert proposal.candidate is candidate


def test_provider_unavailable_propagates() -> None:
    provider = _UnavailableProvider()
    candidate = _candidate(1, "DSCR shall not fall below 1.25 times.")

    with pytest.raises(ProviderUnavailable):
        propose_candidates((candidate,), _client(provider))
