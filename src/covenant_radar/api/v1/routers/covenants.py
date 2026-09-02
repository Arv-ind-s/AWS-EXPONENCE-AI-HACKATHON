"""Scoped version-one API routes for covenant registration and approval."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from covenant_radar.api.deps import requires
from covenant_radar.api.v1.schemas.covenants import (
    ApprovalDecisionRequest,
    ApprovalRequestRead,
    CovenantActionRead,
    CovenantAmendRequest,
    CovenantCreateRequest,
    CovenantRead,
    CovenantVersionRead,
    WaiverCreateRequest,
    WaiverRead,
)
from covenant_radar.db.models.covenant import Covenant, CovenantVersion, CovenantWaiver
from covenant_radar.security.maker_checker import MakerCheckerRequest
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.registry import RegistryService

_READ = requires(Permission.VIEW_COVENANT)
_REGISTER = requires(Permission.REGISTER_COVENANT)
_APPROVE = requires(Permission.APPROVE_COVENANT)
_WAIVER = requires(Permission.RECORD_WAIVER)
_READ_DEP = Depends(_READ)
_REGISTER_DEP = Depends(_REGISTER)
_APPROVE_DEP = Depends(_APPROVE)
_WAIVER_DEP = Depends(_WAIVER)


def create_covenants_router(service: RegistryService, *, prefix: str = "/api/v1") -> APIRouter:
    """Build the covenant API against one caller-owned registry service."""
    if not isinstance(service, RegistryService):
        raise TypeError("create_covenants_router requires a RegistryService.")
    router = APIRouter(prefix=prefix, tags=["covenants"])

    @router.get("/covenants", response_model=list[CovenantRead], name="api_covenant_list")
    async def list_covenants(
        principal: Principal = _READ_DEP,
        active_only: bool | None = Query(default=True),
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[CovenantRead]:
        rows = service.list_covenants(
            principal,
            active_only=active_only,
            limit=limit,
            offset=offset,
        )
        return [_covenant_read(row) for row in rows]

    @router.get("/covenants/{reference}", response_model=CovenantRead, name="api_covenant_detail")
    async def get_covenant(
        reference: str,
        principal: Principal = _READ_DEP,
    ) -> CovenantRead:
        row = service.get_covenant(principal, reference)
        return _covenant_read(row, versions=service.list_versions(principal, row.id))

    @router.post(
        "/covenants",
        response_model=CovenantActionRead,
        status_code=status.HTTP_201_CREATED,
        name="api_covenant_register",
    )
    async def register_covenant(
        payload: CovenantCreateRequest,
        principal: Principal = _REGISTER_DEP,
    ) -> CovenantActionRead:
        result = service.register(
            principal,
            facility_id=payload.facility_id,
            reference=payload.reference,
            name=payload.name,
            covenant_class=payload.covenant_class,
            terms=payload.to_domain(),
        )
        return _action_read(result.covenant, result.version, result.approval_request)

    @router.post(
        "/covenants/{reference}/amend",
        response_model=CovenantActionRead,
        status_code=status.HTTP_201_CREATED,
        name="api_covenant_amend",
    )
    async def amend_covenant(
        reference: str,
        payload: CovenantAmendRequest,
        principal: Principal = _REGISTER_DEP,
    ) -> CovenantActionRead:
        result = service.amend(principal, reference, terms=payload.to_domain())
        return _action_read(
            service.get_covenant(principal, reference),
            result.version,
            result.approval_request,
        )

    @router.post(
        "/covenants/{reference}/retire",
        response_model=CovenantActionRead,
        name="api_covenant_retire",
    )
    async def retire_covenant(
        reference: str,
        principal: Principal = _REGISTER_DEP,
    ) -> CovenantActionRead:
        result = service.retire(principal, reference)
        return _action_read(result.covenant, result.version, result.approval_request)

    @router.post(
        "/covenants/{reference}/waivers",
        response_model=WaiverRead,
        status_code=status.HTTP_201_CREATED,
        name="api_covenant_waiver_request",
    )
    async def request_waiver(
        reference: str,
        payload: WaiverCreateRequest,
        principal: Principal = _WAIVER_DEP,
    ) -> WaiverRead:
        waiver = service.request_waiver(
            principal,
            reference,
            from_date=payload.from_date,
            to_date=payload.to_date,
            reason=payload.reason,
            waiver_scope=payload.waiver_scope,
            document_id=payload.document_id,
        )
        return _waiver_read(waiver)

    @router.get(
        "/covenant-approvals",
        response_model=list[ApprovalRequestRead],
        name="api_covenant_approval_list",
    )
    async def list_approvals(principal: Principal = _APPROVE_DEP) -> list[ApprovalRequestRead]:
        return [_approval_read(request) for request in service.pending_approvals(principal)]

    @router.post(
        "/covenant-approvals/{request_id}/decide",
        response_model=ApprovalRequestRead,
        name="api_covenant_approval_decide",
    )
    async def decide_approval(
        request_id: UUID,
        payload: ApprovalDecisionRequest,
        principal: Principal = _APPROVE_DEP,
    ) -> ApprovalRequestRead:
        request = service.decide_approval(
            principal,
            request_id,
            approved=payload.is_approved,
            reason=payload.reason,
        )
        return _approval_read(request)

    @router.post(
        "/covenants/{reference}/approve",
        response_model=ApprovalRequestRead,
        name="api_covenant_approve",
    )
    async def approve_covenant(
        reference: str,
        payload: ApprovalDecisionRequest,
        principal: Principal = _APPROVE_DEP,
    ) -> ApprovalRequestRead:
        request = service.approve_covenant(
            principal,
            reference,
            approved=payload.is_approved,
            reason=payload.reason,
        )
        return _approval_read(request)

    return router


def _covenant_read(
    row: Covenant, *, versions: tuple[CovenantVersion, ...] | list[CovenantVersion] = ()
) -> CovenantRead:
    return CovenantRead(
        id=row.id,
        reference=row.reference,
        facility_id=row.facility_id,
        name=row.name,
        covenant_class=row.covenant_class,
        is_active=row.is_active,
        version=row.version,
        versions=[_version_read(version) for version in versions],
    )


def _version_read(row: CovenantVersion) -> CovenantVersionRead:
    return CovenantVersionRead.model_validate(row, from_attributes=True)


def _action_read(
    covenant: Covenant,
    version: CovenantVersion,
    request: MakerCheckerRequest | None,
) -> CovenantActionRead:
    return CovenantActionRead(
        covenant=_covenant_read(covenant),
        version=_version_read(version),
        approval_request_id=request.id if request is not None else None,
        state=request.state.value if request is not None else version.status,
    )


def _approval_read(request: MakerCheckerRequest) -> ApprovalRequestRead:
    return ApprovalRequestRead.model_validate(request, from_attributes=True)


def _waiver_read(row: CovenantWaiver) -> WaiverRead:
    return WaiverRead.model_validate(row, from_attributes=True)


__all__ = ["create_covenants_router"]
