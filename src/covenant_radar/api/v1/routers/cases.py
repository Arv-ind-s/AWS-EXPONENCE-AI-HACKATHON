"""Scoped, cursor-paginated read-only case API (`C-21`)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.api.conditional import etag_for_version, is_not_modified
from covenant_radar.api.deps import requires
from covenant_radar.api.pagination import (
    DEFAULT_PAGE_SIZE,
    clamp_page_size,
    digest_filters,
    paginate,
)
from covenant_radar.api.v1.schemas.cases import CaseRead
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.workflow import Case
from covenant_radar.db.scoping import ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_READ = requires(Permission.VIEW_CASE)
_READ_DEP = Depends(_READ)
_DEFAULT_PREFIX = "/api/v1"


def create_cases_router(
    session: Session,
    *,
    prefix: str = _DEFAULT_PREFIX,
    cursor_secret: bytes | str | None = None,
) -> APIRouter:
    """Build the protected, scoped, paginated case resource router."""

    if not is_database_session(session):
        raise TypeError("create_cases_router requires a SQLAlchemy Session.")
    router = APIRouter(prefix=prefix, tags=["cases"])
    ownership = ownership_path_for(Case)

    @router.get("/cases", response_model=list[CaseRead], name="api_case_list")
    def list_cases(
        principal: Principal = _READ_DEP,
        borrower_id: Annotated[UUID | None, Query()] = None,
        state: Annotated[str | None, Query(max_length=20)] = None,
        assignee_id: Annotated[UUID | None, Query()] = None,
        cursor: Annotated[str | None, Query()] = None,
        page_size: Annotated[int | None, Query(ge=1)] = None,
    ) -> list[CaseRead]:
        scope = resolve_scope(principal, session)
        size = clamp_page_size(page_size, default=DEFAULT_PAGE_SIZE)
        filters = {"borrower_id": borrower_id, "state": state, "assignee_id": assignee_id}
        statement = ownership.apply(select(Case)).where(scope.predicate(ownership.path_column))
        if borrower_id is not None:
            statement = statement.where(Case.borrower_id == borrower_id)
        if state is not None:
            statement = statement.where(Case.state == state)
        if assignee_id is not None:
            statement = statement.where(Case.assignee_id == assignee_id)

        page = paginate(
            session,
            statement,
            primary_column=Case.updated_at,
            id_column=Case.id,
            primary_of=lambda row: row.updated_at,
            primary_parse=datetime.fromisoformat,
            cursor=cursor,
            filters_digest=digest_filters(filters),
            page_size=size,
            secret=cursor_secret,
        )
        return [_read(row) for row in page.items]

    @router.get("/cases/{case_id}", response_model=CaseRead, name="api_case_detail")
    def get_case(
        case_id: UUID,
        request: Request,
        response: Response,
        principal: Principal = _READ_DEP,
    ) -> CaseRead | Response:
        scope = resolve_scope(principal, session)
        statement = ownership.apply(select(Case)).where(
            Case.id == case_id, scope.predicate(ownership.path_column)
        )
        row = session.execute(statement).scalars().one_or_none()
        if row is None:
            raise NotFound(f"Case {case_id} was not found within the current scope.")
        etag = etag_for_version(row.version)
        if is_not_modified(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return _read(row)

    return router


def _read(row: Case) -> CaseRead:
    return CaseRead.model_validate(row, from_attributes=True)


__all__ = ["create_cases_router"]
