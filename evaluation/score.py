"""Shared scoring for the product and baseline evaluation arms.

Both arms are scored through this module.  An arm cannot change the metric
used for a category, and a skipped or non-applicable example is never turned
into a zero.  This symmetry is the important control in the permanent
baseline comparison.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Final

from evaluation import EvaluationError

_DECIMAL_TEXT: Final[re.Pattern[str]] = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$")
_ARMS: Final[frozenset[str]] = frozenset({"product", "baseline"})
_STATUSES: Final[frozenset[str]] = frozenset({"scored", "skipped"})


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int | float):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None
    if isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value.strip()):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            return None
        return result if result.is_finite() else None
    return None


def _values_equal(expected: object, actual: object) -> bool:
    """Compare JSON values, treating decimal strings as exact Decimals."""

    expected_decimal = _decimal(expected)
    actual_decimal = _decimal(actual)
    if expected_decimal is not None and actual_decimal is not None:
        return expected_decimal == actual_decimal
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or set(expected) != set(actual):
            return False
        return all(_values_equal(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, Sequence) and not isinstance(expected, str | bytes | bytearray):
        if not isinstance(actual, Sequence) or isinstance(actual, str | bytes | bytearray):
            return False
        return len(expected) == len(actual) and all(
            _values_equal(left, right) for left, right in zip(expected, actual, strict=True)
        )
    if isinstance(expected, date) and not isinstance(expected, datetime):
        if isinstance(actual, str):
            try:
                return expected == date.fromisoformat(actual)
            except ValueError:
                return False
    if isinstance(expected, bool):
        return isinstance(actual, bool) and expected == actual
    return expected == actual


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(child) for child in value]
    if isinstance(value, set | frozenset):
        return [_json_value(child) for child in sorted(value, key=str)]
    return value


def _require_example_fields(example: Mapping[str, object]) -> tuple[str, str, Mapping[str, object]]:
    example_id = example.get("id")
    kind = example.get("kind")
    expected = example.get("expected")
    if not isinstance(example_id, str) or not example_id.strip():
        raise TypeError("An evaluation example requires a non-blank string id.")
    if not isinstance(kind, str) or not kind.strip():
        raise TypeError(f"Evaluation example {example_id!r} requires a kind.")
    if not isinstance(expected, Mapping):
        raise TypeError(f"Evaluation example {example_id!r} requires an expected mapping.")
    return example_id, kind, expected


@dataclass(frozen=True, slots=True)
class ExampleScore:
    """The score for one attempted or skipped example."""

    example_id: str
    arm: str
    kind: str
    status: str
    passed: bool | None
    metric: Mapping[str, object] = field(default_factory=dict)
    actual: Mapping[str, object] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.example_id.strip():
            raise ValueError("ExampleScore.example_id must be non-blank.")
        if self.arm not in _ARMS:
            raise ValueError(f"Unknown evaluation arm {self.arm!r}.")
        if not self.kind.strip():
            raise ValueError("ExampleScore.kind must be non-blank.")
        if self.status not in _STATUSES:
            raise ValueError(f"Unknown example score status {self.status!r}.")
        if self.status == "scored" and not isinstance(self.passed, bool):
            raise TypeError("A scored example must carry a boolean passed value.")
        if self.status == "skipped" and self.passed is not None:
            raise ValueError("A skipped example must not carry a passed value.")
        if self.status == "skipped" and (self.reason is None or not self.reason.strip()):
            raise ValueError("A skipped example must name its reason.")
        if not isinstance(self.metric, Mapping) or not isinstance(self.actual, Mapping):
            raise TypeError("ExampleScore.metric and actual must be mappings.")
        object.__setattr__(self, "metric", MappingProxyType(dict(self.metric)))
        object.__setattr__(self, "actual", MappingProxyType(dict(self.actual)))

    @property
    def scored(self) -> bool:
        return self.status == "scored"

    @property
    def quality(self) -> Decimal | None:
        value = _decimal(self.metric.get("quality"))
        return value

    def as_mapping(self) -> dict[str, object]:
        return {
            "example_id": self.example_id,
            "arm": self.arm,
            "kind": self.kind,
            "status": self.status,
            "passed": self.passed,
            "metric": _json_value(self.metric),
            "actual": _json_value(self.actual),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CategoryScore:
    """An aggregate category result for one arm."""

    category: str
    applicable: bool
    attempted: int
    passed: int
    skipped: int
    score: Decimal | None
    example_scores: tuple[ExampleScore, ...] = ()

    def __post_init__(self) -> None:
        if not self.category.strip():
            raise ValueError("CategoryScore.category must be non-blank.")
        for name in ("attempted", "passed", "skipped"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"CategoryScore.{name} must be a non-negative integer.")
        if self.passed > self.attempted:
            raise ValueError("CategoryScore.passed cannot exceed attempted.")
        if self.applicable != (self.attempted > 0):
            raise ValueError("CategoryScore.applicable must agree with its example counts.")
        if self.applicable and self.score is None:
            raise ValueError("An applicable category must carry a score.")
        if not self.applicable and self.score is not None:
            raise ValueError("A non-applicable category must not carry a score.")
        if self.score is not None and not Decimal("0") <= self.score <= Decimal("1"):
            raise ValueError("CategoryScore.score must be between zero and one.")
        object.__setattr__(self, "example_scores", tuple(self.example_scores))

    @property
    def rate(self) -> Decimal | None:
        return self.score

    def as_mapping(self) -> dict[str, object]:
        return {
            "category": self.category,
            "applicable": self.applicable,
            "attempted": self.attempted,
            "passed": self.passed,
            "skipped": self.skipped,
            "score": _json_value(self.score),
            "examples": [item.as_mapping() for item in self.example_scores],
        }


@dataclass(frozen=True, slots=True)
class ArmScore:
    """All per-example and per-category scores for one arm."""

    arm: str
    examples: tuple[ExampleScore, ...]
    categories: Mapping[str, CategoryScore]

    def __post_init__(self) -> None:
        if self.arm not in _ARMS:
            raise ValueError(f"Unknown evaluation arm {self.arm!r}.")
        if any(item.arm != self.arm for item in self.examples):
            raise ValueError("ArmScore contains an example from a different arm.")
        object.__setattr__(self, "examples", tuple(self.examples))
        object.__setattr__(self, "categories", MappingProxyType(dict(self.categories)))

    @property
    def attempted(self) -> int:
        return sum(item.scored for item in self.examples)

    @property
    def skipped(self) -> int:
        return sum(not item.scored for item in self.examples)

    @property
    def passed(self) -> int:
        return sum(item.passed is True for item in self.examples)

    @property
    def misses(self) -> int:
        return self.attempted - self.passed

    @property
    def score(self) -> Decimal | None:
        values = tuple(
            category.score for category in self.categories.values() if category.score is not None
        )
        if not values:
            return None
        return sum(values, Decimal("0")) / Decimal(len(values))

    def as_mapping(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "attempted": self.attempted,
            "skipped": self.skipped,
            "passed": self.passed,
            "misses": self.misses,
            "score": _json_value(self.score),
            "categories": {
                name: category.as_mapping() for name, category in self.categories.items()
            },
            "examples": [item.as_mapping() for item in self.examples],
        }


def _field_precision_recall(
    expected: Mapping[str, object], actual: Mapping[str, object], *, adversarial: bool
) -> dict[str, object]:
    expected_fields = {key for key, value in expected.items() if value is not None}
    actual_fields = {key for key, value in actual.items() if value is not None}
    true_positive = sum(
        key in actual and _values_equal(expected[key], actual[key]) for key in expected_fields
    )
    false_positive = len(actual_fields - expected_fields) + sum(
        key in expected_fields
        and key in actual_fields
        and not _values_equal(expected[key], actual[key])
        for key in expected_fields
    )
    false_negative = len(expected_fields) - true_positive
    precision = (
        Decimal(true_positive) / Decimal(true_positive + false_positive)
        if true_positive + false_positive
        else Decimal("0")
    )
    recall = (
        Decimal(true_positive) / Decimal(len(expected_fields)) if expected_fields else Decimal("1")
    )
    fail_closed = not adversarial or actual.get("refused") is True
    quality = min(precision, recall) if fail_closed else Decimal("0")
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "fail_closed_adversarial": fail_closed,
        "quality": quality,
    }


def _rubric_score(actual: Mapping[str, object]) -> tuple[Decimal, tuple[object, ...]]:
    criteria = actual.get("criteria")
    if not isinstance(criteria, Sequence) or isinstance(criteria, str | bytes | bytearray):
        return Decimal("0"), ()
    satisfied = tuple(
        item for item in criteria if isinstance(item, Mapping) and item.get("satisfied") is True
    )
    return Decimal(len(satisfied)), tuple(criteria)


def score_example(
    example: Mapping[str, object], actual: Mapping[str, object], *, arm: str
) -> ExampleScore:
    """Score one arm result against the example's hand-labelled expectation."""

    if not isinstance(example, Mapping) or not isinstance(actual, Mapping):
        raise TypeError("score_example requires example and actual mappings.")
    example_id, kind, expected = _require_example_fields(example)
    if arm not in _ARMS:
        raise ValueError(f"Unknown evaluation arm {arm!r}.")
    pass_mark = example.get("pass_mark")
    if not isinstance(pass_mark, Mapping) or not isinstance(pass_mark.get("type"), str):
        raise TypeError(f"Evaluation example {example_id!r} requires a pass_mark type.")

    mark_type = pass_mark["type"]
    passed = False
    metric: dict[str, object]
    if mark_type == "exact":
        passed = _values_equal(expected, actual)
        metric = {"type": mark_type, "matched": passed, "quality": Decimal(int(passed))}
    elif mark_type == "field_precision_recall":
        metric = _field_precision_recall(
            expected,
            actual,
            adversarial=bool(example.get("adversarial", False)),
        )
        precision = metric["precision"]
        recall = metric["recall"]
        passed = (
            isinstance(precision, Decimal)
            and isinstance(recall, Decimal)
            and precision >= Decimal(str(pass_mark["precision_floor"]))
            and recall >= Decimal(str(pass_mark["recall_floor"]))
            and bool(metric["fail_closed_adversarial"])
        )
        metric["type"] = mark_type
    elif mark_type == "date_window":
        difference = _decimal(actual.get("difference_days"))
        within = difference is not None and difference <= Decimal(str(pass_mark["window_days"]))
        passed = bool(within)
        metric = {
            "type": mark_type,
            "difference_days": difference,
            "window_days": pass_mark["window_days"],
            "quality": Decimal(int(passed)),
        }
    elif mark_type == "zero_false_escalation":
        escalates = actual.get("escalates")
        passed = escalates is False
        metric = {
            "type": mark_type,
            "escalates": escalates,
            "quality": Decimal(int(passed)),
        }
    elif mark_type == "zero_ungrounded_figures":
        fabricated = actual.get("fabricated_tokens")
        # This pass mark evaluates the detector, including its fail-closed
        # behaviour.  A negative fixture is expected to contain fabricated
        # tokens; treating every negative result as a miss would make the
        # labelled adversarial boundary impossible to score correctly.
        passed = _values_equal(
            expected.get("grounding_passed"), actual.get("grounding_passed")
        ) and _values_equal(expected.get("fabricated_tokens"), fabricated)
        metric = {
            "type": mark_type,
            "fabricated_tokens": fabricated,
            "quality": Decimal(int(passed)),
        }
    elif mark_type == "exact_refusal":
        passed = actual.get("refused") is True and _values_equal(
            expected.get("reasons"), actual.get("reasons")
        )
        metric = {"type": mark_type, "matched": passed, "quality": Decimal(int(passed))}
    elif mark_type == "rubric_floor":
        score, criteria = _rubric_score(actual)
        floor = Decimal(str(pass_mark["floor"]))
        expected_floor_met = expected.get("rubric_floor_met")
        if not isinstance(expected_floor_met, bool):
            raise TypeError(
                f"Evaluation example {example_id!r} must label rubric_floor_met as a boolean."
            )
        passed = (score >= floor) == expected_floor_met
        metric = {
            "type": mark_type,
            "rubric_score": score,
            "floor": floor,
            "reviewers": pass_mark["reviewers"],
            "criteria": criteria,
            "quality": min(score / Decimal("5"), Decimal("1")),
        }
    else:
        raise EvaluationError(f"Unsupported pass mark {mark_type!r} in {example_id}.")

    return ExampleScore(
        example_id=example_id,
        arm=arm,
        kind=kind,
        status="scored",
        passed=passed,
        metric=metric,
        actual=actual,
    )


