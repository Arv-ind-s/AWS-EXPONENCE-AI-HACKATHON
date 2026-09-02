"""Strict response models for the explainability API (T-072).

The explainability response deliberately mirrors ``ExplainStage`` from the
trace reader.  Keeping the API shape aligned with the reader means the page,
HTMX fragment and JSON resource all expose the same persisted facts rather
than each surface inventing its own interpretation of a decision.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from covenant_radar.audit.trace_reader import ExplainStage

type JsonValue = None | bool | int | float | str | dict[str, "JsonValue"] | list["JsonValue"]


class _ResponseModel(BaseModel):
    """Response defaults shared by the read-only explainability payload."""

    model_config = ConfigDict(extra="forbid")


class ExplainThresholdRead(_ResponseModel):
    """One complete threshold comparison from a stage trace."""

    name: str = Field(min_length=1, max_length=300)
    value: JsonValue
    observed: JsonValue
    side: Literal["above", "below", "at"]


class ExplainStageRead(_ResponseModel):
    """One stage, including an explicit marker when it did not run."""

    stage: int = Field(ge=1, le=7)
    name: str = Field(min_length=1, max_length=100)
    decider: Literal["code", "model", "statistical"] | None
    inputs: dict[str, JsonValue]
    outputs: dict[str, JsonValue]
    rule_or_prompt_version: str | None = Field(max_length=50)
    thresholds_compared: list[ExplainThresholdRead]
    confidence: Decimal | None = Field(ge=Decimal("0"), le=Decimal("1"))
    sources: list[JsonValue]
    not_run: bool
    row_id: UUID | None
    occurred_at: datetime | None

    @classmethod
    def from_stage(cls, stage: ExplainStage) -> ExplainStageRead:
        """Convert the service reader's immutable record into this schema."""

        # ``ExplainStage`` is already normalized by the trace reader, but
        # model_validate remains the schema boundary.  It protects this API
        # if a future reader or adapter supplies an invalid nested value and
        # also keeps the runtime validation aligned with the OpenAPI schema.
        return cls.model_validate(
            {
                "stage": stage.stage,
                "name": stage.name,
                "decider": stage.decider,
                "inputs": dict(stage.inputs),
                "outputs": dict(stage.outputs),
                "rule_or_prompt_version": stage.rule_or_prompt_version,
                "thresholds_compared": list(stage.thresholds_compared),
                "confidence": stage.confidence,
                "sources": list(stage.sources),
                "not_run": stage.not_run,
                "row_id": stage.row_id,
                "occurred_at": stage.occurred_at,
            }
        )


class ExplainRead(_ResponseModel):
    """The complete explanation for one scoped subject."""

    subject_type: str = Field(min_length=1, max_length=50)
    subject_id: UUID
    stages: list[ExplainStageRead] = Field(min_length=7, max_length=7)

    @classmethod
    def from_stages(
        cls,
        *,
        subject_type: str,
        subject_id: UUID,
        stages: tuple[ExplainStage, ...],
    ) -> ExplainRead:
        """Build a response from the one service-owned stage sequence."""

        return cls(
            subject_type=subject_type,
            subject_id=subject_id,
            stages=[ExplainStageRead.from_stage(stage) for stage in stages],
        )


# Descriptive aliases keep callers free to use either the route's short name
# or the noun used in API documentation without creating competing schemas.
ExplanationRead = ExplainRead
ExplanationStageRead = ExplainStageRead
ExplanationThresholdRead = ExplainThresholdRead


__all__ = [
    "ExplainRead",
    "ExplainStageRead",
    "ExplainThresholdRead",
    "ExplanationRead",
    "ExplanationStageRead",
    "ExplanationThresholdRead",
    "JsonValue",
]
