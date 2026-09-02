"""Presentation reader that turns stage-trace rows into why-panel content
(`T-070`, `spec §17.6`, `plan.md §8.6`, `C-41`).

``db/repositories/trace.py``'s ``TraceRepository.read`` already returns one
``TraceReadRecord`` per stage, in stage order, with a missing stage padded as
``not_run``. What that record does not yet carry is a **name** a screen can
show without hardcoding one per template — ``spec §17.6`` requires that a
code stage, a model stage and a future statistical stage all "answer the
same questions in its own terms" through "the same panel, the same
contract", which only holds if the stage's identity comes from one place.
That place is ``domain.trace.TraceStage``; this module resolves a display
name from it and does nothing else to the stage fields, so a code stage and
a model stage keep exactly the field set ``T-037`` fixed.

This module performs no I/O and imports neither SQLAlchemy nor
``covenant_radar.db``, matching ``audit/reconstruct.py``'s precedent: it is
handed already-read ``TraceReadRecord`` values by
``covenant_radar.services.explain`` and only names, validates and shapes
them for display.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from covenant_radar.domain.trace import (
    TRACE_STAGE_MAX,
    TRACE_STAGE_MIN,
    ThresholdSide,
    TraceReadRecord,
    TraceStage,
)

_VALID_SIDES: Final[frozenset[str]] = frozenset(item.value for item in ThresholdSide)


class ExplainSubjectType(StrEnum):
    """The subjects the why-panel can explain today (`C-10`'s path segment).

    Stage 2 writes under ``covenant_test``, stage 3 under ``borrower`` and
    stage 4 under ``forecast`` (`T-037`, `T-051`, `T-058`). Stages 1, 5, 6
    and 7 do not write trace rows yet; the task that makes each of them
    write one adds its subject type here, the one place this registry
    lives, rather than a caller guessing a string that happens to match.
    """

    COVENANT_TEST = "covenant_test"
    BORROWER = "borrower"
    FORECAST = "forecast"


def validate_subject_type(value: object) -> str:
    """Return ``value`` unchanged if it names a known why-panel subject.

    Raises ``ValueError`` naming every valid type otherwise, so a caller can
    show the refusal directly rather than parsing a generic message.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("A why-panel subject type must be non-empty text.")
    candidate = value.strip()
    try:
        return ExplainSubjectType(candidate).value
    except ValueError as error:
        valid = ", ".join(member.value for member in ExplainSubjectType)
        raise ValueError(
            f"Unknown why-panel subject type {candidate!r}; valid types are: {valid}."
        ) from error


def stage_name(stage: int) -> str:
    """Return the one display name for a stage number, from the domain enum."""

    try:
        member = TraceStage(stage)
    except ValueError as error:
        raise ValueError(
            f"Stage {stage!r} is outside the defined range {TRACE_STAGE_MIN}..{TRACE_STAGE_MAX}."
        ) from error
    return member.name.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class ExplainStage:
    """One stage's presentation-ready row for the why-panel.

    Carries exactly ``TraceReadRecord``'s fields plus the resolved
    ``name``, so a code stage, a model stage and a not-run stage share one
    shape — the same guarantee ``T-037`` fixed at the write side.
    """

    stage: int
    name: str
    decider: str | None
    inputs: Mapping[str, object]
    outputs: Mapping[str, object]
    rule_or_prompt_version: str | None
    thresholds_compared: tuple[Mapping[str, object], ...]
    confidence: Decimal | None
    sources: tuple[object, ...]
    not_run: bool
    row_id: UUID | None = None
    occurred_at: datetime | None = None


def present(records: Sequence[TraceReadRecord]) -> tuple[ExplainStage, ...]:
    """Build the ordered, named why-panel stages from repository records.

    ``records`` is expected in the exact shape ``TraceRepository.read``
    returns: one entry per stage, ``TRACE_STAGE_MIN..TRACE_STAGE_MAX`` in
    order, a missing stage already padded as ``not_run``. This function
    checks that shape rather than trusting it blindly, resolves each
    stage's name, and re-asserts — over the stored data, not only at write
    time — that every threshold comparison still names a side, because a
    write-path guarantee is only as strong as a reader that never assumes
    it silently held.
    """

    if not isinstance(records, Sequence) or isinstance(records, str | bytes | bytearray):
        raise TypeError("present() requires a sequence of TraceReadRecord values.")

    stages: list[ExplainStage] = []
    expected = TRACE_STAGE_MIN
    for record in records:
        if not isinstance(record, TraceReadRecord):
            raise TypeError("present() requires TraceReadRecord values.")
        if record.stage != expected:
            raise ValueError(
                "Trace records are not in stage order: expected stage "
                f"{expected}, received stage {record.stage}."
            )
        _assert_sides(record)
        stages.append(
            ExplainStage(
                stage=record.stage,
                name=stage_name(record.stage),
                decider=record.decider,
                inputs=record.inputs,
                outputs=record.outputs,
                rule_or_prompt_version=record.rule_or_prompt_version,
                thresholds_compared=record.thresholds_compared,
                confidence=record.confidence,
                sources=record.sources,
                not_run=record.not_run,
                row_id=record.row_id,
                occurred_at=record.occurred_at,
            )
        )
        expected += 1

    expected_count = TRACE_STAGE_MAX - TRACE_STAGE_MIN + 1
    if len(stages) != expected_count:
        raise ValueError(f"Expected {expected_count} stage records, received {len(stages)}.")
    return tuple(stages)


def _assert_sides(record: TraceReadRecord) -> None:
    for entry in record.thresholds_compared:
        side = entry.get("side") if isinstance(entry, Mapping) else None
        if side not in _VALID_SIDES:
            name = entry.get("name") if isinstance(entry, Mapping) else None
            raise ValueError(
                f"Stage {record.stage} threshold comparison {name!r} is missing a "
                "valid side; the write path should have refused this row."
            )


__all__ = [
    "ExplainStage",
    "ExplainSubjectType",
    "present",
    "stage_name",
    "validate_subject_type",
]
