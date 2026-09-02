"""Unit coverage for T-091 cassette recording and replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from covenant_radar.ai.errors import ProviderUnavailable
from covenant_radar.ai.masking import build_outbound
from covenant_radar.ai.providers.recorded import RecordedProvider, RecordingProvider
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse

pytestmark = pytest.mark.unit


class _LiveProvider:
    provider_name = "fixture"

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(
            text="grounded response",
            model="fixture-model",
            input_tokens=7,
            output_tokens=3,
            latency_ms=4,
            raw_payload={"fixture": True},
        )


def _prompt(version: str = "v1"):
    return build_outbound(
        {
            "driver_names": ["May Rao"],
            "clause_text": "May Rao reported PAN ABCDE1234F in the clause.",
        },
        prompt_version=version,
    )


def _request(prompt, version: str = "v1") -> CompletionRequest:
    return CompletionRequest(
        messages=prompt.messages,
        model="fixture-model",
        prompt_version=version,
    )


def test_round_trip(tmp_path: Path) -> None:
    prompt = _prompt()
    with RecordingProvider(_LiveProvider(), tmp_path) as recorder:
        recorded = recorder.complete_masked(prompt, model="fixture-model")

    replayed = RecordedProvider(cassette_path=tmp_path).complete(_request(prompt))

    assert recorded.text == replayed.text == "grounded response"
    assert replayed.from_cassette
    assert replayed.raw_payload == {"fixture": True}


def test_miss_is_explicit_not_fabricated(tmp_path: Path) -> None:
    provider = RecordedProvider(cassette_path=tmp_path)

    with pytest.raises(ProviderUnavailable) as raised:
        provider.complete(_request(_prompt()))

    assert raised.value.reason == "cassette miss"
    assert "empty" not in str(raised.value).casefold()


def test_version_bump_is_a_miss(tmp_path: Path) -> None:
    prompt_v1 = _prompt("v1")
    with RecordingProvider(_LiveProvider(), tmp_path) as recorder:
        recorder.complete_masked(prompt_v1, model="fixture-model")

    with pytest.raises(ProviderUnavailable) as raised:
        RecordedProvider(cassette_path=tmp_path).complete(_request(_prompt("v2"), "v2"))

    assert raised.value.reason == "cassette miss"


def test_corrupt_file_skipped(tmp_path: Path, recwarn: pytest.WarningsRecorder) -> None:
    prompt = _prompt()
    with RecordingProvider(_LiveProvider(), tmp_path) as recorder:
        recorder.complete_masked(prompt, model="fixture-model")
    (tmp_path / "corrupt.json").write_text("{not-json", encoding="utf-8")

    replayed = RecordedProvider(cassette_path=tmp_path).complete(_request(prompt))

    assert replayed.text == "grounded response"
    assert any("Skipping cassette corrupt.json" in str(item.message) for item in recwarn)


def test_cassette_contains_only_masked_content(tmp_path: Path) -> None:
    prompt = _prompt()
    with RecordingProvider(_LiveProvider(), tmp_path) as recorder:
        recorder.complete_masked(prompt, model="fixture-model")

    contents = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.json"))

    assert "May Rao" not in contents
    assert "ABCDE1234F" not in contents
    assert "ROLE_DRIVER_1" in contents
    assert "token_map" not in contents
