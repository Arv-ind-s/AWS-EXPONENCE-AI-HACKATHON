"""The common, validated trace shape used by every decision stage.

The trace is deliberately a domain value object.  It knows nothing about
SQLAlchemy or the web layer, and therefore can be built and checked before a
write is attempted.  The database adapter is responsible only for attaching
subject and provenance metadata to this value.

Trace payloads cross a JSON boundary.  Values with an unambiguous, lossless
text representation (``Decimal``, UUIDs and dates) are represented as text;
other values are converted with ``str`` and recorded in the reserved
``_coercions`` field of the inputs.  This keeps an explainability row intact
without pretending an arbitrary Python object was faithfully serialised.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from math import isfinite
from typing import Final
from uuid import UUID

TRACE_STAGE_MIN: Final[int] = 1
TRACE_STAGE_MAX: Final[int] = 7
TRACE_COERCIONS_KEY: Final[str] = "_coercions"


class TraceStage(IntEnum):
    """The seven ordered decision stages in the product pipeline."""

    INTAKE = 1
    COVENANT_ENGINE = 2
    EVIDENCE_LEDGER = 3
    FORECAST = 4
    INTERVENTION = 5
    TRIAGE = 6
    MEMO = 7


class Decider(StrEnum):
    """The allowed source of a stage's decision."""

    CODE = "code"
    MODEL = "model"
    STATISTICAL = "statistical"


class ThresholdSide(StrEnum):
    """The relation of an observed value to a compared threshold."""

    ABOVE = "above"
    BELOW = "below"
    AT = "at"


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """One validated stage decision, independent of persistence metadata."""

    stage: int
    decider: str
    inputs: Mapping[str, object]
    outputs: Mapping[str, object]
    rule_or_prompt_version: str | None
    thresholds_compared: tuple[Mapping[str, object], ...]
    confidence: Decimal
    sources: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class TraceReadRecord:
    """A trace row selected for display, including a not-run stage marker.

    ``TraceRecord`` is the write value object.  A read has a presentation
    concern that cannot be persisted as a row: a stage with no history.  This
    separate type makes that state explicit without fabricating a database
    record or weakening the write contract.
    """

    stage: int
    decider: str | None
    inputs: Mapping[str, object]
    outputs: Mapping[str, object]
    rule_or_prompt_version: str | None
    thresholds_compared: tuple[Mapping[str, object], ...]
    confidence: Decimal | None
    sources: tuple[object, ...]
    not_run: bool = False
    row_id: UUID | None = None
    subject_type: str | None = None
    subject_id: UUID | None = None
    request_id: str | None = None
    occurred_at: datetime | None = None


def stage_record(
    stage: int | str | TraceStage,
    decider: str | Decider,
    inputs: Mapping[str, object],
    outputs: Mapping[str, object],
    rule_or_prompt_version: str | None,
    thresholds_compared: Sequence[Mapping[str, object]],
    confidence: Decimal,
    sources: Sequence[object] | None,
) -> TraceRecord:
    """Build and validate the one trace shape shared by all stages.

    The returned mappings are copied, so changing a caller-owned payload after
    this function returns cannot alter the value that is handed to a
    repository.  The values inside those mappings are converted recursively
    to portable JSON values.
    """

    normalized_stage = _stage_number(stage)
    normalized_decider = _decider_value(decider)
    normalized_confidence = _confidence(confidence)
    if not isinstance(inputs, Mapping):
        raise ValueError("Trace inputs must be a mapping.")
    if not isinstance(outputs, Mapping):
        raise ValueError("Trace outputs must be a mapping.")
    if not isinstance(thresholds_compared, Sequence) or isinstance(
        thresholds_compared, str | bytes | bytearray
    ):
        raise ValueError("Trace thresholds_compared must be a sequence of mappings.")
    if sources is not None and (
        not isinstance(sources, Sequence) or isinstance(sources, str | bytes | bytearray)
    ):
        raise ValueError("Trace sources must be a sequence or null.")

    coercions: list[dict[str, str]] = []
    normalized_inputs = _json_mapping(inputs, "inputs", coercions)
    normalized_outputs = _json_mapping(outputs, "outputs", coercions)
    normalized_thresholds = _thresholds(thresholds_compared, coercions)
    normalized_sources = tuple(
        _json_value(value, f"sources[{index}]", coercions)
        for index, value in enumerate(sources or ())
    )

    if coercions:
        if TRACE_COERCIONS_KEY in normalized_inputs:
            raise ValueError(
                f"Trace inputs reserve the {TRACE_COERCIONS_KEY!r} key for coercion notes."
            )
        normalized_inputs[TRACE_COERCIONS_KEY] = coercions

    normalized_rule = _optional_text(rule_or_prompt_version, "rule_or_prompt_version")
    return TraceRecord(
        stage=normalized_stage,
        decider=normalized_decider,
        inputs=normalized_inputs,
        outputs=normalized_outputs,
        rule_or_prompt_version=normalized_rule,
        thresholds_compared=normalized_thresholds,
        confidence=normalized_confidence,
        sources=normalized_sources,
    )


