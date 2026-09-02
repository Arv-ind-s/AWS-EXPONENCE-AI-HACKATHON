"""Unit tests for T-095: the six code verifications, failing closed."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from covenant_radar.domain.intake.candidates import CandidateLine, ClauseCandidate
from covenant_radar.domain.intake.proposal import StageOneProposal, parse_stage1_reply
from covenant_radar.domain.intake.verify import (
    VerificationCheckName,
    VerificationContext,
    verify_proposal,
)
from covenant_radar.domain.ratios.definitions import FacilityFacts

pytestmark = pytest.mark.unit

_CLAUSE_TEXT = "DSCR shall not fall below 1.5 times, tested quarterly."

_BASE_REPLY: dict[str, object] = {
    "definition": "dscr",
    "custom_formula": None,
    "threshold": "1.5x",
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

_SANCTION_DATE = date(2026, 1, 1)


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


def _proposal(candidate_text: str = _CLAUSE_TEXT, **overrides: object) -> StageOneProposal:
    payload = {**_BASE_REPLY, **overrides}
    return parse_stage1_reply(_candidate(candidate_text), json.dumps(payload))


def _raw_proposal(raw_reply: str, candidate_text: str = _CLAUSE_TEXT) -> StageOneProposal:
    return parse_stage1_reply(_candidate(candidate_text), raw_reply)


def _context(
    *,
    statement_lines: dict[str, Decimal] | None = None,
    period_complete: bool = True,
    facility_facts: FacilityFacts | None = None,
    facility_sanction_date: date = _SANCTION_DATE,
    facility_currency: str = "INR",
) -> VerificationContext:
    lines = (
        statement_lines
        if statement_lines is not None
        else {"cash_flow_debt_service": Decimal("150"), "finance_cost": Decimal("100")}
    )
    return VerificationContext(
        statement_lines=lines,
        period_complete=period_complete,
        facility_facts=facility_facts if facility_facts is not None else FacilityFacts(),
        facility_sanction_date=facility_sanction_date,
        facility_currency=facility_currency,
    )


def test_all_six_run_and_collect() -> None:
    passing = verify_proposal(_proposal(), _context())
    assert [outcome.check for outcome in passing.checks] == list(VerificationCheckName)
    assert passing.all_passed is True
    assert passing.failed_checks == ()

    failing = verify_proposal(_raw_proposal("this is not json"), _context())
    assert [outcome.check for outcome in failing.checks] == list(VerificationCheckName)
    assert failing.all_passed is False
    # Every one of the six still ran and was collected, not just the first
    # failure: an unparseable reply also fails definition/recomputability/
    # threshold/unit checks, since none of those fields were ever populated.
    assert set(failing.failed_checks) == {
        VerificationCheckName.SCHEMA_VALID.value,
        VerificationCheckName.DEFINITION_KNOWN.value,
        VerificationCheckName.RECOMPUTABLE.value,
        VerificationCheckName.THRESHOLD_PLAUSIBLE.value,
        VerificationCheckName.UNIT_CURRENCY_CONSISTENT.value,
        VerificationCheckName.FREQUENCY_DATES_CONSISTENT.value,
    }


def test_implausible_threshold_named_with_band() -> None:
    report = verify_proposal(_proposal(threshold="25x"), _context())

    assert report.all_passed is False
    assert VerificationCheckName.THRESHOLD_PLAUSIBLE.value in report.failed_checks
    detail = report.detail_for(VerificationCheckName.THRESHOLD_PLAUSIBLE)
    assert "25" in detail
    assert "20" in detail  # dscr's plausible_max


def test_missing_line_fails_recomputability_naming_it() -> None:
    context = _context(statement_lines={"cash_flow_debt_service": Decimal("150")})
    report = verify_proposal(_proposal(), context)

    assert report.all_passed is False
    assert VerificationCheckName.RECOMPUTABLE.value in report.failed_checks
    detail = report.detail_for(VerificationCheckName.RECOMPUTABLE)
    assert "finance_cost" in detail
    # A plausible-looking proposal still fails here even though its named
    # definition is real and its threshold is well within band.
    assert report.detail_for(VerificationCheckName.DEFINITION_KNOWN).startswith("'dscr'")


def test_unit_mismatch_refused() -> None:
    report = verify_proposal(_proposal(unit="currency", currency="INR"), _context())

    assert report.all_passed is False
    assert VerificationCheckName.UNIT_CURRENCY_CONSISTENT.value in report.failed_checks
    detail = report.detail_for(VerificationCheckName.UNIT_CURRENCY_CONSISTENT)
    assert "dscr" in detail
    assert "ratio" in detail
    assert "currency" in detail


def test_effective_date_before_sanction_refused() -> None:
    report = verify_proposal(_proposal(effective_from="2025-06-01"), _context())

    assert report.all_passed is False
    assert VerificationCheckName.FREQUENCY_DATES_CONSISTENT.value in report.failed_checks
    detail = report.detail_for(VerificationCheckName.FREQUENCY_DATES_CONSISTENT)
    assert "2025-06-01" in detail
    assert str(_SANCTION_DATE) in detail


def test_ambiguous_frequency_refused_not_resolved() -> None:
    conflicting_quote = (
        "DSCR shall not fall below 1.5 times, tested quarterly, save that it is "
        "reviewed annually by the sanctioning authority."
    )
    proposal = _proposal(candidate_text=conflicting_quote, source_quote=conflicting_quote)
    assert proposal.frequency is None
    assert proposal.frequency_ambiguous is True

    report = verify_proposal(proposal, _context())

    assert report.all_passed is False
    assert VerificationCheckName.FREQUENCY_DATES_CONSISTENT.value in report.failed_checks
    detail = report.detail_for(VerificationCheckName.FREQUENCY_DATES_CONSISTENT)
    assert "ambiguous" in detail
