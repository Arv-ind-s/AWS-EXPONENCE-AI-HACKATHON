"""Regression checks for the recorded reference-portfolio calibration.

The calibration record is deliberately kept in the documentation owned by
T-065.  This suite treats that record as an input, reconstructs the selected
settings, and reruns the same deterministic domain stages used by the
evaluation product arm.  A changed record, threshold file, or forecast rule
therefore fails with a useful mismatch instead of silently making the
calibration stale.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from covenant_radar.config.thresholds import DEFAULT_THRESHOLD_PATH, ThresholdStore
from covenant_radar.domain.forecast import (
    Direction,
    Observation,
    Weights,
    evidence_pressure,
    first_crossing,
    probability,
    project,
)
from covenant_radar.domain.forecast.attribution import attribute
from covenant_radar.domain.signals.materiality import CovenantHeadroom, score_materiality
from covenant_radar.domain.signals.persistence import score_persistence
from covenant_radar.domain.triage.banding import band
from evaluation.reference_portfolio import ReferencePortfolioConfig, generate_reference_portfolio
from evaluation.reference_portfolio.cohorts import (
    NOISY_TRANSIENT_COHORT,
    STABLE_COHORT,
    ReferenceCohorts,
    generate_reference_cohorts,
)
from evaluation.reference_portfolio.signals import SignalEventRecord

pytestmark = pytest.mark.integration

_CALIBRATION_DOCUMENT = (
    Path(__file__).resolve().parents[2] / "docs" / "calibration" / "reference-portfolio.md"
)
_CALIBRATION_RECORD_TYPE = "covenant_radar_threshold_calibration"
_CALIBRATED_THRESHOLDS = ("T1", "T3", "T4", "T5")
_WARNING_HORIZON_DAYS = 90
_REPORT_QUANTUM = Decimal("0.000001")
_PROBABILITY_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True, slots=True)
class _CalibrationConfiguration:
    thresholds: Mapping[str, Mapping[str, object]]
    weights: Weights
    covenant_threshold: Decimal


@dataclass(frozen=True, slots=True)
class _DailyAssessment:
    warning: bool
    probability: Decimal | None
    attribution_total: Decimal | None


@dataclass(frozen=True, slots=True)
class _CalibrationRun:
    record: Mapping[str, object]
    dataset: ReferenceCohorts
    thresholds: Mapping[str, Mapping[str, object]]
    assessments: Mapping[UUID, tuple[_DailyAssessment, ...]]
    daily_warning_counts: tuple[int, ...]
    scores: Mapping[str, object]


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object.")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be non-blank text.")
    return value.strip()


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TypeError(f"{field_name} must be a positive integer.")
    return value


def _decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a decimal string.")
    try:
        result = Decimal(value)
    except ArithmeticError as error:
        raise TypeError(f"{field_name} must be a finite decimal string.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    return result


def _load_record() -> Mapping[str, object]:
    text = _CALIBRATION_DOCUMENT.read_text(encoding="utf-8")
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    records = []
    for raw in matches:
        value = json.loads(raw)
        if isinstance(value, Mapping) and value.get("record_type") == _CALIBRATION_RECORD_TYPE:
            records.append(value)
    if len(records) != 1:
        raise AssertionError(
            f"Expected exactly one {_CALIBRATION_RECORD_TYPE} JSON block in "
            f"{_CALIBRATION_DOCUMENT}, found {len(records)}."
        )
    return cast(Mapping[str, object], records[0])


def _configuration(record: Mapping[str, object]) -> _CalibrationConfiguration:
    selected = _mapping(record.get("selected"), "selected")
    raw_thresholds = _mapping(selected.get("thresholds"), "selected.thresholds")
    thresholds: dict[str, Mapping[str, object]] = {}
    for name in _CALIBRATED_THRESHOLDS:
        section = _mapping(raw_thresholds.get(name), f"selected.thresholds.{name}")
        thresholds[name] = {
            str(field): (
                _integer(value, f"selected.thresholds.{name}.{field}")
                if name == "T3"
                else _decimal(value, f"selected.thresholds.{name}.{field}")
            )
            for field, value in section.items()
        }

    raw_weights = _mapping(selected.get("weights"), "selected.weights")
    weights = Weights.from_mapping(
        {
            field: _decimal(raw_weights.get(field), f"selected.weights.{field}")
            for field in ("distance", "velocity", "pressure", "max_probability")
        }
    )
    covenant_threshold = _decimal(
        _mapping(record.get("dataset"), "dataset").get("covenant_threshold"),
        "dataset.covenant_threshold",
    )
    return _CalibrationConfiguration(
        thresholds=thresholds,
        weights=weights,
        covenant_threshold=covenant_threshold,
    )


def _reference_dataset(record: Mapping[str, object]) -> ReferenceCohorts:
    dataset_config = _mapping(record.get("dataset"), "dataset")
    config = ReferencePortfolioConfig(
        seed=_integer(dataset_config.get("seed"), "dataset.seed"),
        borrower_count=_integer(dataset_config.get("borrower_count"), "dataset.borrower_count"),
        facility_count=_integer(dataset_config.get("facility_count"), "dataset.facility_count"),
        quarter_count=_integer(dataset_config.get("quarter_count"), "dataset.quarter_count"),
    )
    return generate_reference_cohorts(
        generate_reference_portfolio(config),
        authored_cohort_size=_integer(
            dataset_config.get("authored_cohort_size"), "dataset.authored_cohort_size"
        ),
        signal_days=_integer(dataset_config.get("signal_days"), "dataset.signal_days"),
    )


def _effective_thresholds(
    store: ThresholdStore, configuration: _CalibrationConfiguration
) -> Mapping[str, Mapping[str, object]]:
    active = dict(store.values())
    for name, section in configuration.thresholds.items():
        active[name] = dict(section)
    return active


def _observations(events: tuple[SignalEventRecord, ...]) -> tuple[Observation, ...]:
    return tuple(
        Observation(
            observed_on=event.event_date,
            value=event.magnitude,
            source_id=event.id,
        )
        for event in events
    )


def _assessment_series(
    borrower_events: tuple[SignalEventRecord, ...],
    *,
    cohort: str,
    thresholds: Mapping[str, Mapping[str, object]],
    weights: Weights,
    covenant_threshold: Decimal,
) -> tuple[_DailyAssessment, ...]:
    assessments: list[_DailyAssessment] = []
    for index in range(len(borrower_events)):
        history = borrower_events[: index + 1]
        as_of = history[-1].event_date
        observations = _observations(history)
        persistence = score_persistence(
            (event.event_date for event in history if event.is_adverse),
            as_of,
            {"T3": thresholds["T3"]},
        )
        current = history[-1].magnitude
        projected_for_materiality = current + Decimal("5") if persistence.sustained else current
        materiality = score_materiality(
            (
                CovenantHeadroom(
                    covenant_id="utilisation",
                    threshold=covenant_threshold,
                    current_value=current,
                    projected_value_90d=projected_for_materiality,
                    direction="max",
                ),
            ),
            {"T4": thresholds["T4"]},
        )
        pressure = evidence_pressure(
            (
                {
                    "id": "utilisation",
                    "state": "sustained" if persistence.sustained else "transient",
                    "counts_toward_pressure": materiality.counts_toward_pressure,
                    "materiality_pct": materiality.materiality_pct,
                    "decay_factor": Decimal("1"),
                },
            ),
            Direction.MAX,
        )
        projection = project(
            observations,
            pressure,
            _WARNING_HORIZON_DAYS,
            covenant_threshold,
            Direction.MAX,
            recent_periods=90,
        )
        crossing = first_crossing(projection, as_of_date=as_of)
        warning = bool(crossing.crossed and persistence.sustained)

        probability_value: Decimal | None = None
        attribution_total: Decimal | None = None
        if cohort == STABLE_COHORT and index == len(borrower_events) - 1:
            endpoint = projection.path[-1].value
            if endpoint is None:
                raise AssertionError("Stable reference trajectory has no forecast endpoint.")
            result = probability(
                covenant_threshold - endpoint,
                projection.net_per_day_drift,
                projection.pressure,
                _WARNING_HORIZON_DAYS,
                weights,
            )
            probability_value = result.probability
            shares = attribute(
                {term.name: term.contribution for term in result.terms},
                thresholds["T5"],
            )
            attribution_total = sum((share.share for share in shares), Decimal("0"))
        assessments.append(
            _DailyAssessment(
                warning=warning,
                probability=probability_value,
                attribution_total=attribution_total,
            )
        )
    return tuple(assessments)


def _report_decimal(value: Decimal) -> str:
    return format(value.quantize(_REPORT_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _probability_report(value: Decimal) -> str:
    return format(value.quantize(_PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _score_run(
    dataset: ReferenceCohorts,
    assessments: Mapping[UUID, tuple[_DailyAssessment, ...]],
    thresholds: Mapping[str, Mapping[str, object]],
) -> tuple[Mapping[str, object], tuple[int, ...]]:
    labels = dataset.labels.by_borrower
    leads: list[int] = []
    for borrower in dataset.borrowers:
        label = labels.get(borrower.id)
        if label is None:
            continue
        events = tuple(dataset.signals.events_for_borrower(borrower.id, family="utilisation"))
        first_warning = next(
            (
                event.event_date
                for event, assessment in zip(events, assessments[borrower.id], strict=True)
                if event.event_date < label.breach_date and assessment.warning
            ),
            None,
        )
        if first_warning is None:
            raise AssertionError(f"No warning was produced for {borrower.reference}.")
        leads.append((label.breach_date - first_warning).days)

    scoring_days = len(
        tuple(dataset.signals.events_for_borrower(dataset.borrowers[0].id, family="utilisation"))
    )
    daily_warning_counts = tuple(
        sum(assessments[borrower.id][day].warning for borrower in dataset.borrowers)
        for day in range(scoring_days)
    )
    false_by_cohort: dict[str, Decimal] = {}
    false_count_total = 0
    false_borrower_total = 0
    for cohort in (NOISY_TRANSIENT_COHORT, STABLE_COHORT):
        cohort_borrowers = tuple(
            borrower for borrower in dataset.borrowers if borrower.cohort == cohort
        )
        false_count = sum(
            any(assessment.warning for assessment in assessments[borrower.id])
            for borrower in cohort_borrowers
        )
        false_by_cohort[cohort] = Decimal(false_count) / Decimal(len(cohort_borrowers))
        false_count_total += false_count
        false_borrower_total += len(cohort_borrowers)

    stable_probabilities = tuple(
        assessment.probability
        for borrower in dataset.borrowers
        if borrower.cohort == STABLE_COHORT
        for assessment in (assessments[borrower.id][-1],)
        if assessment.probability is not None
    )
    if not stable_probabilities:
        raise AssertionError("Stable cohort has no final probabilities.")
    stable_band = tuple(
        band(
            probability_value,
            {"T1": thresholds["T1"], "T2": thresholds["T2"]},
        )
        for probability_value in stable_probabilities
    )
    stable_below_amber = stable_band == ("watch",) * len(stable_band)
    scores: Mapping[str, object] = {
        "g1_flagged_30_rate": _report_decimal(
            Decimal(sum(lead >= 30 for lead in leads)) / Decimal(len(leads))
        ),
        "g1_flagged_60_rate": _report_decimal(
            Decimal(sum(lead >= 60 for lead in leads)) / Decimal(len(leads))
        ),
        "g1_lead_days": leads,
        "g3_max_escalation_share": _report_decimal(
            Decimal(max(daily_warning_counts)) / Decimal(len(dataset.borrowers))
        ),
        "g3_max_escalation_count": max(daily_warning_counts),
        "g3_false_escalation_rate": _report_decimal(
            Decimal(false_count_total) / Decimal(false_borrower_total)
        ),
        "g3_noisy_false_escalation_rate": _report_decimal(false_by_cohort[NOISY_TRANSIENT_COHORT]),
        "g3_stable_false_escalation_rate": _report_decimal(false_by_cohort[STABLE_COHORT]),
        "stable_latest_max_probability": _probability_report(max(stable_probabilities)),
        "stable_latest_below_amber": stable_below_amber,
    }
    return scores, daily_warning_counts


@pytest.fixture(scope="module")
def calibration_run() -> _CalibrationRun:
    record = _load_record()
    configuration = _configuration(record)
    store = ThresholdStore(path=DEFAULT_THRESHOLD_PATH)
    for name in _CALIBRATED_THRESHOLDS:
        expected = configuration.thresholds[name]
        actual = store.get(name)
        assert actual == expected, (
            f"Packaged {name} differs from the selected calibration settings: "
            f"expected {expected}, found {actual}."
        )
    thresholds = _effective_thresholds(store, configuration)
    dataset = _reference_dataset(record)
    assessments = _build_assessments(dataset, thresholds, configuration)
    scores, daily_warning_counts = _score_run(dataset, assessments, thresholds)
    return _CalibrationRun(
        record=record,
        dataset=dataset,
        thresholds=thresholds,
        assessments=assessments,
        daily_warning_counts=daily_warning_counts,
        scores=scores,
    )


def _build_assessments(
    dataset: ReferenceCohorts,
    thresholds: Mapping[str, Mapping[str, object]],
    configuration: _CalibrationConfiguration,
) -> dict[UUID, tuple[_DailyAssessment, ...]]:
    assessments: dict[UUID, tuple[_DailyAssessment, ...]] = {}
    for borrower in dataset.borrowers:
        events = tuple(dataset.signals.events_for_borrower(borrower.id, family="utilisation"))
        assessments[borrower.id] = _assessment_series(
            events,
            cohort=borrower.cohort,
            thresholds=thresholds,
            weights=configuration.weights,
            covenant_threshold=configuration.covenant_threshold,
        )
    return assessments


def _targets(run: _CalibrationRun) -> Mapping[str, Decimal]:
    targets = _mapping(run.record.get("acceptance"), "acceptance")
    return {
        name: _decimal(targets.get(name), f"acceptance.{name}")
        for name in (
            "g1_flagged_30_rate",
            "g1_flagged_60_rate",
            "g3_max_escalation_share",
            "g3_false_escalation_rate",
        )
    }


def test_deteriorating_cohort_lead_time_meets_g1(calibration_run: _CalibrationRun) -> None:
    targets = _targets(calibration_run)
    assert (
        Decimal(str(calibration_run.scores["g1_flagged_30_rate"])) >= targets["g1_flagged_30_rate"]
    )
    assert (
        Decimal(str(calibration_run.scores["g1_flagged_60_rate"])) >= targets["g1_flagged_60_rate"]
    )
    assert calibration_run.scores["g1_lead_days"] == [156, 156]


def test_noisy_cohort_escalation_within_g3(calibration_run: _CalibrationRun) -> None:
    targets = _targets(calibration_run)
    assert (
        Decimal(str(calibration_run.scores["g3_noisy_false_escalation_rate"]))
        <= targets["g3_false_escalation_rate"]
    )


def test_stable_cohort_below_amber(calibration_run: _CalibrationRun) -> None:
    assert calibration_run.scores["stable_latest_below_amber"] is True
    assert calibration_run.scores["stable_latest_max_probability"] == "0.017287133838"
    stable_assessments = [
        calibration_run.assessments[borrower.id][-1]
        for borrower in calibration_run.dataset.borrowers
        if borrower.cohort == STABLE_COHORT
    ]
    assert all(assessment.attribution_total == Decimal("1") for assessment in stable_assessments)


def test_amber_share_within_g3_on_every_day(calibration_run: _CalibrationRun) -> None:
    target = _targets(calibration_run)["g3_max_escalation_share"]
    portfolio_size = Decimal(len(calibration_run.dataset.borrowers))
    assert len(calibration_run.daily_warning_counts) == 365
    assert all(
        Decimal(count) / portfolio_size <= target for count in calibration_run.daily_warning_counts
    )
    assert calibration_run.scores["g3_max_escalation_share"] == "0.083333"
    assert calibration_run.scores["g3_max_escalation_count"] == 2


def test_calibration_reproducible_from_record(calibration_run: _CalibrationRun) -> None:
    expected = _mapping(calibration_run.record.get("final_scores"), "final_scores")
    assert dict(calibration_run.scores) == dict(expected)

    iterations = calibration_run.record.get("iterations")
    assert isinstance(iterations, list) and len(iterations) == 2
    baseline = _mapping(iterations[0], "iterations[0]")
    candidate = _mapping(iterations[1], "iterations[1]")
    assert _mapping(baseline.get("scores"), "iterations[0].scores") == expected
    assert _text(candidate.get("decision"), "iterations[1].decision").startswith("rejected")
    changes = _mapping(candidate.get("changes"), "iterations[1].changes")
    assert set(changes) == {"T3.sustained_events"}
    change = _mapping(changes["T3.sustained_events"], "iterations[1].changes.T3.sustained_events")
    assert change == {"before": 3, "after": 1}
    candidate_scores = _mapping(candidate.get("scores"), "iterations[1].scores")
    configuration = _configuration(calibration_run.record)
    candidate_thresholds = {
        name: dict(section) for name, section in calibration_run.thresholds.items()
    }
    candidate_thresholds["T3"] = {
        **candidate_thresholds["T3"],
        "sustained_events": 1,
    }
    candidate_assessments = _build_assessments(
        calibration_run.dataset,
        candidate_thresholds,
        configuration,
    )
    recomputed_candidate_scores, _ = _score_run(
        calibration_run.dataset,
        candidate_assessments,
        candidate_thresholds,
    )
    assert dict(recomputed_candidate_scores) == dict(candidate_scores)
    assert (
        Decimal(str(candidate_scores["g3_max_escalation_share"]))
        > _targets(calibration_run)["g3_max_escalation_share"]
    )
    assert (
        Decimal(str(candidate_scores["g3_false_escalation_rate"]))
        > _targets(calibration_run)["g3_false_escalation_rate"]
    )

    encoded = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    assert digest == _text(calibration_run.record.get("final_scores_sha256"), "final_scores_sha256")
