"""Unit tests for T-093 rule-based clause-candidate detection."""

from __future__ import annotations

import pytest

from covenant_radar.domain.intake.candidates import (
    CandidateLine,
    CandidatePage,
    detect_candidates,
)

pytestmark = pytest.mark.unit


def _page(
    page_number: int,
    lines: tuple[str, ...],
    *,
    needs_review: bool = False,
) -> CandidatePage:
    candidate_lines: list[CandidateLine] = []
    offset = 0
    for text in lines:
        candidate_lines.append(
            CandidateLine(
                page_number=page_number,
                start_offset=offset,
                end_offset=offset + len(text),
                text=text,
            )
        )
        offset += len(text) + 1
    return CandidatePage(
        page_number=page_number,
        text="\n".join(lines) if lines else None,
        lines=tuple(candidate_lines),
        needs_review=needs_review,
    )


def test_review_flagged_pages_excluded() -> None:
    page = _page(
        1,
        ("Current ratio shall be maintained above 1.20",),
        needs_review=True,
    )

    result = detect_candidates([page])

    assert result.candidates == ()
    assert result.rules_tried != ()


def test_page_break_candidate_captured_whole() -> None:
    page_one = _page(1, ("DSCR shall be maintained at a minimum of",))
    page_two = _page(2, ("1.25 times per annum, tested quarterly.",))

    result = detect_candidates([page_one, page_two])

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.start_page == 1
    assert candidate.end_page == 2
    assert candidate.spans_page_break is True
    assert candidate.text == (
        "DSCR shall be maintained at a minimum of 1.25 times per annum, tested quarterly."
    )
    assert len(candidate.lines) == 2
    assert candidate.lines[0].page_number == 1
    assert candidate.lines[1].page_number == 2
    assert any(label.startswith("threshold:") for label in candidate.matched_rules)
    assert any(label == "ratio:dscr" for label in candidate.matched_rules)


def test_multiple_rules_one_candidate() -> None:
    page = _page(
        1,
        ("The leverage ratio (Total Debt/TNW) shall not exceed 3.00x at all times.",),
    )

    result = detect_candidates([page])

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert len(candidate.matched_rules) > 1
    assert "ratio:leverage_ratio" in candidate.matched_rules
    assert "ratio:tnw_floor" in candidate.matched_rules
    assert any(label.startswith("comparison:") for label in candidate.matched_rules)
    assert any(label.startswith("threshold:") for label in candidate.matched_rules)


def test_no_candidates_reports_rules_tried() -> None:
    page = _page(
        1,
        (
            "This sanction letter is valid for thirty days from the date of issue.",
            "Please acknowledge receipt of this letter and return the duplicate copy.",
            "Yours faithfully,",
            "Branch Manager",
        ),
    )

    result = detect_candidates([page])

    assert result.candidates == ()
    assert result.has_candidates is False
    assert len(result.rules_tried) > 0