def skipped_example(example: Mapping[str, object], *, arm: str, reason: str) -> ExampleScore:
    """Create a non-scoring example result with an explicit reason."""

    example_id, kind, _expected = _require_example_fields(example)
    return ExampleScore(
        example_id=example_id,
        arm=arm,
        kind=kind,
        status="skipped",
        passed=None,
        reason=reason,
    )


def score_arm(
    examples: Iterable[Mapping[str, object]],
    example_scores: Iterable[ExampleScore],
    *,
    arm: str,
) -> ArmScore:
    """Aggregate an arm while retaining non-applicable categories as N/A."""

    if arm not in _ARMS:
        raise ValueError(f"Unknown evaluation arm {arm!r}.")
    example_values = tuple(examples)
    scores = tuple(example_scores)
    if any(score.arm != arm for score in scores):
        raise ValueError("score_arm received a score from a different arm.")
    categories: dict[str, CategoryScore] = {}
    all_kinds = sorted(
        {kind for example in example_values if isinstance(kind := example.get("kind"), str)}
    )
    by_kind: dict[str, list[ExampleScore]] = defaultdict(list)
    for score in scores:
        by_kind[score.kind].append(score)
    for kind in all_kinds:
        items = tuple(by_kind.get(kind, ()))
        attempted = sum(item.scored for item in items)
        skipped = sum(not item.scored for item in items)
        passed = sum(item.passed is True for item in items)
        score_value = Decimal(passed) / Decimal(attempted) if attempted else None
        categories[kind] = CategoryScore(
            category=kind,
            applicable=attempted > 0,
            attempted=attempted,
            passed=passed,
            skipped=skipped,
            score=score_value,
            example_scores=items,
        )
    return ArmScore(arm=arm, examples=scores, categories=categories)


__all__ = [
    "ArmScore",
    "CategoryScore",
    "ExampleScore",
    "score_arm",
    "score_example",
    "skipped_example",
]
