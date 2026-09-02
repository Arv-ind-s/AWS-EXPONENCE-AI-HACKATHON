"""Rule-based clause-candidate detection over already-extracted page text.

`spec §R-06` and `plan.md §8`'s narrowing step: find the lines of a sanction
letter that might state a financial covenant, so `T-094` sends the model one
small, bounded clause at a time instead of a forty-page document. Detection
is deterministic — five categories of regular-expression signal, scored per
line and never guessed — and this module never touches a database, an HTTP
client or a model provider, matching the layering `domain/ratios/` already
establishes: pure functions over plain values, exercised by unit tests alone.

Ratio-name vocabulary is derived from `domain.ratios.library.LIBRARY` rather
than duplicated here, so the two modules cannot silently drift apart; a
curated alias map adds the abbreviations and phrasing (``DSCR``, ``TOL/TNW``)
an Indian sanction letter actually uses, since a ratio's formal
`RatioDefinition.name` is rarely the word a banker writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, NamedTuple

from covenant_radar.domain.ratios.library import LIBRARY

DEFAULT_MATCH_FLOOR: Final[Decimal] = Decimal("0.50")
DEFAULT_RECALL_FLOOR: Final[Decimal] = Decimal("0.90")

# A page-break candidate only ever borrows the first line of the following
# page, and only while its own text still lacks a threshold figure; this
# bounds how many pages one clause can pull in before detection gives up
# rather than merging without limit.
_MAX_PAGE_BREAK_MERGE: Final[int] = 2

_CATEGORY_WEIGHT: Final[Mapping[str, Decimal]] = {
    "heading": Decimal("0.20"),
    "vocabulary": Decimal("0.30"),
    "ratio_name": Decimal("0.35"),
    "comparison": Decimal("0.35"),
    "threshold": Decimal("0.35"),
}


class _Rule(NamedTuple):
    """One regular-expression signal contributing to a line's match score."""

    category: str
    label: str
    pattern: re.Pattern[str]


def _rule(category: str, label: str, pattern: str) -> _Rule:
    if category not in _CATEGORY_WEIGHT:
        raise ValueError(f"Unknown clause-detection rule category: {category!r}.")
    return _Rule(category, label, re.compile(pattern, re.IGNORECASE))


# Every ratio code this module scores for, paired with the phrasing an Indian
# sanction letter actually uses for it. Validated against `LIBRARY` below so a
# renamed or removed ratio code fails the import rather than silently going
# unmatched.
_RATIO_NAME_ALIASES: Final[Mapping[str, tuple[str, ...]]] = {
    "leverage_ratio": (r"\bleverage ratio\b", r"\bdebt[\s-]*equity ratio\b", r"\bgearing ratio\b"),
    "dscr": (r"\bdscr\b", r"\bdebt service coverage ratio\b"),
    "interest_coverage_ratio": (r"\binterest coverage ratio\b", r"\bicr\b"),
    "fixed_charge_coverage_ratio": (r"\bfixed[\s-]*charge coverage ratio\b", r"\bfccr\b"),
    "current_ratio": (r"\bcurrent ratio\b",),
    "quick_ratio": (r"\bquick ratio\b", r"\bacid test ratio\b"),
    "tol_tnw": (r"\btol\s*/\s*tnw\b", r"\btotal outside liabilit(?:y|ies)\b"),
    "debt_to_ebitda": (r"\bdebt\s*/\s*ebitda\b", r"\bdebt to ebitda\b"),
    "net_debt_to_ebitda": (r"\bnet debt\s*/\s*ebitda\b",),
    "ebitda_margin": (r"\bebitda margin\b",),
    "tnw_floor": (r"\btangible net worth\b", r"\btnw\b"),
    "minimum_net_worth": (r"\bnet worth\b",),
    "utilisation": (r"\butili[sz]ation\b",),
    "drawing_power_headroom": (r"\bdrawing power\b",),
    "receivable_days": (r"\breceivable days\b", r"\bdebtor days\b"),
    "inventory_days": (r"\binventory days\b", r"\bstock days\b"),
    "payable_days": (r"\bpayable days\b", r"\bcreditor days\b"),
    "cash_conversion_cycle": (r"\bcash conversion cycle\b",),
    "working_capital_gap": (r"\bworking capital gap\b", r"\bworking capital\b"),
    "asset_cover_ratio": (r"\basset cover(?:age)? ratio\b",),
    "minimum_liquidity": (r"\bminimum liquidity\b", r"\bcash and bank balances?\b"),
    "maximum_capex": (r"\bcapital expenditure\b", r"\bcapex\b"),
    "dividend_restriction": (r"\bdividend\b",),
    "promoter_shareholding_floor": (r"\bpromoter shareholding\b",),
}

