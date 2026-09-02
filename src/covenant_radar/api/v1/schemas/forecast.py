"""Strict response models for the stored forecast-path API (C-03)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _ResponseModel(BaseModel):
    """Response defaults shared by read-only API payloads."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ForecastDriverRead(_ResponseModel):
    """One persisted driver attached to the selected forecast horizon."""

    name: str = Field(min_length=1, max_length=100)
    share: Decimal
    evidence_id: UUID | None
    is_other: bool


class ForecastPathRead(_ResponseModel):
    """One stored daily path point and its persisted horizon facts."""

    day: int = Field(ge=0)
    projected_value: Decimal | None
    headroom_pct: Decimal | None
    probability: Decimal | None
    confidence: Decimal | None
    below_confidence_floor: bool
    crossing_date: date | None
    drivers: list[ForecastDriverRead] = Field(default_factory=list)


class ForecastRead(_ResponseModel):
    """One covenant version's outcome at one horizon, within one run (`C-21`)."""

    id: UUID
    run_id: UUID
    covenant_version_id: UUID
    horizon_days: int
    probability: Decimal | None
    probability_source: str
    fallback_reason: str | None
    confidence: Decimal | None
    below_confidence_floor: bool
    projected_cross_date: date | None
    direction: str | None
    data_as_of: date | None
    staleness_days: int | None
    created_at: datetime


__all__ = ["ForecastDriverRead", "ForecastPathRead", "ForecastRead"]
