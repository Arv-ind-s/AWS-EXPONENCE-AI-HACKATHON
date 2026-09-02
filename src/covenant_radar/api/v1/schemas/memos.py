"""Response schema for the read-only memo resource (`C-21`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class MemoRead(_ResponseModel):
    """One generated borrower memo, written only after the shape check passes."""

    id: UUID
    borrower_id: UUID
    run_id: UUID | None
    case_id: UUID | None
    template_version: str
    prompt_version: str | None
    provider: str | None
    model_version: str | None
    slots: dict[str, Any]
    drafted_text: str
    actions: dict[str, Any] | None
    simulations: dict[str, Any] | None
    check_verdict: str | None
    generated_by_id: UUID | None
    version: int
    created_at: datetime


__all__ = ["MemoRead"]