_unknown_codes = sorted(set(_RATIO_NAME_ALIASES) - set(LIBRARY))
if _unknown_codes:
    raise ValueError(
        f"Clause-detection ratio aliases name unknown ratio codes: {', '.join(_unknown_codes)}."
    )


def _ratio_name_rules() -> tuple[_Rule, ...]:
    return tuple(
        _rule("ratio_name", f"ratio:{code}", pattern)
        for code, patterns in _RATIO_NAME_ALIASES.items()
        for pattern in patterns
    )


# Comparison language a covenant threshold is stated against.
_COMPARISON_PHRASES: Final[tuple[str, ...]] = (
    r"\bshall not exceed\b",
    r"\bshall be maintained\b",
    r"\bshall be at least\b",
    r"\bshall not fall below\b",
    r"\bshall not be less than\b",
    r"\bshall remain (?:above|below)\b",
    r"\bnot (?:to )?exceed\b",
    r"\bnot fall below\b",
    r"\bnot be less than\b",
    r"\bno less than\b",
    r"\bno more than\b",
    r"\bat least\b",
    r"\bminimum of\b",
    r"\bmaximum of\b",
    r"\bat all times\b",
    r">=|<=|≥|≤",
)

# A numeric threshold, in the units an Indian sanction letter states one in.
_THRESHOLD_PATTERNS: Final[tuple[str, ...]] = (
    r"\b\d+(?:\.\d+)?\s*(?:x|times)\b",
    r"\b\d+(?:\.\d+)?\s*%",
    r"₹\s?\d+(?:\.\d+)?\s*(?:crore|lakh)\b",
    r"\b\d+\.\d{1,2}\b",
    r"\b\d+\s*days\b",
)

# Financial-covenant vocabulary not already covered by a ratio name.
_VOCABULARY_PHRASES: Final[tuple[str, ...]] = (
    r"\bfinancial covenants?\b",
    r"\bcovenant compliance\b",
    r"\bshall maintain\b",
    r"\bborrower undertakes\b",
    r"\bhereby covenants?\b",
    r"\bshall ensure that\b",
    r"\bevents? of default\b",
)

# Section headings that mark where a covenants block begins; never enough
# alone to reach the match floor (see `_CATEGORY_WEIGHT`), so a numbered list
# item is never mistaken for the section header that precedes it.
_HEADING_PATTERNS: Final[tuple[str, ...]] = (
    r"\bcovenants and conditions\b",
    r"\bterms and conditions\b",
    r"\bconditions of sanction\b",
    r"^\s*(?:\d+\.)+\s*\S",
)


def _rules_from(category: str, phrases: tuple[str, ...]) -> tuple[_Rule, ...]:
    return tuple(
        _rule(category, f"{category}:{index}", phrase) for index, phrase in enumerate(phrases)
    )


_RULES: Final[tuple[_Rule, ...]] = (
    _ratio_name_rules()
    + _rules_from("comparison", _COMPARISON_PHRASES)
    + _rules_from("threshold", _THRESHOLD_PATTERNS)
    + _rules_from("vocabulary", _VOCABULARY_PHRASES)
    + _rules_from("heading", _HEADING_PATTERNS)
)

