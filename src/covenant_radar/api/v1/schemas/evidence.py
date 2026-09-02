"""Response schema for the read-only evidence resource (`C-21`)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class EvidenceItemRead(_ResponseModel):
    """One scored, persisted evidence ledger row (`plan.md §5.6`)."""

    id: UUID
    borrower_id: UUID
    facility_id: UUID | None
    family: str
    evidence_type: str
    first_seen: date
    last_seen: date
    persistence_days: int | None
    event_count_window: int | None
    materiality_pct: Decimal | None
    decay_factor: Decimal | None
    state: str
    counts_toward_pressure: bool
    superseded_by_id: UUID | None
    supersedes_id: UUID | None
    last_scored_at: datetime | None
    version: int


__all__ = ["EvidenceItemRead"]
