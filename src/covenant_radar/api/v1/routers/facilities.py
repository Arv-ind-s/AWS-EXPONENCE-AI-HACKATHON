"""Facility REST routes, including effective-dated limit changes."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from covenant_radar.api.deps import requires
from covenant_radar.api.v1.schemas.master_data import (
    FacilityCreate,
    FacilityRead,
    FacilityUpdate,
)
from covenant_radar.db.models.facility import Facility
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.master_data import MasterDataService

_READ = requires(Permission.VIEW_BORROWER)
_WRITE = requires(Permission.CORRECT_SOURCE_DATA)
_READ_DEP = Depends(_READ)
_WRITE_DEP = Depends(_WRITE)


def create_facilities_router(service: MasterDataService, *, prefix: str = "/api/v1") -> APIRouter:
    """Build the scoped facility endpoints."""
    router = APIRouter(prefix=prefix, tags=["master-data"])

    @router.get("/facilities", response_model=list[FacilityRead], name="api_facility_list")
    async def list_facilities(
        principal: Principal = _READ_DEP,
        current_only: bool = Query(default=True),
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[FacilityRead]:
        rows = service.list_facilities(
            principal,
            current_only=current_only,
            limit=limit,
            offset=offset,
        )
        return [_facility_read(row) for row in rows]

    @router.post(
        "/facilities",
        response_model=FacilityRead,
        status_code=status.HTTP_201_CREATED,
        name="api_facility_create",
    )
    async def create_facility(
        payload: FacilityCreate,
        principal: Principal = _WRITE_DEP,
    ) -> FacilityRead:
        row = service.create_facility(principal, **payload.model_dump())
        return _facility_read(row)

    @router.get("/facilities/{reference}", response_model=FacilityRead, name="api_facility_detail")
    async def get_facility(reference: str, principal: Principal = _READ_DEP) -> FacilityRead:
        return _facility_read(service.get_facility(principal, reference))

    @router.get(
        "/facilities/by-borrower/{borrower_reference}/as-of",
        response_model=FacilityRead,
        name="api_facility_as_of",
    )
    async def get_facility_as_of(
        borrower_reference: str,
        as_of: date,
        principal: Principal = _READ_DEP,
    ) -> FacilityRead:
        return _facility_read(
            service.get_facility_as_of(
                principal,
                borrower_reference=borrower_reference,
                as_of=as_of,
            )
        )

    @router.patch(
        "/facilities/{reference}", response_model=FacilityRead, name="api_facility_update"
    )
    async def update_facility(
        reference: str,
        payload: FacilityUpdate,
        principal: Principal = _WRITE_DEP,
    ) -> FacilityRead:
        values = payload.model_dump(
            exclude={"expected_version", "sanctioned_limit", "effective_from", "new_reference"},
            exclude_unset=True,
        )
        row = service.update_facility(
            principal,
            reference,
            expected_version=payload.expected_version,
            sanctioned_limit=payload.sanctioned_limit,
            effective_from=payload.effective_from,
            new_reference=payload.new_reference,
            **values,
        )
        return _facility_read(row)

    @router.post(
        "/facilities/{reference}/deactivate",
        response_model=FacilityRead,
        name="api_facility_deactivate",
    )
    async def deactivate_facility(
        reference: str,
        expected_version: Annotated[int, Query(ge=1)],
        effective_to: date | None = None,
        principal: Principal = _WRITE_DEP,
    ) -> FacilityRead:
        row = service.deactivate_facility(
            principal,
            reference,
            expected_version=expected_version,
            effective_to=effective_to,
        )
        return _facility_read(row)

    return router


def _facility_read(row: Facility) -> FacilityRead:
    return FacilityRead(
        id=row.id,
        reference=row.reference,
        borrower_id=row.borrower_id,
        facility_type=row.facility_type,
        sanctioned_limit=row.sanctioned_limit,
        currency=row.currency,
        drawing_power=row.drawing_power,
        outstanding=row.outstanding,
        security_type=row.security_type,
        pricing_bps=row.pricing_bps,
        sanction_date=row.sanction_date,
        maturity_date=row.maturity_date,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        superseded_by_id=row.superseded_by_id,
        version=row.version,
    )


__all__ = ["create_facilities_router"]