_ALL_RULE_LABELS: Final[tuple[str, ...]] = tuple(rule.label for rule in _RULES)


@dataclass(frozen=True, slots=True)
class CandidateLine:
    """One already-extracted line of page text, scored as a detection unit."""

    page_number: int
    start_offset: int
    end_offset: int
    text: str

    def __post_init__(self) -> None:
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int):
            raise TypeError("CandidateLine.page_number must be an integer.")
        if self.page_number < 1:
            raise ValueError("CandidateLine.page_number must be one-based.")
        for name, value in (("start_offset", self.start_offset), ("end_offset", self.end_offset)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"CandidateLine.{name} must be a non-negative integer.")
        if self.end_offset <= self.start_offset:
            raise ValueError("CandidateLine.end_offset must be after start_offset.")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("CandidateLine.text must be non-empty.")


@dataclass(frozen=True, slots=True)
class CandidatePage:
    """One page's already-extracted text, eligible or not for detection.

    ``needs_review`` mirrors `documents.ocr.page_is_eligible_for_detection`'s
    rule: a page a person has not yet confirmed can never reach a candidate,
    even if the caller forgets to filter it out first — the same defensive
    recheck `DocumentService.list_detection_pages` already applies at its own
    layer.
    """

    page_number: int
    text: str | None
    lines: tuple[CandidateLine, ...] = ()
    needs_review: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int):
            raise TypeError("CandidatePage.page_number must be an integer.")
        if self.page_number < 1:
            raise ValueError("CandidatePage.page_number must be one-based.")
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("CandidatePage.text must be a string or None.")
        if not isinstance(self.needs_review, bool):
            raise TypeError("CandidatePage.needs_review must be boolean.")
        object.__setattr__(self, "lines", tuple(self.lines))
        expected_start = 0
        for line in self.lines:
            if not isinstance(line, CandidateLine):
                raise TypeError("CandidatePage.lines must contain CandidateLine values.")
            if line.page_number != self.page_number:
                raise ValueError("A CandidatePage line belongs to a different page.")
            if line.start_offset < expected_start:
                raise ValueError("CandidatePage.lines must be ordered by offset.")
            expected_start = line.end_offset

    @property
    def is_eligible(self) -> bool:
        """Whether this page's text may be searched for a clause candidate."""
        return isinstance(self.text, str) and bool(self.text.strip()) and not self.needs_review


