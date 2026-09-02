"""Unit coverage for the N-01 evaluation example schema and authored set
(`spec §17.7`, `T-103`).

These tests own two different kinds of proof. The schema, id-uniqueness,
coverage and adversarial-presence checks are about the *authored set as a
whole* — the shape every example must have and the categories the set as a
whole must cover. `test_engine_examples_recompute_exactly` and
`test_examples_reference_labels_not_hardcoded_values` are about individual
examples' *content*: an engine or boundary example's hand-labelled
expectation must be exactly what the real, production domain code produces,
and a reference-portfolio-derived expectation must be resolved against a
fresh regeneration rather than a value copied into the file.

No harness dispatch happens here — that is `T-104`'s job. This module never
imports `evaluation.arms` or `evaluation.run`, neither of which exists yet.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from covenant_radar.domain.covenants.evaluate import (
    CovenantVersionFacts,
    PeriodFacts,
    Thresholds,
    evaluate_covenant,
)
from covenant_radar.domain.covenants.exceptions import WaiverFacts
from covenant_radar.domain.ratios.compute import RatioResult, compute_ratio
from covenant_radar.domain.ratios.definitions import FacilityFacts
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.ratios.reasons import NotComputableReason
from evaluation.reference_portfolio import ReferencePortfolioConfig, generate_reference_portfolio
from evaluation.reference_portfolio.cohorts import ReferenceCohorts, generate_reference_cohorts

pytestmark = pytest.mark.unit

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "evaluation" / "examples"
_SCHEMA_PATH = _EXAMPLES_DIR / "_schema.json"

_ALL_KINDS = frozenset(
    {
        "extraction",
        "engine",
        "boundary",
        "persistence",
        "materiality",
        "forecast_dating",
        "false_escalation",
        "grounding",
        "refusal",
        "usefulness",
    }
)


@dataclass(frozen=True, slots=True)
class ExampleFile:
    """One authored example, paired with the path it was read from."""

    path: Path
    body: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.body["id"])

    @property
    def kind(self) -> str:
        return str(self.body["kind"])


def _example_paths() -> tuple[Path, ...]:
    paths = tuple(sorted(_EXAMPLES_DIR.glob("EX-*.json")))
    assert paths, f"No EX-*.json example files found under {_EXAMPLES_DIR}."
    return paths


def _load_examples() -> tuple[ExampleFile, ...]:
    examples = []
    for path in _example_paths():
        body = json.loads(path.read_text(encoding="utf-8"))
        examples.append(ExampleFile(path=path, body=body))
    return tuple(examples)


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _examples_of(examples: tuple[ExampleFile, ...], kind: str) -> Iterator[ExampleFile]:
    return (example for example in examples if example.kind == kind)


def test_every_file_matches_schema() -> None:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    failures: list[str] = []
    for example in _load_examples():
        errors = sorted(validator.iter_errors(example.body), key=lambda error: list(error.path))
        for error in errors:
            location = "/".join(str(part) for part in error.path) or "<root>"
            failures.append(f"{example.path.name} [{location}]: {error.message}")

    assert not failures, "Schema violation(s):\n" + "\n".join(failures)


def test_ids_unique() -> None:
    examples = _load_examples()
    ids = [example.id for example in examples]
    duplicates = sorted({example_id for example_id in ids if ids.count(example_id) > 1})
    assert not duplicates, f"Duplicate example id(s): {duplicates}"

    mismatched = [
        f"{example.path.name} carries id {example.id!r}"
        for example in examples
        if example.path.stem != example.id
    ]
    assert not mismatched, "Example id must match its filename stem:\n" + "\n".join(mismatched)


def _decimal(value: str) -> Decimal:
    return Decimal(value)


def _engine_expected(example: ExampleFile) -> None:
    body = example.body
    definition_code = body["input"]["definition_code"]
    definition = LIBRARY[definition_code]
    lines = {code: _decimal(value) for code, value in body["input"]["lines"].items()}
    facility_input = body["input"].get("facility")
    facility = (
        FacilityFacts(**{key: _decimal(value) for key, value in facility_input.items()})
        if facility_input
        else None
    )

    result: RatioResult = compute_ratio(definition, lines, facility)
    expected = body["expected"]

    assert result.computable is expected["computable"], example.id
    if result.computable:
        assert result.value is not None
        assert result.value == _decimal(expected["value"]), example.id
        assert result.band_breached is expected["band_breached"], example.id
    else:
        assert result.reason is not None
        assert result.reason.value == expected["reason"], example.id


def _boundary_ratio(ratio_input: dict[str, Any]) -> RatioResult:
    if ratio_input["computable"]:
        return RatioResult(
            code="leverage_ratio",
            value=_decimal(ratio_input["value"]),
            computable=True,
            reason=None,
            inputs_used={},
            band_breached=False,
        )
    return RatioResult(
        code="leverage_ratio",
        value=None,
        computable=False,
        reason=NotComputableReason(ratio_input["reason"]),
        inputs_used={},
        band_breached=False,
    )


def _boundary_expected(example: ExampleFile) -> None:
    body = example.body
    covenant_input = body["input"]["covenant"]
    version = CovenantVersionFacts(
        threshold=_decimal(covenant_input["threshold"]),
        direction=covenant_input["direction"],
        warning_headroom_pct=(
            _decimal(covenant_input["warning_headroom_pct"])
            if "warning_headroom_pct" in covenant_input
            else None
        ),
        cure_days=covenant_input.get("cure_days"),
    )
    ratio = _boundary_ratio(body["input"]["ratio"])
    period_input = body["input"]["period"]
    period = PeriodFacts(
        is_complete=period_input["is_complete"],
        as_of_date=(
            date.fromisoformat(period_input["as_of_date"]) if "as_of_date" in period_input else None
        ),
        last_complete_period=period_input.get("last_complete_period"),
    )
    waiver_input = body["input"].get("waiver")
    waiver = (
        WaiverFacts(
            from_date=date.fromisoformat(waiver_input["from_date"]),
            to_date=(
                date.fromisoformat(waiver_input["to_date"]) if "to_date" in waiver_input else None
            ),
            state=waiver_input["state"],
        )
        if waiver_input is not None
        else None
    )

    evaluation = evaluate_covenant(version, ratio, period, None, waiver, Thresholds())
    expected = body["expected"]

    assert evaluation.verdict == expected["verdict"], example.id
    if "headroom_pct" in expected:
        assert evaluation.headroom_pct == _decimal(expected["headroom_pct"]), example.id
    if "threshold_used" in expected:
        assert evaluation.threshold_used == _decimal(expected["threshold_used"]), example.id
    if "cure_ends_on" in expected:
        assert evaluation.cure_ends_on == date.fromisoformat(expected["cure_ends_on"]), example.id
    if "reason" in expected:
        assert evaluation.reason is not None
        assert evaluation.reason.value == expected["reason"], example.id
    if "stale_reason" in expected:
        assert evaluation.stale_reason == expected["stale_reason"], example.id


def test_engine_examples_recompute_exactly() -> None:
    examples = _load_examples()
    engine_examples = tuple(_examples_of(examples, "engine"))
    boundary_examples = tuple(_examples_of(examples, "boundary"))
    assert engine_examples, "No engine-kind examples authored."
    assert boundary_examples, "No boundary-kind examples authored."

    for example in engine_examples:
        _engine_expected(example)
    for example in boundary_examples:
        _boundary_expected(example)


def _dataset_for(reference_dataset: dict[str, int]) -> ReferenceCohorts:
    portfolio = generate_reference_portfolio(ReferencePortfolioConfig(**reference_dataset))
    return generate_reference_cohorts(portfolio)


def _contains_iso_date(value: Any) -> bool:
    if isinstance(value, str):
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if isinstance(value, dict):
        return any(_contains_iso_date(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_iso_date(item) for item in value)
    return False


def test_examples_reference_labels_not_hardcoded_values() -> None:
    examples = _load_examples()
    flagged = [example for example in examples if example.body["references_reference_portfolio"]]
    assert flagged, (
        "No example declares references_reference_portfolio; forecast dating and "
        "false-escalation examples must be resolved by label."
    )

    dataset_cache: dict[tuple[int, int, int, int], ReferenceCohorts] = {}
    for example in flagged:
        assert example.kind in {"forecast_dating", "false_escalation"}, (
            f"{example.id} sets references_reference_portfolio but is kind "
            f"{example.kind!r}, not forecast_dating or false_escalation."
        )
        assert not _contains_iso_date(example.body["expected"]), (
            f"{example.id} references the reference portfolio but its 'expected' carries a "
            "literal date — it must be resolved by label instead, so a portfolio "
            "regeneration cannot silently invalidate it."
        )

        reference_dataset = example.body["input"]["reference_dataset"]
        cache_key = (
            reference_dataset["seed"],
            reference_dataset["borrower_count"],
            reference_dataset["facility_count"],
            reference_dataset["quarter_count"],
        )
        dataset = dataset_cache.setdefault(cache_key, _dataset_for(reference_dataset))

        borrower_reference = example.body["input"]["borrower_reference"]
        cohort = example.body["input"]["cohort"]
        matching_assignment = next(
            (
                assignment
                for assignment in dataset.assignments
                if assignment.borrower_reference == borrower_reference
            ),
            None,
        )
        assert matching_assignment is not None, (
            f"{example.id} names borrower_reference {borrower_reference!r}, which does not "
            "exist in a fresh regeneration of its declared reference_dataset."
        )
        assert matching_assignment.cohort == cohort, (
            f"{example.id} claims cohort {cohort!r} for {borrower_reference!r}, but a fresh "
            f"regeneration assigns it to {matching_assignment.cohort!r}."
        )
        if example.kind == "forecast_dating":
            label = dataset.labels.by_borrower.get(matching_assignment.borrower_id)
            assert label is not None, (
                f"{example.id} names a deteriorating borrower with no derived outcome label "
                "in a fresh regeneration — derive_labels is the only permitted source of its "
                "breach date, and it produced none."
            )


def test_coverage_across_every_category() -> None:
    examples = _load_examples()
    covered = {example.kind for example in examples}
    missing = _ALL_KINDS - covered
    assert not missing, f"No authored example covers kind(s): {sorted(missing)}"


def test_adversarial_extraction_cases_present() -> None:
    examples = _load_examples()
    adversarial_extraction = [
        example
        for example in examples
        if example.kind == "extraction" and example.body["adversarial"]
    ]
    assert adversarial_extraction, "No adversarial extraction example is authored."

    tags_present = {tag for example in adversarial_extraction for tag in example.body["tags"]}
    assert "redirection" in tags_present, "No adversarial extraction example attempts redirection."
    assert "implausible_threshold" in tags_present, (
        "No adversarial extraction example carries an implausible threshold."
    )
    assert all(example.body["expected"]["refused"] for example in adversarial_extraction), (
        "Every adversarial extraction example must be fail-closed (expected.refused is true), "
        "per spec §17.7's 100% fail-closed pass mark."
    )
