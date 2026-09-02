"""Scoped, cursor-paginated read-only covenant-test API (`C-21`).

Covenant tests are written once by the engine that computed them and never
edited (`db/models/covenant.py::CovenantTest`), so this router is a thin,
scope-enforcing read over that table â€” no service layer sits in front of it,
matching the session-direct pattern `api/v1/routers/forecast.py` and
`api/v1/routers/explain.py` already use for read-only resources with no
write counterpart of their own.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.api.conditional import etag_for_id, is_not_modified
from covenant_radar.api.deps import requires
from covenant_radar.api.pagination import (
    DEFAULT_PAGE_SIZE,
    clamp_page_size,
    digest_filters,
    paginate,
)
from covenant_radar.api.v1.schemas.covenant_tests import CovenantTestRead
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.covenant import CovenantTest
from covenant_radar.db.scoping import ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_READ = requires(Permission.VIEW_COVENANT)
_READ_DEP = Depends(_READ)
_DEFAULT_PREFIX = "/api/v1"


def create_covenant_tests_router(
    session: Session,
    *,
    prefix: str = _DEFAULT_PREFIX,
    cursor_secret: bytes | str | None = None,
) -> APIRouter:
    """Build the protected, scoped, paginated covenant-test resource router."""

    if not is_database_session(session):
        raise TypeError("create_covenant_tests_router requires a SQLAlchemy Session.")
    router = APIRouter(prefix=prefix, tags=["tests"])
    ownership = ownership_path_for(CovenantTest)

    @router.get("/tests", response_model=list[CovenantTestRead], name="api_test_list")
    def list_tests(
        principal: Principal = _READ_DEP,
        covenant_version_id: Annotated[UUID | None, Query()] = None,
        verdict: Annotated[str | None, Query(max_length=32)] = None,
        from_date: Annotated[date | None, Query()] = None,
        to_date: Annotated[date | None, Query()] = None,
        cursor: Annotated[str | None, Query()] = None,
        page_size: Annotated[int | None, Query(ge=1)] = None,
    ) -> list[CovenantTestRead]:
        scope = resolve_scope(principal, session)
        size = clamp_page_size(page_size, default=DEFAULT_PAGE_SIZE)
        filters = {
            "covenant_version_id": covenant_version_id,
            "verdict": verdict,
            "from_date": from_date,
            "to_date": to_date,
        }
        statement = ownership.apply(select(CovenantTest)).where(
            scope.predicate(ownership.path_column)
        )
        if covenant_version_id is not None:
            statement = statement.where(CovenantTest.covenant_version_id == covenant_version_id)
        if verdict is not None:
            statement = statement.where(CovenantTest.verdict == verdict)
        if from_date is not None:
            statement = statement.where(CovenantTest.as_of_date >= from_date)
        if to_date is not None:
            statement = statement.where(CovenantTest.as_of_date <= to_date)

        page = paginate(
            session,
            statement,
            primary_column=CovenantTest.as_of_date,
            id_column=CovenantTest.id,
            primary_of=lambda row: row.as_of_date,
            primary_parse=date.fromisoformat,
            cursor=cursor,
            filters_digest=digest_filters(filters),
            page_size=size,
            secret=cursor_secret,
        )
        return [_read(row) for row in page.items]

    @router.get("/tests/{test_id}", response_model=CovenantTestRead, name="api_test_detail")
    def get_test(
        test_id: UUID,
        request: Request,
        response: Response,
        principal: Principal = _READ_DEP,
    ) -> CovenantTestRead | Response:
        scope = resolve_scope(principal, session)
        statement = ownership.apply(select(CovenantTest)).where(
            CovenantTest.id == test_id, scope.predicate(ownership.path_column)
        )
        row = session.execute(statement).scalars().one_or_none()
        if row is None:
            raise NotFound(f"Covenant test {test_id} was not found within the current scope.")
        etag = etag_for_id(row.id)
        if is_not_modified(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return _read(row)

    return router


def _read(row: CovenantTest) -> CovenantTestRead:
    return CovenantTestRead.model_validate(row, from_attributes=True)


__all__ = ["create_covenant_tests_router"]
