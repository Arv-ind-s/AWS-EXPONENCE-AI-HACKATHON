"""Scoped, cursor-paginated read-only evidence API (`C-21`).

Evidence is a mutable ledger (`db/models/signal.py::EvidenceItem` carries
`VersionedColumns`), scored and superseded by the signal-scoring pipeline;
this router only reads it, matching the session-direct pattern
`api/v1/routers/forecast.py` and `api/v1/routers/explain.py` already use.
"""

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
from covenant_radar.api.v1.schemas.evidence import EvidenceItemRead
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.db.scoping import ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_READ = requires(Permission.VIEW_EVIDENCE)
_READ_DEP = Depends(_READ)
_DEFAULT_PREFIX = "/api/v1"


def create_evidence_router(
    session: Session,
    *,
    prefix: str = _DEFAULT_PREFIX,
    cursor_secret: bytes | str | None = None,
) -> APIRouter:
    """Build the protected, scoped, paginated evidence resource router."""

    if not is_database_session(session):
        raise TypeError("create_evidence_router requires a SQLAlchemy Session.")
    router = APIRouter(prefix=prefix, tags=["evidence"])
    ownership = ownership_path_for(EvidenceItem)

    @router.get("/evidence", response_model=list[EvidenceItemRead], name="api_evidence_list")
    def list_evidence(
        principal: Principal = _READ_DEP,
        borrower_id: Annotated[UUID | None, Query()] = None,
        facility_id: Annotated[UUID | None, Query()] = None,
        family: Annotated[str | None, Query(max_length=50)] = None,
        state: Annotated[str | None, Query(max_length=20)] = None,
        cursor: Annotated[str | None, Query()] = None,
        page_size: Annotated[int | None, Query(ge=1)] = None,
    ) -> list[EvidenceItemRead]:
        scope = resolve_scope(principal, session)
        size = clamp_page_size(page_size, default=DEFAULT_PAGE_SIZE)
        filters = {
            "borrower_id": borrower_id,
            "facility_id": facility_id,
            "family": family,
            "state": state,
        }
        statement = ownership.apply(select(EvidenceItem)).where(
            scope.predicate(ownership.path_column)
        )
        if borrower_id is not None:
            statement = statement.where(EvidenceItem.borrower_id == borrower_id)
        if facility_id is not None:
            statement = statement.where(EvidenceItem.facility_id == facility_id)
        if family is not None:
            statement = statement.where(EvidenceItem.family == family)
        if state is not None:
            statement = statement.where(EvidenceItem.state == state)

        page = paginate(
            session,
            statement,
            primary_column=EvidenceItem.updated_at,
            id_column=EvidenceItem.id,
            primary_of=lambda row: row.updated_at,
            primary_parse=datetime.fromisoformat,
            cursor=cursor,
            filters_digest=digest_filters(filters),
            page_size=size,
            secret=cursor_secret,
        )
        return [_read(row) for row in page.items]

    @router.get(
        "/evidence/{evidence_id}", response_model=EvidenceItemRead, name="api_evidence_detail"
    )
    def get_evidence(
        evidence_id: UUID,
        request: Request,
        response: Response,
        principal: Principal = _READ_DEP,
    ) -> EvidenceItemRead | Response:
        scope = resolve_scope(principal, session)
        statement = ownership.apply(select(EvidenceItem)).where(
            EvidenceItem.id == evidence_id, scope.predicate(ownership.path_column)
        )
        row = session.execute(statement).scalars().one_or_none()
        if row is None:
            raise NotFound(f"Evidence item {evidence_id} was not found within the current scope.")
        etag = etag_for_version(row.version)
        if is_not_modified(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return _read(row)

    return router


def _read(row: EvidenceItem) -> EvidenceItemRead:
    return EvidenceItemRead.model_validate(row, from_attributes=True)


__all__ = ["create_evidence_router"]
