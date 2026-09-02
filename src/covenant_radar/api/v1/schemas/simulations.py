"""Response schema for the read-only simulation resource (`C-21`)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SimulationRead(_ResponseModel):
    """One persisted "what if" counterfactual against one forecast (`plan.md §5.7`)."""

    id: UUID
    forecast_id: UUID
    intervention_id: UUID
    parameters: dict[str, Any]
    assumptions: dict[str, Any] | None
    projected_cross_date: date | None
    probability: Decimal | None
    delta_days: int | None
    delta_probability: Decimal | None
    created_at: datetime
    supersedes_simulation_id: UUID | None = None
    superseded_by_simulation_id: UUID | None = None
    based_on_superseded_run: bool = False


__all__ = ["SimulationRead"]
