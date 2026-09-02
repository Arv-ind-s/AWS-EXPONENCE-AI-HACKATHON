"""Validated request and response schemas for master-data APIs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

Money = Annotated[Decimal, Field(gt=Decimal("0"), max_digits=18, decimal_places=4)]
NonNegativeMoney = Annotated[Decimal, Field(ge=Decimal("0"), max_digits=18, decimal_places=4)]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class BorrowerCreate(_RequestModel):
    reference: Annotated[str, Field(min_length=1, max_length=20)]
    legal_name: Annotated[str, Field(min_length=1, max_length=300)]
    portfolio_id: UUID
    cin: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    pan: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    industry_code: Annotated[str, Field(min_length=1, max_length=20)] | None = None
    group_id: UUID | None = None
    constitution: Annotated[str, Field(min_length=1, max_length=50)] | None = None
    incorporation_date: date | None = None


class BorrowerUpdate(_RequestModel):
    expected_version: PositiveInt
    legal_name: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    portfolio_id: UUID | None = None
    cin: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    pan: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    industry_code: Annotated[str, Field(min_length=1, max_length=20)] | None = None
    group_id: UUID | None = None
    constitution: Annotated[str, Field(min_length=1, max_length=50)] | None = None
    incorporation_date: date | None = None


class BorrowerRead(_ResponseModel):
    id: UUID
    reference: str
    legal_name: str
    portfolio_id: UUID
    industry_code: str | None
    group_id: UUID | None
    constitution: str | None
    incorporation_date: date | None
    is_active: bool
    version: int
    cin_present: bool = False
    pan_present: bool = False


class FacilityCreate(_RequestModel):
    reference: Annotated[str, Field(min_length=1, max_length=24)]
    borrower_id: UUID
    facility_type: Annotated[str, Field(min_length=1, max_length=50)]
    sanctioned_limit: Money
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    sanction_date: date
    effective_from: date
    drawing_power: NonNegativeMoney | None = None
    outstanding: NonNegativeMoney | None = None
    security_type: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    pricing_bps: Annotated[int, Field(ge=0)] | None = None
    maturity_date: date | None = None

    @field_validator("currency")
    @classmethod
    def currency_is_ascii_code(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.isascii() or not normalized.isalpha():
            raise ValueError("currency must be a three-letter ASCII code")
        return normalized

    @model_validator(mode="after")
    def dates_are_ordered(self) -> FacilityCreate:
        if self.effective_from < self.sanction_date:
            raise ValueError("effective_from cannot precede sanction_date")
        if self.maturity_date is not None and self.maturity_date < self.sanction_date:
            raise ValueError("maturity_date cannot precede sanction_date")
        return self


class FacilityUpdate(_RequestModel):
    expected_version: PositiveInt
    sanctioned_limit: Money | None = None
    effective_from: date | None = None
    new_reference: Annotated[str, Field(min_length=1, max_length=24)] | None = None
    facility_type: Annotated[str, Field(min_length=1, max_length=50)] | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None = None
    drawing_power: NonNegativeMoney | None = None
    outstanding: NonNegativeMoney | None = None
    security_type: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    pricing_bps: Annotated[int, Field(ge=0)] | None = None
    sanction_date: date | None = None
    maturity_date: date | None = None

    @field_validator("currency")
    @classmethod
    def currency_is_ascii_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.upper()
        if not normalized.isascii() or not normalized.isalpha():
            raise ValueError("currency must be a three-letter ASCII code")
        return normalized


class FacilityRead(_ResponseModel):
    id: UUID
    reference: str
    borrower_id: UUID
    facility_type: str
    sanctioned_limit: Decimal
    currency: str
    drawing_power: Decimal | None
    outstanding: Decimal | None
    security_type: str | None
    pricing_bps: int | None
    sanction_date: date
    maturity_date: date | None
    effective_from: date
    effective_to: date | None
    superseded_by_id: UUID | None
    version: int


class PortfolioCreate(_RequestModel):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    parent_id: UUID | None = None
    branch_code: Annotated[str, Field(min_length=1, max_length=32)] | None = None


class PortfolioUpdate(_RequestModel):
    expected_version: PositiveInt
    code: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    parent_id: UUID | None = None
    branch_code: Annotated[str, Field(min_length=1, max_length=32)] | None = None


class PortfolioRead(_ResponseModel):
    id: UUID
    code: str
    name: str
    parent_id: UUID | None
    branch_code: str | None
    path: str
    version: int


__all__ = [
    "BorrowerCreate",
    "BorrowerRead",
    "BorrowerUpdate",
    "FacilityCreate",
    "FacilityRead",
    "FacilityUpdate",
    "PortfolioCreate",
    "PortfolioRead",
    "PortfolioUpdate",
]
