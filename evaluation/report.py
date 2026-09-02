"""Two-arm evaluation scoreboard and durable report formatting."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Final

from evaluation.score import ArmScore, CategoryScore, ExampleScore

_CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "engine",
    "boundary",
    "persistence",
    "materiality",
    "forecast_dating",
    "false_escalation",
    "extraction",
    "grounding",
    "refusal",
    "usefulness",
)


def _number(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class CategoryGap:
    """The product-minus-baseline score for one category."""

    category: str
    product: Decimal | None
    baseline: Decimal | None
    gap: Decimal | None

    @property
    def baseline_outscores_product(self) -> bool:
        return (
            self.product is not None and self.baseline is not None and self.baseline > self.product
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "category": self.category,
            "product": _json_value(self.product),
            "baseline": _json_value(self.baseline),
            "gap": _json_value(self.gap),
            "baseline_outscores_product": self.baseline_outscores_product,
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """A complete side-by-side scoreboard for one two-arm run."""

    product: ArmScore
    baseline: ArmScore
    gaps: Mapping[str, CategoryGap]
    commit_reference: str | None = None

    def __post_init__(self) -> None:
        if self.product.arm != "product" or self.baseline.arm != "baseline":
            raise ValueError("EvaluationReport requires product and baseline arm scores.")
        object.__setattr__(self, "gaps", MappingProxyType(dict(self.gaps)))

    @property
    def baseline_wins(self) -> tuple[CategoryGap, ...]:
        return tuple(gap for gap in self.gaps.values() if gap.baseline_outscores_product)

    def as_mapping(self) -> dict[str, object]:
        return {
            "commit_reference": self.commit_reference,
            "product": self.product.as_mapping(),
            "baseline": self.baseline.as_mapping(),
            "gaps": {name: gap.as_mapping() for name, gap in self.gaps.items()},
        }

    def render(self) -> str:
        return render_scoreboard(self)


def build_report(
    product: ArmScore, baseline: ArmScore, *, commit_reference: str | None = None
) -> EvaluationReport:
    """Build category gaps without converting N/A into a numeric zero."""

    if product.arm != "product" or baseline.arm != "baseline":
        raise ValueError("build_report requires one product and one baseline score.")
    categories = tuple(
        name
        for name in _CATEGORY_ORDER
        if name in product.categories or name in baseline.categories
    )
    categories += tuple(
        name
        for name in sorted(set(product.categories) | set(baseline.categories))
        if name not in categories
    )
    gaps: dict[str, CategoryGap] = {}
    for name in categories:
        product_score = _category_score(product.categories.get(name))
        baseline_score = _category_score(baseline.categories.get(name))
        gap = (
            product_score - baseline_score
            if product_score is not None and baseline_score is not None
            else None
        )
        gaps[name] = CategoryGap(name, product_score, baseline_score, gap)
    return EvaluationReport(
        product=product,
        baseline=baseline,
        gaps=gaps,
        commit_reference=commit_reference,
    )


def _category_score(category: CategoryScore | None) -> Decimal | None:
    if category is None or not category.applicable:
        return None
    return category.score


def render_arm_table(arm: ArmScore) -> str:
    """Render a compact one-arm table used by the product-only command."""

    lines = [f"ARM {arm.arm}", "EXAMPLE       CATEGORY             STATUS   RESULT"]
    for item in arm.examples:
        result = "PASS" if item.passed is True else "MISS" if item.scored else "SKIP"
        detail = "" if item.scored else f" ({item.reason})"
        lines.append(f"{item.example_id:<13}{item.kind:<21}{item.status:<9}{result}{detail}")
    lines.append(
        f"SUMMARY {arm.arm}: {arm.attempted} scored, {arm.skipped} skipped, "
        f"{arm.passed} passed, {arm.misses} misses"
    )
    return "\n".join(lines)


def _example_result(item: ExampleScore | None) -> str:
    if item is None:
        return "N/A"
    if item.status == "skipped":
        return "SKIP"
    return "PASS" if item.passed is True else "MISS"


def render_scoreboard(report: EvaluationReport) -> str:
    """Render both arms per example and per category, with baseline wins visible."""

    lines = ["COVENANT RADAR EVALUATION SCOREBOARD"]
    if report.commit_reference:
        lines.append(f"COMMIT {report.commit_reference}")
    product_by_id = {item.example_id: item for item in report.product.examples}
    baseline_by_id = {item.example_id: item for item in report.baseline.examples}
    example_ids = tuple(dict.fromkeys((*product_by_id, *baseline_by_id)))
    lines.extend(
        (
            "EXAMPLE       CATEGORY             PRODUCT   BASELINE",
            "-" * 60,
        )
    )
    for example_id in example_ids:
        product_item = product_by_id.get(example_id)
        baseline_item = baseline_by_id.get(example_id)
        if product_item is not None:
            kind = product_item.kind
        elif baseline_item is not None:
            kind = baseline_item.kind
        else:  # pragma: no cover - the id came from one of the two mappings
            continue
        lines.append(
            f"{example_id:<13}{kind:<21}{_example_result(product_item):>8}  "
            f"{_example_result(baseline_item):>8}"
        )
    lines.append("")
    lines.extend(
        (
            "CATEGORY             PRODUCT   BASELINE  GAP       STATUS",
            "-" * 70,
        )
    )
    for gap in report.gaps.values():
        status = "N/A" if gap.gap is None else "compared"
        lines.append(
            f"{gap.category:<21}{_number(gap.product):>8}  {_number(gap.baseline):>8}  "
            f"{_number(gap.gap):>8}  {status}"
        )
    lines.append(
        f"SUMMARY product: {report.product.attempted} scored, {report.product.skipped} skipped, "
        f"{report.product.passed} passed, {report.product.misses} misses"
    )
    lines.append(
        f"SUMMARY baseline: {report.baseline.attempted} scored, {report.baseline.skipped} skipped, "
        f"{report.baseline.passed} passed, {report.baseline.misses} misses"
    )
    if report.baseline_wins:
        for gap in report.baseline_wins:
            lines.append(
                f"BASELINE OUTSCORES PRODUCT: {gap.category} "
                f"({_number(gap.baseline)} vs {_number(gap.product)})"
            )
    else:
        lines.append("BASELINE OUTSCORES PRODUCT: none")
    return "\n".join(lines)


def persist_report(report: EvaluationReport, path: Path | str) -> Path:
    """Atomically store a JSON scoreboard, creating no partial report."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        _json_value(report.as_mapping()), ensure_ascii=False, sort_keys=True, indent=2
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return destination


__all__ = [
    "CategoryGap",
    "EvaluationReport",
    "build_report",
    "persist_report",
    "render_arm_table",
    "render_scoreboard",
]
