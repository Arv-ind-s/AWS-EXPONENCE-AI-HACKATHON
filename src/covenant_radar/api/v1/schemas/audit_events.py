"""Response schema for the read-only audit-event resource (`C-21`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class AuditEventRead(_ResponseModel):
    """One immutable link in the append-only audit chain (`plan.md §5.9`)."""

    id: UUID
    sequence: int
    occurred_at: datetime
    actor_id: UUID | None
    actor_label: str | None
    event_type: str
    subject_type: str
    subject_id: UUID
    payload: dict[str, Any]
    prev_hash: str | None
    hash: str


__all__ = ["AuditEventRead"]
