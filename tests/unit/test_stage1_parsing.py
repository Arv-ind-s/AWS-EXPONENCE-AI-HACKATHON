"""Unit tests for T-094 stage-1 reply parsing and normalisation."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from covenant_radar.domain.intake.candidates import CandidateLine, ClauseCandidate
from covenant_radar.domain.intake.proposal import StageOneProposal, parse_stage1_reply
from covenant_radar.domain.ratios.library import LIBRARY

pytestmark = pytest.mark.unit

_CLAUSE_TEXT = "DSCR shall not fall below 1.25 times, tested quarterly."

_BASE_REPLY: dict[str, object] = {
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
    "source_quote": _CLAUSE_TEXT,
}


def _reply(**overrides: object) -> str:
    payload = {**_BASE_REPLY, **overrides}
    return json.dumps(payload)


def _candidate(text: str = _CLAUSE_TEXT) -> ClauseCandidate:
    line = CandidateLine(page_number=1, start_offset=0, end_offset=len(text), text=text)
    return ClauseCandidate(
        start_page=1,
        start_offset=0,
        end_page=1,
        end_offset=len(text),
        text=text,
        matched_rules=("ratio:dscr", "threshold:0"),
        lines=(line,),
    )


def _assert_nothing_trusted(proposal: StageOneProposal) -> None:
    assert proposal.definition_ref is None
    assert proposal.custom_formula is None
    assert proposal.threshold is None
    assert proposal.threshold_ambiguous is False
    assert proposal.unit is None
    assert proposal.currency is None
    assert proposal.direction is None
    assert proposal.frequency is None
    assert proposal.frequency_ambiguous is False
    assert proposal.effective_from is None
    assert proposal.effective_to is None
    assert proposal.exceptions == ()
    assert proposal.cure_period_days is None
    assert proposal.source_quote is None


def test_unparseable_reply_marked_not_partially_trusted() -> None:
    candidate = _candidate()

    not_json = parse_stage1_reply(candidate, "this is not json")
    assert not_json.parseable is False
    assert not_json.raw_reply == "this is not json"
    assert not_json.parse_error is not None and "JSON" in not_json.parse_error
    _assert_nothing_trusted(not_json)

    missing_key_payload = {
        key: value for key, value in _BASE_REPLY.items() if key != "source_quote"
    }
    missing_key = parse_stage1_reply(candidate, json.dumps(missing_key_payload))
    assert missing_key.parseable is False
    assert missing_key.parse_error is not None and "source_quote" in missing_key.parse_error
    _assert_nothing_trusted(missing_key)

    extra_key = parse_stage1_reply(candidate, _reply(unexpected_field="surprise"))
    assert extra_key.parseable is False
    assert extra_key.parse_error is not None and "unexpected_field" in extra_key.parse_error
    _assert_nothing_trusted(extra_key)

    bad_enum = parse_stage1_reply(candidate, _reply(direction="sideways"))
    assert bad_enum.parseable is False
    assert bad_enum.parse_error is not None and "direction" in bad_enum.parse_error
    _assert_nothing_trusted(bad_enum)

    not_an_object = parse_stage1_reply(candidate, json.dumps(["not", "an", "object"]))
    assert not_an_object.parseable is False
    _assert_nothing_trusted(not_an_object)


@pytest.mark.parametrize(
    ("raw_threshold", "expected"),
    [
        ("1.25x", Decimal("1.25")),
        ("1.25 times", Decimal("1.25")),
        ("45%", Decimal("45")),
        ("45 percent", Decimal("45")),
        ("50 crore", Decimal("50")),
        ("₹50 crore", Decimal("50")),
        ("50 lakh", Decimal("0.5")),
        ("₹1,50,00,000", Decimal("15000000")),
        ("180 days", Decimal("180")),
        ("3", Decimal("3")),
    ],
)
def test_threshold_normalisation_unambiguous(raw_threshold: str, expected: Decimal) -> None:
    proposal = parse_stage1_reply(_candidate(), _reply(threshold=raw_threshold))

    assert proposal.parseable is True
    assert proposal.threshold_ambiguous is False
    assert proposal.threshold == expected


def test_threshold_normalisation_unambiguous_numeric_json_value() -> None:
    proposal = parse_stage1_reply(_candidate(), _reply(threshold=1.25))

    assert proposal.parseable is True
    assert proposal.threshold_ambiguous is False
    assert proposal.threshold == Decimal("1.25")


def test_ambiguous_value_flagged_not_guessed() -> None:
    threshold_case = parse_stage1_reply(
        _candidate(), _reply(threshold="approximately 1.25 times, subject to review")
    )
    assert threshold_case.parseable is True
    assert threshold_case.threshold is None
    assert threshold_case.threshold_ambiguous is True

    conflicting_quote = (
        "DSCR shall not fall below 1.25 times, tested quarterly, save that it is "
        "reviewed annually by the sanctioning authority."
    )
    frequency_case = parse_stage1_reply(
        _candidate(conflicting_quote),
        _reply(source_quote=conflicting_quote),
    )
    assert frequency_case.parseable is True
    assert frequency_case.frequency is None
    assert frequency_case.frequency_ambiguous is True


def test_out_of_library_definition_carried_for_verification() -> None:
    assert "not_a_real_ratio_code" not in LIBRARY

    proposal = parse_stage1_reply(_candidate(), _reply(definition="not_a_real_ratio_code"))

    assert proposal.parseable is True
    assert proposal.definition_ref == "not_a_real_ratio_code"
