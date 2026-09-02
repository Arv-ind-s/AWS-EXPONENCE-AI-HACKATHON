"""Authenticated JSON routes for saved views and recent navigation.

The browser queue remains server-rendered, while these small resource routes
provide one stable interface for the queue controls and progressive-enhancement
clients.  They return filter definitions, never result rows captured under a
different user's scope.
"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, scoped_session

from covenant_radar.api.deps import requires
from covenant_radar.db.repositories.view import ViewRecord
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.views import AppliedView, RecentItem, ViewService

_READ = requires(Permission.VIEW_QUEUE)
_READ_DEP = Depends(_READ)
_MAX_FILTER_FIELDS = 20


class ViewCreatePayload(BaseModel):
    """Validated saved-view creation payload."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: Annotated[str, Field(pattern="^(queue|case|search)$")] = "queue"
    filters: dict[str, object] = Field(default_factory=dict, max_length=_MAX_FILTER_FIELDS)
    shared_user_ids: list[UUID] = Field(default_factory=list, max_length=100)
    shared_role_codes: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(
        default_factory=list, max_length=20
    )
    share_all: bool = False
    is_default: bool = False
    description: Annotated[str, Field(max_length=500)] | None = None


class ViewSharePayload(BaseModel):
    """Validated replacement sharing policy."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    shared_user_ids: list[UUID] = Field(default_factory=list, max_length=100)
    shared_role_codes: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(
        default_factory=list, max_length=20
    )
    share_all: bool = False


class ViewUpdatePayload(BaseModel):
    """Validated optimistic-concurrency update payload."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: Annotated[int, Field(ge=1)]
    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    kind: Annotated[str, Field(pattern="^(queue|case|search)$")] | None = None
    filters: dict[str, object] | None = Field(default=None, max_length=_MAX_FILTER_FIELDS)
    description: Annotated[str, Field(max_length=500)] | None = None


class RecentItemPayload(BaseModel):
    """Validated recent subject reference."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject_type: Annotated[str, Field(min_length=1, max_length=30)]
    subject_id: UUID


def create_views_router(
    service_or_session: ViewService | Session | scoped_session[Session],
    *,
    service: ViewService | None = None,
) -> APIRouter:
    """Build the protected saved-view and recent-item resource routes."""
    if service is not None:
        if not isinstance(service, ViewService):
            raise TypeError("views service must be a ViewService.")
        view_service = service
    elif isinstance(service_or_session, ViewService):
        view_service = service_or_session
    elif is_database_session(service_or_session):
        view_service = ViewService(cast(Session, service_or_session))
    else:
        raise TypeError("create_views_router requires a ViewService or SQLAlchemy Session.")

    router = APIRouter(tags=["views"])

    @router.get("/views", response_class=JSONResponse, name="views_list")
    def list_views(principal: Principal = _READ_DEP) -> JSONResponse:
        views = view_service.list_views(principal)
        default = next((view for view in views if view.is_default), None)
        recent = view_service.recent_items(principal, limit=25)
        return JSONResponse(
            {
                "views": [_view_payload(view) for view in views],
                "default_view_id": str(default.id) if default is not None else None,
                "recent_items": [_recent_payload(item) for item in recent],
            }
        )

    @router.get("/views/default", response_class=JSONResponse, name="views_default")
    def default_view(principal: Principal = _READ_DEP) -> JSONResponse:
        view = view_service.default_view(principal)
        return JSONResponse(_view_payload(view))

    @router.get("/views/{view_id}", response_class=JSONResponse, name="views_detail")
    def get_view(view_id: UUID, principal: Principal = _READ_DEP) -> JSONResponse:
        view = view_service.get_view(principal, view_id)
        return JSONResponse(_view_payload(view))

    @router.post(
        "/views",
        response_class=JSONResponse,
        status_code=status.HTTP_201_CREATED,
        name="views_create",
    )
    def create_view(
        payload: ViewCreatePayload,
        principal: Principal = _READ_DEP,
    ) -> JSONResponse:
        view = view_service.create(
            principal,
            name=payload.name,
            filters=payload.filters,
            kind=payload.kind,
            shared_user_ids=payload.shared_user_ids,
            shared_role_codes=payload.shared_role_codes,
            share_all=payload.share_all,
            is_default=payload.is_default,
            description=payload.description,
        )
        return JSONResponse(_view_payload(view), status_code=status.HTTP_201_CREATED)

    @router.post("/views/{view_id}/share", response_class=JSONResponse, name="views_share")
    def share_view(
        view_id: UUID,
        payload: ViewSharePayload,
        principal: Principal = _READ_DEP,
    ) -> JSONResponse:
        view = view_service.share(
            principal,
            view_id,
            user_ids=payload.shared_user_ids,
            role_codes=payload.shared_role_codes,
            share_all=payload.share_all,
        )
        return JSONResponse(_view_payload(view))

    @router.patch("/views/{view_id}", response_class=JSONResponse, name="views_update")
    def update_view(
        view_id: UUID,
        payload: ViewUpdatePayload,
        principal: Principal = _READ_DEP,
    ) -> JSONResponse:
        view = view_service.update(
            principal,
            view_id,
            expected_version=payload.expected_version,
            name=payload.name,
            filters=payload.filters,
            kind=payload.kind,
            description=payload.description,
        )
        return JSONResponse(_view_payload(view))

    @router.post("/views/{view_id}/default", response_class=JSONResponse, name="views_set_default")
    def set_default(view_id: UUID, principal: Principal = _READ_DEP) -> JSONResponse:
        view = view_service.set_default(principal, view_id)
        return JSONResponse(_view_payload(view))

    @router.delete("/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT, name="views_delete")
    def delete_view(view_id: UUID, principal: Principal = _READ_DEP) -> Response:
        view_service.delete(principal, view_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/recent-items", response_class=JSONResponse, name="recent_items")
    def recent_items(principal: Principal = _READ_DEP) -> JSONResponse:
        return JSONResponse(
            {
                "recent_items": [
                    _recent_payload(item) for item in view_service.recent_items(principal)
                ]
            }
        )

    @router.post("/recent-items", status_code=status.HTTP_204_NO_CONTENT, name="recent_item_open")
    def recent_item(
        payload: RecentItemPayload,
        principal: Principal = _READ_DEP,
    ) -> Response:
        view_service.record_recent_item(
            principal,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _view_payload(view: AppliedView | ViewRecord) -> dict[str, object]:
    if isinstance(view, AppliedView):
        record = view.view
        filters = view.filters
        notice = view.notice
        dropped_filters = view.dropped_filters
    else:
        record = view
        filters = view.filters
        notice = None
        dropped_filters = ()
    return {
        "id": str(record.id),
        "owner_id": str(record.owner_id),
        "name": record.name,
        "kind": record.kind,
        "filters": dict(filters),
        "is_shared": record.is_shared,
        "shared_user_ids": [str(value) for value in record.shared_user_ids],
        "shared_role_codes": list(record.shared_role_codes),
        "share_all": record.share_all,
        "is_default": record.is_default,
        "description": record.description,
        "version": record.version,
        "notice": notice,
        "dropped_filters": list(dropped_filters),
    }


def _recent_payload(item: RecentItem) -> dict[str, object]:
    return {
        "subject_type": item.subject_type,
        "subject_id": str(item.subject_id),
        "label": item.label,
        "title": item.title,
        "href": item.href,
        "opened_at": item.opened_at.isoformat(),
    }


__all__ = [
    "RecentItemPayload",
    "ViewCreatePayload",
    "ViewSharePayload",
    "ViewUpdatePayload",
    "create_views_router",
]
