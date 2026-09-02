"""Strict request and response models for the covenant registry API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from covenant_radar.core.errors import DomainError
from covenant_radar.domain.covenants.model import CovenantVersionTerms

_Direction = Literal["min", "max"]
_Frequency = Literal["monthly", "quarterly", "half_yearly", "annual", "on_event"]
_Text = Annotated[str, Field(min_length=1, max_length=300)]
_Reference = Annotated[str, Field(min_length=1, max_length=20)]
_Unit = Annotated[str, Field(min_length=1, max_length=20)]
_TestBasis = Annotated[str, Field(min_length=1, max_length=20)]
_Reason = Annotated[str, Field(min_length=1, max_length=2_000)]


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class CovenantTermsRequest(_RequestModel):
    """The version fields shared by registration and amendment."""

    definition_ref: Annotated[str, Field(min_length=1, max_length=20)] | None = None
    custom_formula: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    threshold: Decimal
    direction: _Direction
    unit: _Unit
    frequency: _Frequency
    test_basis: _TestBasis
    effective_from: date
    effective_to: date | None = None
    warning_headroom_pct: Decimal | None = Field(default=None, ge=Decimal("0"))
    cure_days: Annotated[int, Field(ge=0)] | None = None
    grace_days: Annotated[int, Field(ge=0)] | None = None
    source_document_id: UUID | None = None
    source_span_id: UUID | None = None

    @model_validator(mode="after")
    def validate_terms(self) -> CovenantTermsRequest:
        # Domain validation remains authoritative; constructing the frozen
        # value object here gives the API the same error semantics before any
        # service or database work begins.
        try:
            CovenantVersionTerms(**_term_values(self.model_dump()))
        except DomainError as error:
            raise ValueError(error.message) from error
        return self

    def to_domain(self) -> CovenantVersionTerms:
        return CovenantVersionTerms(**_term_values(self.model_dump()))


class CovenantCreateRequest(CovenantTermsRequest):
    facility_id: UUID
    reference: _Reference
    name: _Text
    covenant_class: Annotated[str, Field(min_length=1, max_length=50)]


class CovenantAmendRequest(CovenantTermsRequest):
    pass


class WaiverCreateRequest(_RequestModel):
    from_date: date
    to_date: date | None = None
    reason: _Reason
    waiver_scope: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    document_id: UUID | None = None

    @model_validator(mode="after")
    def validate_window(self) -> WaiverCreateRequest:
        if self.to_date is not None and self.to_date < self.from_date:
            raise ValueError("to_date must not precede from_date")
        return self


class ApprovalDecisionRequest(_RequestModel):
    """Accept both the public ``decision`` wording and API-friendly bool."""

    decision: Literal["approve", "reject"] | None = None
    approved: bool | None = None
    reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def normalize_decision(self) -> ApprovalDecisionRequest:
        if self.decision is None and self.approved is None:
            raise ValueError("decision or approved is required")
        if self.decision is not None and self.approved is not None:
            expected = self.decision == "approve"
            if expected is not self.approved:
                raise ValueError("decision and approved must describe the same outcome")
        return self

    @property
    def is_approved(self) -> bool:
        if self.approved is not None:
            return self.approved
        return self.decision == "approve"


class CovenantVersionRead(_ResponseModel):
    id: UUID
    version_no: int
    definition_ref: str | None
    custom_formula: str | None
    threshold: Decimal
    direction: str
    unit: str
    frequency: str
    test_basis: str
    effective_from: date
    effective_to: date | None
    warning_headroom_pct: Decimal | None
    cure_days: int | None
    grace_days: int | None
    source_document_id: UUID | None
    source_span_id: UUID | None
    status: str
    tested_at_least_once: bool
    registered_by_id: UUID
    approved_by_id: UUID | None
    version: int


class CovenantRead(_ResponseModel):
    id: UUID
    reference: str
    facility_id: UUID
    name: str
    covenant_class: str
    is_active: bool
    version: int
    versions: list[CovenantVersionRead] = Field(default_factory=list)


class ApprovalRequestRead(_ResponseModel):
    id: UUID
    subject_type: str
    subject_id: UUID
    operation: str
    payload: dict[str, object]
    maker_id: UUID
    checker_id: UUID | None
    state: str
    created_at: datetime
    decided_at: datetime | None
    reason: str | None
    version: int


class CovenantActionRead(_ResponseModel):
    covenant: CovenantRead
    version: CovenantVersionRead
    approval_request_id: UUID | None = None
    state: str


class WaiverRead(_ResponseModel):
    id: UUID
    covenant_id: UUID
    from_date: date
    to_date: date | None
    scope: str | None
    reason: str
    document_id: UUID | None
    requested_by_id: UUID | None
    approved_by_id: UUID | None
    state: str
    version: int


_TERM_FIELDS = frozenset(CovenantTermsRequest.model_fields)


def _term_values(values: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in values.items() if key in _TERM_FIELDS}


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalRequestRead",
    "CovenantActionRead",
    "CovenantAmendRequest",
    "CovenantCreateRequest",
    "CovenantRead",
    "CovenantTermsRequest",
    "CovenantVersionRead",
    "WaiverCreateRequest",
    "WaiverRead",
]
