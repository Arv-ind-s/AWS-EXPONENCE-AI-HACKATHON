"""Response schema for the read-only case resource (`C-21`)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class CaseRead(_ResponseModel):
    """One borrower's tracked remediation case (`plan.md §5.8`)."""

    id: UUID
    reference: str
    borrower_id: UUID
    opened_from_run_id: UUID | None
    state: str
    band_at_open: str | None
    assignee_id: UUID | None
    due_at: datetime | None
    sla_hours: int | None
    closed_at: datetime | None
    closure_reason: str | None
    closure_note: str | None
    version: int
    created_at: datetime
    updated_at: datetime


__all__ = ["CaseRead"]