@dataclass(frozen=True, slots=True)
class ClauseCandidate:
    """One detected clause candidate: its text, its span(s), and why.

    ``start_page``/``end_page`` differ only for a candidate whose clause was
    split across a page boundary; every other candidate is single-page.
    """

    start_page: int
    start_offset: int
    end_page: int
    end_offset: int
    text: str
    matched_rules: tuple[str, ...]
    lines: tuple[CandidateLine, ...]

    def __post_init__(self) -> None:
        for name, value in (("start_page", self.start_page), ("end_page", self.end_page)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"ClauseCandidate.{name} must be a positive integer.")
        if self.end_page < self.start_page:
            raise ValueError("ClauseCandidate.end_page cannot precede start_page.")
        for name, value in (("start_offset", self.start_offset), ("end_offset", self.end_offset)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"ClauseCandidate.{name} must be a non-negative integer.")
        if self.start_page == self.end_page and self.end_offset <= self.start_offset:
            raise ValueError(
                "A single-page ClauseCandidate must have end_offset after start_offset."
            )
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("ClauseCandidate.text must be non-empty.")
        object.__setattr__(self, "matched_rules", tuple(self.matched_rules))
        if not self.matched_rules:
            raise ValueError("ClauseCandidate.matched_rules must name at least one matched rule.")
        object.__setattr__(self, "lines", tuple(self.lines))
        if not self.lines:
            raise ValueError("ClauseCandidate.lines must contain at least one line.")
        for line in self.lines:
            if not isinstance(line, CandidateLine):
                raise TypeError("ClauseCandidate.lines must contain CandidateLine values.")
            if not self.start_page <= line.page_number <= self.end_page:
                raise ValueError("A ClauseCandidate line's page is outside its own page range.")

    @property
    def pages(self) -> tuple[int, ...]:
        """Every one-based page number this candidate's text touches."""
        return tuple(range(self.start_page, self.end_page + 1))

    @property
    def spans_page_break(self) -> bool:
        """Whether this candidate's clause was split across a page boundary."""
        return self.start_page != self.end_page


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Every candidate found in one document, and every rule that was tried.

    ``rules_tried`` is populated even when ``candidates`` is empty, so a
    caller can tell "no covenants on this page" from "detection never ran."
    """

    candidates: tuple[ClauseCandidate, ...]
    rules_tried: tuple[str, ...] = _ALL_RULE_LABELS

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "rules_tried", tuple(self.rules_tried))
        if not self.rules_tried:
            raise ValueError("DetectionResult.rules_tried must name every rule attempted.")

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)


def _matched_categories(text: str) -> frozenset[str]:
    return frozenset(rule.category for rule in _RULES if rule.pattern.search(text))


def _score_line(text: str) -> tuple[Decimal, tuple[str, ...]]:
    """Score one line: the summed, capped per-category weight and every
    individual rule label that matched, in declaration order."""
    if not text or not text.strip():
        return Decimal("0"), ()
    category_scores: dict[str, Decimal] = {}
    matched_labels: list[str] = []
    for rule in _RULES:
        if rule.pattern.search(text):
            weight = _CATEGORY_WEIGHT[rule.category]
            existing = category_scores.get(rule.category, Decimal("0"))
            if weight > existing:
                category_scores[rule.category] = weight
            matched_labels.append(rule.label)
    total = min(Decimal("1"), sum(category_scores.values(), Decimal("0")))
    return total, tuple(matched_labels)


def _validated_pages(pages: Iterable[CandidatePage]) -> tuple[CandidatePage, ...]:
    ordered = sorted(pages, key=lambda page: page.page_number)
    seen: set[int] = set()
    for page in ordered:
        if not isinstance(page, CandidatePage):
            raise TypeError("detect_candidates requires CandidatePage values.")
        if page.page_number in seen:
            raise ValueError(
                f"Duplicate page number {page.page_number} in detect_candidates input."
            )
        seen.add(page.page_number)
    return tuple(ordered)


def _validated_floor(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError("match_floor must be a Decimal.")
    if not value.is_finite() or not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("match_floor must be between 0 and 1.")
    return value


def detect_candidates(
    pages: Iterable[CandidatePage],
    *,
    match_floor: Decimal = DEFAULT_MATCH_FLOOR,
) -> DetectionResult:
    """Find every clause candidate across a document's eligible pages.

    A page flagged ``needs_review`` never contributes a line, matching or
    not. A candidate whose last matched line is the final line on its page
    and still lacks a ``threshold`` match is extended into the first line of
    the following eligible page — capped at `_MAX_PAGE_BREAK_MERGE` pages —
    so a threshold pushed onto the next page by pagination is not lost.
    """
    floor = _validated_floor(match_floor)
    ordered_pages = _validated_pages(pages)
    eligible = {page.page_number: page for page in ordered_pages if page.is_eligible}
    consumed: set[tuple[int, int]] = set()
    candidates: list[ClauseCandidate] = []

    for page_number in sorted(eligible):
        page = eligible[page_number]
        for index, line in enumerate(page.lines):
            if (page_number, index) in consumed:
                continue
            score, matched_labels = _score_line(line.text)
            if score < floor:
                continue
            candidates.append(
                _build_candidate(
                    page_number,
                    index,
                    line,
                    matched_labels,
                    eligible,
                    consumed,
                )
            )

    return DetectionResult(candidates=tuple(candidates), rules_tried=_ALL_RULE_LABELS)


def _build_candidate(
    page_number: int,
    index: int,
    line: CandidateLine,
    matched_labels: tuple[str, ...],
    eligible: Mapping[int, CandidatePage],
    consumed: set[tuple[int, int]],
) -> ClauseCandidate:
    lines = [line]
    matched_rules = list(matched_labels)
    categories = _matched_categories(line.text)
    end_page = page_number
    end_offset = line.end_offset
    current_page_number = page_number
    is_last_on_page = index == len(eligible[page_number].lines) - 1
    merges = 0

    while (
        is_last_on_page
        and "threshold" not in categories
        and merges < _MAX_PAGE_BREAK_MERGE
    ):
        next_page_number = current_page_number + 1
        next_page = eligible.get(next_page_number)
        if next_page is None or not next_page.lines or (next_page_number, 0) in consumed:
            break
        next_line = next_page.lines[0]
        lines.append(next_line)
        _, next_matched = _score_line(next_line.text)
        for label in next_matched:
            if label not in matched_rules:
                matched_rules.append(label)
        categories |= _matched_categories(next_line.text)
        consumed.add((next_page_number, 0))
        end_page = next_page_number
        end_offset = next_line.end_offset
        current_page_number = next_page_number
        is_last_on_page = len(next_page.lines) == 1
        merges += 1

    return ClauseCandidate(
        start_page=page_number,
        start_offset=line.start_offset,
        end_page=end_page,
        end_offset=end_offset,
        text=" ".join(one_line.text for one_line in lines),
        matched_rules=tuple(matched_rules),
        lines=tuple(lines),
    )


@dataclass(frozen=True, slots=True)
class RecallReport:
    """How many labelled ground-truth clauses a detection run recovered."""

    total_ground_truth: int
    matched: int
    missed: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        is_int = isinstance(self.total_ground_truth, int)
        if isinstance(self.total_ground_truth, bool) or not is_int:
            raise TypeError("RecallReport.total_ground_truth must be an integer.")
        if self.total_ground_truth < 1:
            raise ValueError("RecallReport.total_ground_truth must be positive.")
        if isinstance(self.matched, bool) or not isinstance(self.matched, int):
            raise TypeError("RecallReport.matched must be an integer.")
        if not 0 <= self.matched <= self.total_ground_truth:
            raise ValueError("RecallReport.matched must be between 0 and total_ground_truth.")
        object.__setattr__(self, "missed", tuple(self.missed))
        if len(self.missed) != self.total_ground_truth - self.matched:
            raise ValueError("RecallReport.missed must name every unmatched clause, once each.")

    @property
    def recall(self) -> Decimal:
        return Decimal(self.matched) / Decimal(self.total_ground_truth)


def measure_recall(result: DetectionResult, ground_truth: Iterable[str]) -> RecallReport:
    """Score one detection run against hand-labelled ground-truth clauses.

    A ground-truth clause counts as recovered when its exact text appears
    within some candidate's text — the same substring relationship a
    reviewer checking a candidate against the source document would use.
    """
    truths = tuple(ground_truth)
    if not truths:
        raise ValueError("measure_recall requires at least one ground-truth clause.")
    candidate_texts = tuple(candidate.text for candidate in result.candidates)
    missed: list[str] = []
    matched = 0
    for clause in truths:
        if any(clause in text for text in candidate_texts):
            matched += 1
        else:
            missed.append(clause)
    return RecallReport(total_ground_truth=len(truths), matched=matched, missed=tuple(missed))


__all__ = [
    "DEFAULT_MATCH_FLOOR",
    "DEFAULT_RECALL_FLOOR",
    "CandidateLine",
    "CandidatePage",
    "ClauseCandidate",
    "DetectionResult",
    "RecallReport",
    "detect_candidates",
    "measure_recall",
]
