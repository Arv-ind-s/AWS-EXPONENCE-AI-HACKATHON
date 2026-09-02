"""Response schema for the read-only covenant-test resource (`C-21`)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class CovenantTestRead(_ResponseModel):
    """One computed test of one covenant version, as persisted (`plan.md §5.5`)."""

    id: UUID
    covenant_version_id: UUID
    period_id: UUID | None
    as_of_date: date
    value: Decimal | None
    threshold_used: Decimal | None
    headroom_pct: Decimal | None
    verdict: str
    exception_id: UUID | None
    waiver_id: UUID | None
    cure_ends_on: date | None
    not_computable_reason: str | None
    computed_at: datetime
    job_run_id: UUID | None


__all__ = ["CovenantTestRead"]
