"""Validated request and response schemas for ingestion endpoints."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_MAX_BATCH_SIZE = 10_000


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SignalEventRequest(_RequestModel):
    """One source-neutral signal event."""

    borrower_id: UUID
    facility_id: UUID | None = None
    event_date: date
    family: Annotated[str, Field(min_length=1, max_length=20)]
    event_type: Annotated[str, Field(min_length=1, max_length=50)]
    magnitude: Decimal = Field(max_digits=24, decimal_places=8)
    unit: Annotated[str, Field(min_length=1, max_length=20)]
    payload: dict[str, object]
    source_id: UUID | None = None
    is_late: bool = False
    content_hash: Annotated[str, Field(min_length=64, max_length=128)] | None = None


class SignalBatchRequest(_RequestModel):
    """A bounded signal batch accepted by ``POST /ingest/signals``."""

    events: Annotated[list[SignalEventRequest], Field(min_length=1, max_length=_MAX_BATCH_SIZE)]
    source_id: UUID | None = None
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class SignalIngestionResponse(BaseModel):
    """Reconciled counts returned by a successful ingestion run."""

    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    received: int
    inserted: int
    duplicates: int
    rejected: int
    quarantined: int
    accepted: int
    reconciled: bool
    source_ids: list[UUID]


class StatementQuarantineSummary(BaseModel):
    """One quarantined row in a statement import response (`T-025`)."""

    model_config = ConfigDict(extra="forbid")

    row_number: int
    rule_failed: str
    message: str


class StatementDiscrepancySummary(BaseModel):
    """One totals-row reconciliation discrepancy in a statement import
    response (`T-025`)."""

    model_config = ConfigDict(extra="forbid")

    line_code: str
    expected: str
    actual: str
    difference: str


class StatementImportResponse(BaseModel):
    """Reconciled counts and report returned by a successful statement
    import run (`T-025`, `C-22`)."""

    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    mapping_name: str
    mapping_version: int
    source_type: str
    content_hash: str
    received: int
    accepted: int
    quarantined: int
    totals_rows: int
    quarantine: list[StatementQuarantineSummary]
    discrepancies: list[StatementDiscrepancySummary]
    reconciled: bool


__all__ = [
    "SignalBatchRequest",
    "SignalEventRequest",
    "SignalIngestionResponse",
    "StatementDiscrepancySummary",
    "StatementImportResponse",
    "StatementQuarantineSummary",
]
