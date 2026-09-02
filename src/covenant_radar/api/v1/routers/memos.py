"""Scoped, cursor-paginated read-only memo API (`C-21`)."""

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
from covenant_radar.api.v1.schemas.memos import MemoRead
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.workflow import Memo
from covenant_radar.db.scoping import ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_READ = requires(Permission.VIEW_MEMO)
_READ_DEP = Depends(_READ)
_DEFAULT_PREFIX = "/api/v1"


def create_memos_router(
    session: Session,
    *,
    prefix: str = _DEFAULT_PREFIX,
    cursor_secret: bytes | str | None = None,
) -> APIRouter:
    """Build the protected, scoped, paginated memo resource router."""

    if not is_database_session(session):
        raise TypeError("create_memos_router requires a SQLAlchemy Session.")
    router = APIRouter(prefix=prefix, tags=["memos"])
    ownership = ownership_path_for(Memo)

    @router.get("/memos", response_model=list[MemoRead], name="api_memo_list")
    def list_memos(
        principal: Principal = _READ_DEP,
        borrower_id: Annotated[UUID | None, Query()] = None,
        case_id: Annotated[UUID | None, Query()] = None,
        cursor: Annotated[str | None, Query()] = None,
        page_size: Annotated[int | None, Query(ge=1)] = None,
    ) -> list[MemoRead]:
        scope = resolve_scope(principal, session)
        size = clamp_page_size(page_size, default=DEFAULT_PAGE_SIZE)
        filters = {"borrower_id": borrower_id, "case_id": case_id}
        statement = ownership.apply(select(Memo)).where(scope.predicate(ownership.path_column))
        if borrower_id is not None:
            statement = statement.where(Memo.borrower_id == borrower_id)
        if case_id is not None:
            statement = statement.where(Memo.case_id == case_id)

        page = paginate(
            session,
            statement,
            primary_column=Memo.created_at,
            id_column=Memo.id,
            primary_of=lambda row: row.created_at,
            primary_parse=datetime.fromisoformat,
            cursor=cursor,
            filters_digest=digest_filters(filters),
            page_size=size,
            secret=cursor_secret,
        )
        return [_read(row) for row in page.items]

    @router.get("/memos/{memo_id}", response_model=MemoRead, name="api_memo_detail")
    def get_memo(
        memo_id: UUID,
        request: Request,
        response: Response,
        principal: Principal = _READ_DEP,
    ) -> MemoRead | Response:
        scope = resolve_scope(principal, session)
        statement = ownership.apply(select(Memo)).where(
            Memo.id == memo_id, scope.predicate(ownership.path_column)
        )
        row = session.execute(statement).scalars().one_or_none()
        if row is None:
            raise NotFound(f"Memo {memo_id} was not found within the current scope.")
        etag = etag_for_version(row.version)
        if is_not_modified(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return _read(row)

    return router


def _read(row: Memo) -> MemoRead:
    return MemoRead.model_validate(row, from_attributes=True)


__all__ = ["create_memos_router"]
