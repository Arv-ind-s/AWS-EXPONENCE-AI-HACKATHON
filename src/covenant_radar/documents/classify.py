"""Rule-based document-type classification.

`spec §R-04.e` names five categories: sanction letter, amendment, compliance
certificate, stock statement and other. This module scores a document's
earliest extracted page text against a fixed vocabulary for the four named
*financial* categories only. ``other`` is a real, selectable category — a
human can classify a document as one that is clearly none of the four — but
it is deliberately not auto-detected: a rule-based scorer has no positive
vocabulary for "not one of these four", and inventing negative evidence
would mean guessing exactly where guessing is forbidden. A document that
does not confidently match one of the four scored categories is
``unclassified``, never ``other``, and is offered for manual selection
instead.

This module is intentionally pure: it never touches a database, a request,
or a permission. `services/documents.py` is the only caller and owns the
scope checks, the page selection, and the persisted manual-override record.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, NamedTuple

DOCUMENT_TYPES: Final[tuple[str, ...]] = (
    "sanction_letter",
    "amendment",
    "compliance_certificate",
    "stock_statement",
    "other",
)
UNCLASSIFIED_DOC_TYPE: Final[str] = "unclassified"

# Every value a classification decision may carry: the four scored types,
# the manual-only "other" bucket, and the fail-closed "unclassified" state.
_ALL_DOC_TYPE_VALUES: Final[frozenset[str]] = frozenset((*DOCUMENT_TYPES, UNCLASSIFIED_DOC_TYPE))

# "other" is excluded: see the module docstring for why it is manual-only.
_SCORED_DOC_TYPES: Final[tuple[str, ...]] = tuple(
    doc_type for doc_type in DOCUMENT_TYPES if doc_type != "other"
)

DEFAULT_CONFIDENCE_FLOOR: Final[Decimal] = Decimal("0.55")


class _Rule(NamedTuple):
    """One weighted vocabulary signal for a scored document type."""

    doc_type: str
    label: str
    weight: Decimal
    pattern: re.Pattern[str]


def _rule(doc_type: str, label: str, weight: str, pattern: str) -> _Rule:
    if doc_type not in _SCORED_DOC_TYPES:
        raise ValueError(f"Classification rules may only score {_SCORED_DOC_TYPES}.")
    return _Rule(doc_type, label, Decimal(weight), re.compile(pattern, re.IGNORECASE))


# Indian sanction-letter idiom. Weights are additive per matched rule, capped
# at 1.0 per document type; a single strong phrase can clear the confidence
# floor on its own, while several weaker phrases can combine to clear it too.
_RULES: Final[tuple[_Rule, ...]] = (
    _rule(
        "sanction_letter",
        "pleased to sanction",
        "0.45",
        r"\bwe are pleased to (?:sanction|inform you)\b",
    ),
    _rule("sanction_letter", "sanction letter", "0.35", r"\bsanction letter\b"),
    _rule(
        "sanction_letter",
        "sanctioned limit or amount",
        "0.30",
        r"\bsanctioned (?:limit|amount)\b",
    ),
    _rule(
        "sanction_letter",
        "facility named by type",
        "0.20",
        r"\b(?:cash credit|term loan|overdraft) facility\b",
    ),
    _rule(
        "amendment",
        "amendment to sanction letter",
        "0.45",
        r"\bamendment to (?:the )?sanction letter\b",
    ),
    _rule("amendment", "addendum", "0.35", r"\baddendum\b"),
    _rule(
        "amendment",
        "revised terms and conditions",
        "0.30",
        r"\brevised terms and conditions\b",
    ),
    _rule("amendment", "supplementary sanction", "0.25", r"\bsupplementary sanction\b"),
    _rule(
        "compliance_certificate",
        "compliance certificate",
        "0.45",
        r"\bcompliance certificate\b",
    ),
    _rule(
        "compliance_certificate",
        "we certify that",
        "0.30",
        r"\bwe (?:hereby )?certify that\b",
    ),
    _rule("compliance_certificate", "covenant compliance", "0.30", r"\bcovenant compliance\b"),
    _rule("compliance_certificate", "statutory dues", "0.20", r"\bstatutory dues\b"),
    _rule("stock_statement", "stock statement", "0.45", r"\bstock statement\b"),
    _rule("stock_statement", "drawing power", "0.35", r"\bdrawing power\b"),
    _rule("stock_statement", "book debts", "0.25", r"\bbook debts\b"),
    _rule("stock_statement", "hypothecated stock", "0.20", r"\bhypothecated stock\b"),
)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """One rule-based classification decision, never a guess.

    ``doc_type`` is either one of the four scored :data:`DOCUMENT_TYPES` or
    :data:`UNCLASSIFIED_DOC_TYPE` — it is never the manual-only ``"other"``,
    since nothing here scores it. ``confidence`` is the winning score even
    when unclassified, so a reviewer can see how close the nearest guess
    came. ``scores`` carries every scored type's score for full
    transparency and testability.
    """

    doc_type: str
    confidence: Decimal
    matched_rules: tuple[str, ...] = ()
    scores: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.doc_type, str) or self.doc_type not in _ALL_DOC_TYPE_VALUES:
            raise ValueError(f"Unknown classification doc_type: {self.doc_type!r}.")
        if not isinstance(self.confidence, Decimal) or not self.confidence.is_finite():
            raise TypeError("Classification confidence must be a finite Decimal.")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("Classification confidence must be between 0 and 1.")
        object.__setattr__(self, "matched_rules", tuple(self.matched_rules))
        object.__setattr__(self, "scores", dict(self.scores))

    @property
    def is_unclassified(self) -> bool:
        """Whether this result requires manual selection rather than trust."""
        return self.doc_type == UNCLASSIFIED_DOC_TYPE


def classify_text(
    text: str,
    *,
    confidence_floor: Decimal = DEFAULT_CONFIDENCE_FLOOR,
) -> ClassificationResult:
    """Score one block of already-extracted text against the vocabulary.

    Below the floor, the result is :data:`UNCLASSIFIED_DOC_TYPE` — recorded
    as such rather than reported as the nearest guess — so a caller can
    never mistake a low-confidence lean for a decision.
    """
    if not isinstance(text, str):
        raise TypeError("classify_text requires a string.")
    floor = _validated_floor(confidence_floor)

    scores: dict[str, Decimal] = dict.fromkeys(_SCORED_DOC_TYPES, Decimal("0"))
    matched: dict[str, list[str]] = {doc_type: [] for doc_type in _SCORED_DOC_TYPES}
    if text.strip():
        for rule in _RULES:
            if rule.pattern.search(text):
                scores[rule.doc_type] = min(Decimal("1"), scores[rule.doc_type] + rule.weight)
                matched[rule.doc_type].append(rule.label)

    best_type = max(_SCORED_DOC_TYPES, key=lambda doc_type: scores[doc_type])
    best_score = scores[best_type]
    decided_type = best_type if best_score >= floor else UNCLASSIFIED_DOC_TYPE
    return ClassificationResult(
        doc_type=decided_type,
        confidence=best_score,
        matched_rules=tuple(matched[best_type]),
        scores=scores,
    )


def classify_pages(
    pages: Iterable[tuple[int, str]],
    *,
    confidence_floor: Decimal = DEFAULT_CONFIDENCE_FLOOR,
) -> ClassificationResult:
    """Classify a document from a caller-selected set of its page texts.

    ``pages`` is normally the document's earliest already-extracted,
    review-eligible pages — the service layer selects and bounds that set,
    since a document type is ordinarily evident from the first page or two
    of a sanction letter. This function only orders and joins what it is
    given; it applies no eligibility rule of its own.
    """
    ordered = sorted(pages, key=lambda item: item[0])
    combined = "\n".join(text for _, text in ordered if text)
    return classify_text(combined, confidence_floor=confidence_floor)


def _validated_floor(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError("confidence_floor must be a Decimal.")
    if not value.is_finite() or not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("confidence_floor must be between 0 and 1.")
    return value


__all__ = [
    "DEFAULT_CONFIDENCE_FLOOR",
    "DOCUMENT_TYPES",
    "UNCLASSIFIED_DOC_TYPE",
    "ClassificationResult",
    "classify_pages",
    "classify_text",
]
