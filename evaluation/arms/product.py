"""The production evaluation arm.

This arm deliberately calls the same deterministic functions used by the
application.  Model stages are routed only through the recorded provider; a
missing cassette is an evaluation skip and can never fall through to a live
network call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Final

from covenant_radar.ai.budget import BudgetLimits
from covenant_radar.ai.client import InMemoryModelCallWriter, ModelClient
from covenant_radar.ai.errors import ProviderUnavailable
from covenant_radar.ai.intake import propose_candidates
from covenant_radar.ai.providers.recorded import RecordedProvider
from covenant_radar.ai.shapes import check_stage7_shapes, extract_numeric_tokens, scan_for_injection
from covenant_radar.core.clock import FixedClock
from covenant_radar.domain.covenants.evaluate import (
    CovenantVersionFacts,
    PeriodFacts,
    Thresholds,
    evaluate_covenant,
)
from covenant_radar.domain.covenants.exceptions import WaiverFacts
from covenant_radar.domain.forecast import (
    Direction,
    Observation,
    evidence_pressure,
    first_crossing,
    project,
)
from covenant_radar.domain.intake.candidates import CandidateLine, CandidatePage, detect_candidates
from covenant_radar.domain.intake.proposal import StageOneProposal
from covenant_radar.domain.memo.slots import MemoRecord, MemoRecords, MemoSlotMap, RecordReference
from covenant_radar.domain.ratios.compute import RatioResult, compute_ratio
from covenant_radar.domain.ratios.definitions import FacilityFacts
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.ratios.reasons import NotComputableReason
from covenant_radar.domain.signals.materiality import CovenantHeadroom, score_materiality
from covenant_radar.domain.signals.persistence import score_persistence
from evaluation import DEFAULT_CASSETTES_DIR, EvaluationError, EvaluationSkip
from evaluation.reference_portfolio import ReferencePortfolioConfig, generate_reference_portfolio
from evaluation.reference_portfolio.cohorts import (
    BorrowerWithCohort,
    ReferenceCohorts,
    generate_reference_cohorts,
)

_EVALUATION_CLOCK: Final[datetime] = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
_REFERENCE_THRESHOLD: Final[Decimal] = Decimal("85")
_FORECAST_HORIZON_DAYS: Final[int] = 90
_T3_THRESHOLDS: Final[Mapping[str, Mapping[str, int]]] = {
    "T3": {
        "sustained_days": 14,
        "sustained_events": 3,
        "event_window_days": 30,
    }
}
_T4_THRESHOLDS: Final[Mapping[str, Mapping[str, Decimal]]] = {
    "T4": {"headroom_erosion_pct": Decimal("0.05")}
}
_ISO_DATE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GENERIC_DRIVER_NAMES: Final[frozenset[str]] = frozenset(
    {"general deterioration", "deterioration", "not available from the recorded evidence"}
)


class ProductArmError(EvaluationError):
    """The product arm could not produce a trustworthy result."""


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProductArmError(f"{field_name} must be a finite decimal value.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProductArmError(f"{field_name} must be a finite decimal value.") from error
    if not result.is_finite():
        raise ProductArmError(f"{field_name} must be a finite decimal value.")
    return result


def _date(value: object, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ProductArmError(f"{field_name} must be an ISO date.") from error
    raise ProductArmError(f"{field_name} must be an ISO date.")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProductArmError(f"{field_name} must be an object.")
    return value


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductArmError(f"{field_name} must be an integer.")
    return value


def _engine(example_input: Mapping[str, object]) -> Mapping[str, object]:
    code = example_input.get("definition_code")
    if not isinstance(code, str) or code not in LIBRARY:
        raise ProductArmError(f"Unknown ratio definition {code!r}.")
    lines_input = _mapping(example_input.get("lines"), "input.lines")
    lines = {name: _decimal(value, f"input.lines[{name!r}]") for name, value in lines_input.items()}
    facility_input = example_input.get("facility")
    facility = None
    if facility_input is not None:
        facility_values = _mapping(facility_input, "input.facility")
        facility = FacilityFacts(
            **{
                name: _decimal(value, f"input.facility[{name!r}]")
                for name, value in facility_values.items()
            }
        )
    result = compute_ratio(LIBRARY[code], lines, facility)
    output: dict[str, object] = {"computable": result.computable}
    if result.computable:
        output["value"] = format(result.value, "f") if result.value is not None else None
        output["band_breached"] = result.band_breached
    else:
        output["reason"] = result.reason.value if result.reason is not None else None
    return output


def _boundary_ratio(value: Mapping[str, object]) -> RatioResult:
    computable = value.get("computable")
    if not isinstance(computable, bool):
        raise ProductArmError("input.ratio.computable must be boolean.")
    if computable:
        return RatioResult(
            code="leverage_ratio",
            value=_decimal(value.get("value"), "input.ratio.value"),
            computable=True,
            reason=None,
            inputs_used={},
            band_breached=False,
        )
    reason = value.get("reason")
    try:
        parsed_reason = NotComputableReason(reason)
    except (TypeError, ValueError) as error:
        raise ProductArmError(f"Unknown ratio reason {reason!r}.") from error
    return RatioResult(
        code="leverage_ratio",
        value=None,
        computable=False,
        reason=parsed_reason,
        inputs_used={},
        band_breached=False,
    )


def _boundary(example_input: Mapping[str, object]) -> Mapping[str, object]:
    covenant_input = _mapping(example_input.get("covenant"), "input.covenant")
    period_input = _mapping(example_input.get("period"), "input.period")
    version = CovenantVersionFacts(
        threshold=_decimal(covenant_input.get("threshold"), "input.covenant.threshold"),
        direction=str(covenant_input.get("direction", "")),
        warning_headroom_pct=(
            _decimal(covenant_input["warning_headroom_pct"], "input.covenant.warning_headroom_pct")
            if "warning_headroom_pct" in covenant_input
            else None
        ),
        cure_days=covenant_input.get("cure_days"),
    )
    period = PeriodFacts(
        is_complete=period_input.get("is_complete") is True,
        as_of_date=(
            _date(period_input["as_of_date"], "input.period.as_of_date")
            if "as_of_date" in period_input
            else None
        ),
        last_complete_period=period_input.get("last_complete_period"),
    )
    ratio = _boundary_ratio(_mapping(example_input.get("ratio"), "input.ratio"))
    waiver_input = example_input.get("waiver")
    waiver = None
    if waiver_input is not None:
        raw_waiver = _mapping(waiver_input, "input.waiver")
        from_date = _date(raw_waiver.get("from_date"), "input.waiver.from_date")
        to_date = (
            _date(raw_waiver["to_date"], "input.waiver.to_date")
            if "to_date" in raw_waiver
            else None
        )
        test_date = period.test_date
        if (
            raw_waiver.get("state") == "approved"
            and test_date is not None
            and from_date <= test_date
            and (to_date is None or test_date <= to_date)
        ):
            waiver = WaiverFacts(
                from_date=from_date,
                to_date=to_date,
                state="approved",
            )
    evaluation = evaluate_covenant(version, ratio, period, None, waiver, Thresholds())
    output: dict[str, object] = {
        "verdict": evaluation.verdict,
        "threshold_used": (
            format(evaluation.threshold_used, "f")
            if evaluation.threshold_used is not None
            else None
        ),
    }
    if evaluation.headroom_pct is not None:
        output["headroom_pct"] = format(evaluation.headroom_pct, "f")
    if evaluation.cure_ends_on is not None:
        output["cure_ends_on"] = evaluation.cure_ends_on.isoformat()
    if evaluation.reason is not None:
        output["reason"] = evaluation.reason.value
    if evaluation.stale_reason is not None:
        output["stale_reason"] = evaluation.stale_reason
    return output


def _persistence(example_input: Mapping[str, object]) -> Mapping[str, object]:
    events_input = example_input.get("events")
    if not isinstance(events_input, Sequence) or isinstance(events_input, str | bytes | bytearray):
        raise ProductArmError("input.events must be an array.")
    events = [_date(value, "input.events[]") for value in events_input]
    thresholds = _mapping(example_input.get("thresholds"), "input.thresholds")
    result = score_persistence(
        events,
        _date(example_input.get("as_of"), "input.as_of"),
        {"T3": dict(thresholds)},
    )
    return {
        "sustained": result.sustained,
        "firing_arm": result.firing_arm.value if result.firing_arm is not None else None,
        "persistence_days": result.persistence_days,
        "event_count_window": result.event_count_window,
    }


def _materiality(example_input: Mapping[str, object]) -> Mapping[str, object]:
    raw_headrooms = example_input.get("covenant_headrooms")
    if not isinstance(raw_headrooms, Sequence) or isinstance(
        raw_headrooms, str | bytes | bytearray
    ):
        raise ProductArmError("input.covenant_headrooms must be an array.")
    headrooms = []
    for index, raw in enumerate(raw_headrooms):
        value = _mapping(raw, f"input.covenant_headrooms[{index}]")
        headrooms.append(
            CovenantHeadroom(
                covenant_id=str(value.get("covenant_id", "")),
                threshold=_decimal(value.get("threshold"), "covenant threshold"),
                current_headroom_pct=(
                    _decimal(value["current_headroom_pct"], "current headroom")
                    if "current_headroom_pct" in value
                    else None
                ),
                projected_headroom_pct=(
                    _decimal(value["projected_headroom_pct"], "projected headroom")
                    if "projected_headroom_pct" in value
                    else None
                ),
            )
        )
    thresholds = _mapping(example_input.get("thresholds"), "input.thresholds")
    result = score_materiality(
        headrooms,
        {
            "T4": {
                "headroom_erosion_pct": _decimal(
                    thresholds.get("headroom_erosion_pct"),
                    "input.thresholds.headroom_erosion_pct",
                )
            }
        },
    )
    return {
        "materiality_pct": format(result.materiality_pct, "f"),
        "counts_toward_pressure": result.counts_toward_pressure,
        "driving_covenant_id": result.driving_covenant_id,
    }


@lru_cache(maxsize=8)
def reference_dataset(
    seed: int, borrower_count: int, facility_count: int, quarter_count: int
) -> ReferenceCohorts:
    """Build one immutable reference dataset per declared configuration."""

    config = ReferencePortfolioConfig(
        seed=seed,
        borrower_count=borrower_count,
        facility_count=facility_count,
        quarter_count=quarter_count,
    )
    return generate_reference_cohorts(generate_reference_portfolio(config))


def _reference_context(
    example_input: Mapping[str, object],
) -> tuple[ReferenceCohorts, BorrowerWithCohort, date]:
    raw_dataset = _mapping(example_input.get("reference_dataset"), "input.reference_dataset")
    dataset = reference_dataset(
        _integer(raw_dataset.get("seed"), "input.reference_dataset.seed"),
        _integer(raw_dataset.get("borrower_count"), "input.reference_dataset.borrower_count"),
        _integer(raw_dataset.get("facility_count"), "input.reference_dataset.facility_count"),
        _integer(raw_dataset.get("quarter_count"), "input.reference_dataset.quarter_count"),
    )
    reference = example_input.get("borrower_reference")
    borrower = next((item for item in dataset.borrowers if item.reference == reference), None)
    if borrower is None:
        raise ProductArmError(f"Reference borrower {reference!r} does not exist.")
    label = dataset.labels.by_borrower.get(borrower.id)
    if example_input.get("cohort") == "deteriorating" and label is None:
        raise ProductArmError(f"Reference borrower {reference!r} has no outcome label.")
    return dataset, borrower, label.breach_date if label is not None else dataset.signal_end_date


def _forecast(example_input: Mapping[str, object]) -> Mapping[str, object]:
    dataset, borrower, label_date = _reference_context(example_input)
    all_events = tuple(dataset.events_for_borrower(borrower.id))
    utilisation = tuple(event for event in all_events if event.family == "utilisation")
    cohort = str(example_input.get("cohort"))
    as_of = label_date - timedelta(days=1) if cohort == "deteriorating" else dataset.signal_end_date
    history = tuple(event for event in utilisation if event.event_date <= as_of)
    observations = tuple(
        Observation(observed_on=event.event_date, value=event.magnitude, source_id=event.id)
        for event in history
    )
    adverse_dates = tuple(event.event_date for event in history if event.is_adverse)
    persistence = score_persistence(adverse_dates, as_of, _T3_THRESHOLDS)
    current = history[-1].magnitude if history else None
    projected_for_materiality = (
        current + Decimal("5") if current is not None and persistence.sustained else current
    )
    materiality = score_materiality(
        (
            CovenantHeadroom(
                covenant_id="utilisation",
                threshold=_REFERENCE_THRESHOLD,
                direction="max",
                current_value=current,
                projected_value_90d=projected_for_materiality,
            ),
        ),
        _T4_THRESHOLDS,
    )
    evidence = {
        "id": f"{borrower.reference}:utilisation",
        "state": "sustained" if persistence.sustained else "transient",
        "counts_toward_pressure": materiality.counts_toward_pressure,
        "materiality_pct": materiality.materiality_pct,
        "decay_factor": Decimal("1"),
    }
    projection = project(
        observations,
        evidence_pressure((evidence,), Direction.MAX),
        _FORECAST_HORIZON_DAYS,
        _REFERENCE_THRESHOLD,
        Direction.MAX,
        recent_periods=90,
    )
    crossing = first_crossing(projection, as_of_date=as_of)
    crossing_date = (
        crossing.crossing_date.isoformat() if crossing.crossing_date is not None else None
    )
    difference = abs((crossing.crossing_date - label_date).days) if crossing.crossing_date else None
    escalates = bool(crossing.crossed and persistence.sustained)
    return {
        "crossing_date": crossing_date,
        "label_date": label_date.isoformat(),
        "difference_days": difference,
        "crossing_within_days_of_label": difference is not None and difference <= 10,
        "escalates": escalates,
    }


def _recorded_client(cassette_path: Path) -> ModelClient:
    if not cassette_path.exists():
        raise EvaluationSkip(f"cassette miss: recorded response path not found ({cassette_path})")
    try:
        provider = RecordedProvider(cassette_path=cassette_path)
    except (OSError, ValueError, RuntimeError) as error:
        raise EvaluationSkip(f"cassette miss: {error}") from error
    return ModelClient(
        provider,
        model="evaluation-recorded",
        budget=BudgetLimits(calls_per_hour=100, calls_per_day=1000),
        model_calls=InMemoryModelCallWriter(),
        clock=FixedClock(_EVALUATION_CLOCK),
    )


def _proposal_output(proposal: StageOneProposal) -> dict[str, object]:
    definition = LIBRARY.get(proposal.definition_ref or "")
    plausible = None
    if definition is not None and proposal.threshold is not None:
        plausible = not (
            definition.plausible_min is not None
            and proposal.threshold < definition.plausible_min
            or definition.plausible_max is not None
            and proposal.threshold > definition.plausible_max
        )
    refused = (
        not proposal.parseable or proposal.definition_ref is None or proposal.threshold is None
    )
    refusal_reason = None
    if not proposal.parseable:
        refusal_reason = "proposal_unparseable"
    elif proposal.threshold is not None and plausible is False:
        refused = True
        refusal_reason = "threshold_not_plausible"
    elif proposal.definition_ref is None:
        refusal_reason = "definition_not_known"
    return {
        "refused": refused,
        "definition_code": proposal.definition_ref,
        "direction": proposal.direction,
        "threshold": format(proposal.threshold, "f") if proposal.threshold is not None else None,
        "unit": proposal.unit,
        "plausible": plausible,
        "refusal_reason": refusal_reason,
    }


def _extraction(
    example_input: Mapping[str, object], *, cassette_path: Path, client: ModelClient | None
) -> Mapping[str, object]:
    clause_text = example_input.get("clause_text")
    if not isinstance(clause_text, str) or not clause_text.strip():
        raise ProductArmError("input.clause_text must be non-blank text.")
    scan = scan_for_injection(clause_text)
    if scan.detected:
        return {
            "refused": True,
            "definition_code": None,
            "direction": None,
            "threshold": None,
            "unit": None,
            "plausible": None,
            "refusal_reason": "injection_detected",
        }
    line = CandidateLine(
        page_number=1, start_offset=0, end_offset=len(clause_text), text=clause_text
    )
    detection = detect_candidates((CandidatePage(page_number=1, text=clause_text, lines=(line,)),))
    if not detection.candidates:
        raise ProductArmError("The production detector found no clause candidate.")
    resolved_client = client or _recorded_client(cassette_path)
    try:
        proposals = propose_candidates(detection.candidates, resolved_client)
    except ProviderUnavailable as error:
        raise EvaluationSkip(f"cassette miss: {error}") from error
    if not proposals:
        raise ProductArmError("The production proposal stage returned no proposal.")
    return _proposal_output(proposals[0])


def _memo_slot_map(
    example_id: str, raw_slots: Mapping[str, object], drivers: Sequence[object]
) -> MemoSlotMap:
    from covenant_radar.services.memo import assemble_memo_slots

    reference = RecordReference("evaluation", example_id)
    position = MemoRecord(
        reference=reference,
        values={
            "ratio_name": raw_slots.get("ratio_name"),
            "value": _decimal(raw_slots.get("current_value"), "input.slots.current_value"),
            "threshold": _decimal(raw_slots.get("threshold"), "input.slots.threshold"),
            "headroom": None,
            "probability": None,
            "confidence": None,
            "crossing_date": _date(raw_slots.get("breach_date"), "input.slots.breach_date"),
        },
    )
    driver_records = tuple(
        MemoRecord(
            reference=RecordReference("evaluation-driver", f"{example_id}-{index}"),
            values={"name": str(driver)},
        )
        for index, driver in enumerate(drivers, start=1)
    )
    return assemble_memo_slots(MemoRecords(covenant_position=position, drivers=driver_records))


def _nested_text(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(text for child in value.values() for text in _nested_text(child))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(text for child in value for text in _nested_text(child))
    if isinstance(value, Decimal | date):
        return (format(value, "f") if isinstance(value, Decimal) else value.isoformat(),)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return (str(value),)
    return ()


def _grounding(example_id: str, example_input: Mapping[str, object]) -> Mapping[str, object]:
    raw_slots = _mapping(example_input.get("slots"), "input.slots")
    draft = _mapping(example_input.get("draft"), "input.draft")
    raw_drivers = draft.get("drivers", ())
    drivers = (
        raw_drivers
        if isinstance(raw_drivers, Sequence)
        and not isinstance(raw_drivers, str | bytes | bytearray)
        else ()
    )
    slots = _memo_slot_map(example_id, raw_slots, drivers)
    report = check_stage7_shapes(draft, slots, ())
    allowed = {
        token
        for slot in slots
        for text in _nested_text(slot.value)
        for token in extract_numeric_tokens(text)
    }
    prose = "\n".join(
        str(draft.get(name, ""))
        for name in ("headline", "summary", "recommended_next_step", "disclaimer")
    )
    fabricated = list(
        dict.fromkeys(token for token in extract_numeric_tokens(prose) if token not in allowed)
    )
    return {"grounding_passed": report.grounding.passed, "fabricated_tokens": fabricated}


def _refusal(example_input: Mapping[str, object]) -> Mapping[str, object]:
    raw_reply = example_input.get("model_reply_raw")
    if not isinstance(raw_reply, str) or not raw_reply:
        raise ProductArmError("input.model_reply_raw must be non-empty text.")
    slots = _memo_slot_map(
        "refusal",
        {
            "ratio_name": "Leverage ratio",
            "current_value": "3.25",
            "threshold": "3.00",
            "breach_date": "2026-08-04",
        },
        (),
    )
    report = check_stage7_shapes(raw_reply, slots, ())
    failures = report.failures
    if not failures:
        return {"refused": False, "reasons": []}
    first = failures[0].split(": ", maxsplit=1)[-1]
    if first.startswith("model reply is not valid JSON"):
        first = "model reply is not valid JSON"
    return {"refused": True, "reasons": [first]}


def _usefulness(example_input: Mapping[str, object]) -> Mapping[str, object]:
    draft = _mapping(example_input.get("memo_draft"), "input.memo_draft")
    prose = " ".join(str(draft.get(name, "")) for name in ("headline", "summary"))
    drivers = draft.get("drivers")
    driver_values = (
        tuple(item.strip() for item in drivers if isinstance(item, str) and item.strip())
        if isinstance(drivers, Sequence) and not isinstance(drivers, str | bytes | bytearray)
        else ()
    )
    criteria = (
        {
            "name": "names the specific covenant and its threshold",
            "satisfied": bool(
                re.search(r"\b(?:ratio|covenant|dscr|leverage|liquidity|coverage)\b", prose, re.I)
            )
            and len(extract_numeric_tokens(prose)) >= 1,
        },
        {
            "name": "states the breach or crossing date",
            "satisfied": bool(re.search(r"\b\d{4}-\d{2}-\d{2}\b", prose)),
        },
        {
            "name": "cites a concrete driver rather than a generic statement",
            "satisfied": bool(driver_values)
            and any(value.casefold() not in _GENERIC_DRIVER_NAMES for value in driver_values),
        },
        {
            "name": "recommends a next step from the permitted catalogue, or names its absence",
            "satisfied": bool(str(draft.get("recommended_next_step", "")).strip()),
        },
        {
            "name": "carries the fixed advisory disclaimer",
            "satisfied": str(draft.get("disclaimer", "")).strip().casefold()
            == "human credit review is required before action",
        },
    )
    score = sum(item["satisfied"] is True for item in criteria)
    return {"rubric_score": score, "criteria": criteria, "rubric_floor_met": score >= 4}


def run_product_example(
    example: Mapping[str, object],
    *,
    cassette_path: Path | str = DEFAULT_CASSETTES_DIR,
    client: ModelClient | None = None,
) -> Mapping[str, object]:
    """Run one example through the production arm and return score facts."""

    if not isinstance(example, Mapping):
        raise TypeError("run_product_example requires an example mapping.")
    kind = example.get("kind")
    example_id = example.get("id", "<unknown>")
    if not isinstance(kind, str):
        raise ProductArmError(f"Example {example_id!r} has no valid kind.")
    raw_input = example.get("input")
    input_values = _mapping(raw_input, f"{example_id}.input")
    cassette = Path(cassette_path)
    if kind == "engine":
        return _engine(input_values)
    if kind == "boundary":
        return _boundary(input_values)
    if kind == "persistence":
        return _persistence(input_values)
    if kind == "materiality":
        return _materiality(input_values)
    if kind == "forecast_dating" or kind == "false_escalation":
        return _forecast(input_values)
    if kind == "extraction":
        return _extraction(input_values, cassette_path=cassette, client=client)
    if kind == "grounding":
        return _grounding(str(example_id), input_values)
    if kind == "refusal":
        return _refusal(input_values)
    if kind == "usefulness":
        return _usefulness(input_values)
    raise ProductArmError(f"No production dispatcher exists for example kind {kind!r}.")


__all__ = [
    "ProductArmError",
    "reference_dataset",
    "run_product_example",
]
