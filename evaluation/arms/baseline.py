"""Documented baseline arm for the evaluation harness.

The baseline intentionally has no access to product-only stages.  Its three
attempts are:

* a distance-to-threshold forecast using the latest observed slope;
* a bounded regular-expression extractor; and
* an ungrounded memo prompt, represented by the draft supplied by the example.

The implementation is useful precisely because it is small, deterministic and
honest about what it cannot attempt.  Shared scoring in :mod:`evaluation.score`
keeps its comparison with the product fair.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from covenant_radar.domain.ratios.library import LIBRARY
from evaluation import EvaluationError, EvaluationSkip
from evaluation.arms.product import reference_dataset
from evaluation.reference_portfolio.cohorts import BorrowerWithCohort, ReferenceCohorts

_THRESHOLD: Final[Decimal] = Decimal("85")
_HORIZON_DAYS: Final[int] = 90
_NUMBER = r"(?P<number>[+-]?\d+(?:\.\d+)?)"
_RATIO_ALIASES: Final[Mapping[str, tuple[str, ...]]] = {
    "leverage_ratio": (r"\bleverage\s+ratio\b", r"\bdebt[\s-]+equity\s+ratio\b"),
    "dscr": (r"\bdscr\b", r"\bdebt\s+service\s+coverage\s+ratio\b"),
    "interest_coverage_ratio": (r"\binterest\s+coverage\s+ratio\b", r"\bicr\b"),
    "current_ratio": (r"\bcurrent\s+ratio\b",),
    "quick_ratio": (r"\bquick\s+ratio\b", r"\bacid\s+test\s+ratio\b"),
    "tol_tnw": (r"\btol\s*/\s*tnw\b", r"\btotal\s+outside\s+liabilit(?:y|ies)\b"),
    "debt_to_ebitda": (r"\bdebt\s*/\s*ebitda\b", r"\bdebt\s+to\s+ebitda\b"),
    "net_debt_to_ebitda": (r"\bnet\s+debt\s*/\s*ebitda\b",),
    "ebitda_margin": (r"\bebitda\s+margin\b",),
    "utilisation": (r"\butili[sz]ation\b",),
    "drawing_power_headroom": (r"\bdrawing\s+power\b",),
    "receivable_days": (r"\b(?:receivable|debtor)\s+days\b",),
    "inventory_days": (r"\b(?:inventory|stock)\s+days\b",),
    "payable_days": (r"\b(?:payable|creditor)\s+days\b",),
    "cash_conversion_cycle": (r"\bcash\s+conversion\s+cycle\b",),
    "working_capital_gap": (r"\bworking\s+capital(?:\s+gap)?\b",),
    "asset_cover_ratio": (r"\basset\s+cover(?:age)?\s+ratio\b",),
    "minimum_liquidity": (r"\bminimum\s+liquidity\b",),
    "maximum_capex": (r"\b(?:capital\s+expenditure|capex)\b",),
    "tnw_floor": (r"\btangible\s+net\s+worth\b",),
    "minimum_net_worth": (r"\bnet\s+worth\b",),
}
_MAX_DIRECTION: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(?:not\s+)?exceed(?:ing)?\b", re.I),
    re.compile(r"\bno\s+more\s+than\b", re.I),
    re.compile(r"\bmaximum\b", re.I),
)
_MIN_DIRECTION: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(?:not\s+)?fall\s+below\b", re.I),
    re.compile(r"\bno\s+less\s+than\b", re.I),
    re.compile(r"\bat\s+least\b", re.I),
    re.compile(r"\bminimum\b", re.I),
)
_THRESHOLD_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(_NUMBER + r"\s*(?P<suffix>x|times)\b", re.I),
    re.compile(_NUMBER + r"\s*(?P<suffix>%|percent)\b", re.I),
    re.compile(_NUMBER + r"\s*(?P<suffix>days?)\b", re.I),
    re.compile(_NUMBER + r"\b", re.I),
)


class BaselineArmError(EvaluationError):
    """The baseline could not produce a trustworthy attempt."""


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BaselineArmError(f"{field_name} must be an object.")
    return value


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BaselineArmError(f"{field_name} must be an integer.")
    return value


def _date(value: object, field_name: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise BaselineArmError(f"{field_name} must be an ISO date.") from error
    raise BaselineArmError(f"{field_name} must be an ISO date.")


def _reference_context(
    example_input: Mapping[str, object],
) -> tuple[ReferenceCohorts, BorrowerWithCohort]:
    dataset_values = _mapping(example_input.get("reference_dataset"), "input.reference_dataset")
    dataset = reference_dataset(
        _integer(dataset_values.get("seed"), "input.reference_dataset.seed"),
        _integer(dataset_values.get("borrower_count"), "input.reference_dataset.borrower_count"),
        _integer(dataset_values.get("facility_count"), "input.reference_dataset.facility_count"),
        _integer(dataset_values.get("quarter_count"), "input.reference_dataset.quarter_count"),
    )
    reference = example_input.get("borrower_reference")
    borrower = next((item for item in dataset.borrowers if item.reference == reference), None)
    if borrower is None:
        raise BaselineArmError(f"Reference borrower {reference!r} does not exist.")
    return dataset, borrower


def naive_headroom_forecast(example_input: Mapping[str, object]) -> Mapping[str, object]:
    """Forecast by distance to the threshold divided by the latest slope."""

    dataset, borrower = _reference_context(example_input)
    events = tuple(dataset.signals.events_for_borrower(borrower.id, family="utilisation"))
    cohort = str(example_input.get("cohort"))
    label = dataset.labels.by_borrower.get(borrower.id)
    as_of = (
        label.breach_date - timedelta(days=1)
        if cohort == "deteriorating" and label
        else dataset.signal_end_date
    )
    history = tuple(event for event in events if event.event_date <= as_of)
    if not history:
        raise BaselineArmError(f"No utilisation history exists for {borrower.reference}.")
    current = history[-1].magnitude
    if current >= _THRESHOLD:
        crossing_date = as_of
    elif len(history) < 2:
        crossing_date = None
    else:
        previous = history[-2]
        elapsed = (history[-1].event_date - previous.event_date).days
        slope = (current - previous.magnitude) / Decimal(elapsed) if elapsed > 0 else Decimal("0")
        if slope <= 0:
            crossing_date = None
        else:
            days = int(((_THRESHOLD - current) / slope).to_integral_value(rounding="ROUND_CEILING"))
            crossing_date = as_of + timedelta(days=days) if days <= _HORIZON_DAYS else None
    label_date = label.breach_date if label is not None else None
    difference = abs((crossing_date - label_date).days) if crossing_date and label_date else None
    return {
        "crossing_date": crossing_date.isoformat() if crossing_date else None,
        "label_date": label_date.isoformat() if label_date else None,
        "difference_days": difference,
        "crossing_within_days_of_label": difference is not None and difference <= 10,
        "escalates": crossing_date is not None,
        "rule": "distance_to_threshold_using_latest_observed_slope",
    }


def regex_extract(clause_text: str) -> Mapping[str, object]:
    """Extract the first ratio, direction and threshold with regular expressions."""

    if not isinstance(clause_text, str) or not clause_text.strip():
        raise BaselineArmError("clause_text must be non-blank text.")
    definition_code = next(
        (
            code
            for code, patterns in _RATIO_ALIASES.items()
            if any(re.search(pattern, clause_text, re.I) for pattern in patterns)
        ),
        None,
    )
    direction = (
        "max"
        if any(pattern.search(clause_text) for pattern in _MAX_DIRECTION)
        else "min"
        if any(pattern.search(clause_text) for pattern in _MIN_DIRECTION)
        else None
    )
    threshold_match = next((pattern.search(clause_text) for pattern in _THRESHOLD_PATTERNS), None)
    threshold = None
    unit = None
    if threshold_match is not None:
        threshold = threshold_match.group("number")
        suffix = threshold_match.groupdict().get("suffix", "")
        suffix = suffix.casefold()
        unit = (
            "ratio"
            if suffix in {"x", "times"}
            else "percent"
            if suffix in {"%", "percent"}
            else "days"
            if suffix in {"day", "days"}
            else None
        )
    refused = definition_code is None or direction is None or threshold is None
    plausible = None
    if not refused and definition_code in LIBRARY and threshold is not None:
        threshold_value = Decimal(threshold)
        definition = LIBRARY[definition_code]
        plausible = (
            definition.plausible_min is None or threshold_value >= definition.plausible_min
        ) and (definition.plausible_max is None or threshold_value <= definition.plausible_max)
    return {
        "refused": refused,
        "definition_code": definition_code,
        "direction": direction,
        "threshold": threshold,
        "unit": unit,
        "plausible": plausible,
        "refusal_reason": "regex_parser_could_not_extract_all_fields" if refused else None,
    }


def ungrounded_memo_prompt(memo_draft: Mapping[str, object]) -> Mapping[str, object]:
    """Return the supplied draft as the baseline's ungrounded prompt result."""

    if not isinstance(memo_draft, Mapping):
        raise BaselineArmError("memo_draft must be an object.")
    return dict(memo_draft)


def _extraction(example_input: Mapping[str, object]) -> Mapping[str, object]:
    return regex_extract(str(example_input.get("clause_text", "")))


def run_baseline_example(example: Mapping[str, object]) -> Mapping[str, object]:
    """Run one baseline-supported example.

    Product-only categories raise :class:`EvaluationSkip`; the runner records
    them as not applicable rather than as failed baseline observations.
    """

    if not isinstance(example, Mapping):
        raise TypeError("run_baseline_example requires an example mapping.")
    kind = example.get("kind")
    raw_input = _mapping(example.get("input"), "example.input")
    if kind == "forecast_dating" or kind == "false_escalation":
        return naive_headroom_forecast(raw_input)
    if kind == "extraction":
        return _extraction(raw_input)
    if kind == "usefulness":
        return ungrounded_memo_prompt(_mapping(raw_input.get("memo_draft"), "input.memo_draft"))
    raise EvaluationSkip(f"baseline not applicable to category {kind!r}")


__all__ = [
    "BaselineArmError",
    "naive_headroom_forecast",
    "regex_extract",
    "run_baseline_example",
    "ungrounded_memo_prompt",
]
