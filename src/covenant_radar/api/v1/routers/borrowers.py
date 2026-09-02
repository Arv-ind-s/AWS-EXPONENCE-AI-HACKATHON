"""Borrower and portfolio REST routes for the T-023 vertical slice."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from covenant_radar.api.deps import requires
from covenant_radar.api.v1.schemas.master_data import (
    BorrowerCreate,
    BorrowerRead,
    BorrowerUpdate,
    PortfolioCreate,
    PortfolioRead,
    PortfolioUpdate,
)
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.master_data import MasterDataService

_READ = requires(Permission.VIEW_BORROWER)
_WRITE = requires(Permission.CORRECT_SOURCE_DATA)
_READ_DEP = Depends(_READ)
_WRITE_DEP = Depends(_WRITE)


def create_borrowers_router(service: MasterDataService, *, prefix: str = "/api/v1") -> APIRouter:
    """Build the scoped borrower and portfolio endpoints."""
    router = APIRouter(prefix=prefix, tags=["master-data"])

    @router.get("/borrowers", response_model=list[BorrowerRead], name="api_borrower_list")
    async def list_borrowers(
        principal: Principal = _READ_DEP,
        active_only: bool | None = Query(default=None),
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[BorrowerRead]:
        rows = service.list_borrowers(
            principal,
            active_only=active_only,
            limit=limit,
            offset=offset,
        )
        return [_borrower_read(row) for row in rows]

    @router.post(
        "/borrowers",
        response_model=BorrowerRead,
        status_code=status.HTTP_201_CREATED,
        name="api_borrower_create",
    )
    async def create_borrower(
        payload: BorrowerCreate,
        principal: Principal = _WRITE_DEP,
    ) -> BorrowerRead:
        row = service.create_borrower(principal, **payload.model_dump())
        return _borrower_read(row)

    @router.get("/borrowers/{reference}", response_model=BorrowerRead, name="api_borrower_detail")
    async def get_borrower(reference: str, principal: Principal = _READ_DEP) -> BorrowerRead:
        return _borrower_read(service.get_borrower(principal, reference))

    @router.patch("/borrowers/{reference}", response_model=BorrowerRead, name="api_borrower_update")
    async def update_borrower(
        reference: str,
        payload: BorrowerUpdate,
        principal: Principal = _WRITE_DEP,
    ) -> BorrowerRead:
        values = payload.model_dump(exclude={"expected_version"}, exclude_unset=True)
        row = service.update_borrower(
            principal,
            reference,
            expected_version=payload.expected_version,
            **values,
        )
        return _borrower_read(row)

    @router.post(
        "/borrowers/{reference}/deactivate",
        response_model=BorrowerRead,
        name="api_borrower_deactivate",
    )
    async def deactivate_borrower(
        reference: str,
        expected_version: Annotated[int, Query(ge=1)],
        principal: Principal = _WRITE_DEP,
    ) -> BorrowerRead:
        row = service.deactivate_borrower(
            principal,
            reference,
            expected_version=expected_version,
        )
        return _borrower_read(row)

    @router.get("/portfolios", response_model=list[PortfolioRead], name="api_portfolio_list")
    async def list_portfolios(
        principal: Principal = _READ_DEP,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[PortfolioRead]:
        rows = service.list_portfolios(principal, limit=limit, offset=offset)
        return [_portfolio_read(row) for row in rows]

    @router.post(
        "/portfolios",
        response_model=PortfolioRead,
        status_code=status.HTTP_201_CREATED,
        name="api_portfolio_create",
    )
    async def create_portfolio(
        payload: PortfolioCreate,
        principal: Principal = _WRITE_DEP,
    ) -> PortfolioRead:
        row = service.create_portfolio(principal, **payload.model_dump())
        return _portfolio_read(row)

    @router.get(
        "/portfolios/{portfolio_id}", response_model=PortfolioRead, name="api_portfolio_detail"
    )
    async def get_portfolio(portfolio_id: UUID, principal: Principal = _READ_DEP) -> PortfolioRead:
        return _portfolio_read(service.get_portfolio(principal, portfolio_id))

    @router.patch(
        "/portfolios/{portfolio_id}", response_model=PortfolioRead, name="api_portfolio_update"
    )
    async def update_portfolio(
        portfolio_id: UUID,
        payload: PortfolioUpdate,
        principal: Principal = _WRITE_DEP,
    ) -> PortfolioRead:
        values = payload.model_dump(exclude={"expected_version"}, exclude_unset=True)
        row = service.update_portfolio(
            principal,
            portfolio_id,
            expected_version=payload.expected_version,
            **values,
        )
        return _portfolio_read(row)

    return router


def _borrower_read(row: Borrower) -> BorrowerRead:
    return BorrowerRead(
        id=row.id,
        reference=row.reference,
        legal_name=row.legal_name,
        portfolio_id=row.portfolio_id,
        industry_code=row.industry_code,
        group_id=row.group_id,
        constitution=row.constitution,
        incorporation_date=row.incorporation_date,
        is_active=row.is_active,
        version=row.version,
        cin_present=row.cin_fingerprint is not None,
        pan_present=row.pan_enc is not None,
    )


def _portfolio_read(row: Portfolio) -> PortfolioRead:
    return PortfolioRead(
        id=row.id,
        code=row.code,
        name=row.name,
        parent_id=row.parent_id,
        branch_code=row.branch_code,
        path=row.path,
        version=row.version,
    )


__all__ = ["create_borrowers_router"]