def _stage_number(value: int | str | TraceStage) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Trace stage must be between {TRACE_STAGE_MIN} and {TRACE_STAGE_MAX}.")
    if isinstance(value, TraceStage):
        number = int(value)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str):
        candidate = value.strip().lower().replace("_", "-")
        if candidate.startswith("stage-"):
            candidate = candidate.removeprefix("stage-")
        try:
            number = int(candidate)
        except ValueError as error:
            raise ValueError(
                f"Trace stage {value!r} must be a number between "
                f"{TRACE_STAGE_MIN} and {TRACE_STAGE_MAX}."
            ) from error
    else:
        raise ValueError(
            f"Trace stage {value!r} must be a number between "
            f"{TRACE_STAGE_MIN} and {TRACE_STAGE_MAX}."
        )
    if not TRACE_STAGE_MIN <= number <= TRACE_STAGE_MAX:
        raise ValueError(
            f"Trace stage {number} is outside the defined range "
            f"{TRACE_STAGE_MIN}..{TRACE_STAGE_MAX}."
        )
    return number


def _decider_value(value: str | Decider) -> str:
    if isinstance(value, Decider):
        return value.value
    if not isinstance(value, str) or value not in {item.value for item in Decider}:
        allowed = ", ".join(item.value for item in Decider)
        raise ValueError(f"Trace decider must be one of: {allowed}.")
    return value


def _confidence(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Trace confidence must be a finite Decimal between 0 and 1.")
    if not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("Trace confidence must be between 0 and 1.")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Trace {field_name} must be text or null.")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"Trace {field_name} must be non-empty text or null.")
    return cleaned


def _json_mapping(
    value: Mapping[str, object], path: str, coercions: list[dict[str, str]]
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            key_text = _safe_string(key)
            coercions.append({"path": f"{path}.{key_text}", "type": type(key).__name__})
        else:
            key_text = key
        if key_text in normalized:
            raise ValueError(f"Trace payload contains duplicate key {key_text!r} at {path}.")
        normalized[key_text] = _json_value(item, f"{path}.{key_text}", coercions)
    return normalized


def _json_value(value: object, path: str, coercions: list[dict[str, str]]) -> object:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if isfinite(value):
            return value
        return _coerce(value, path, coercions)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return _json_mapping(value, path, coercions)
    if isinstance(value, list | tuple):
        return [
            _json_value(item, f"{path}[{index}]", coercions) for index, item in enumerate(value)
        ]
    return _coerce(value, path, coercions)


def _coerce(value: object, path: str, coercions: list[dict[str, str]]) -> str:
    text = _safe_string(value)
    coercions.append({"path": path, "type": type(value).__name__})
    return text


def _safe_string(value: object) -> str:
    try:
        text = str(value)
    except Exception:
        text = f"<{type(value).__name__} could not be stringified>"
    return text if isinstance(text, str) else f"<{type(value).__name__}>"


def _thresholds(
    values: Sequence[Mapping[str, object]], coercions: list[dict[str, str]]
) -> tuple[Mapping[str, object], ...]:
    required = frozenset({"name", "value", "observed", "side"})
    result: list[Mapping[str, object]] = []
    for index, comparison in enumerate(values):
        if not isinstance(comparison, Mapping):
            raise ValueError(f"Threshold comparison entry {index} must be a mapping.")
        name = comparison.get("name")
        label = name if isinstance(name, str) and name.strip() else f"entry {index}"
        missing = required.difference(comparison)
        if "side" in missing:
            raise ValueError(f"Threshold comparison {label!r} is missing its side.")
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"Threshold comparison {label!r} is missing: {names}.")
        extra = set(comparison).difference(required)
        if extra:
            names = ", ".join(sorted(str(item) for item in extra))
            raise ValueError(f"Threshold comparison {label!r} has unknown fields: {names}.")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Threshold comparison entry {index} must have a name.")
        side = comparison["side"]
        if isinstance(side, ThresholdSide):
            side = side.value
        if not isinstance(side, str) or side not in {item.value for item in ThresholdSide}:
            allowed = ", ".join(item.value for item in ThresholdSide)
            raise ValueError(f"Threshold comparison {name!r} side must be one of: {allowed}.")
        result.append(
            {
                "name": name.strip(),
                "value": _json_value(
                    comparison["value"],
                    f"thresholds_compared[{index}].value",
                    coercions,
                ),
                "observed": _json_value(
                    comparison["observed"], f"thresholds_compared[{index}].observed", coercions
                ),
                "side": side,
            }
        )
    return tuple(result)


__all__ = [
    "Decider",
    "TRACE_COERCIONS_KEY",
    "TRACE_STAGE_MAX",
    "TRACE_STAGE_MIN",
    "ThresholdSide",
    "TraceReadRecord",
    "TraceRecord",
    "TraceStage",
    "stage_record",
]
