"""Scoped-by-permission, cursor-paginated read-only audit-event API (`C-21`).

Unlike every other `C-21` resource, `AuditEvent` carries no portfolio
ownership column and `db/scoping.py::ownership_path_for` deliberately has no
registered path for it (`spec`'s "append-only audit trail" is visible in
full to `VIEW_AUDIT`, gated only by that permission) â€” the same convention
`web/routes/audit.py`'s HTML search screen already follows. This router does
not add portfolio scoping that the rest of the audit surface intentionally
does not have.

Date filters use UTC calendar-day boundaries, matching the ISO-8601 dates a
machine API client sends; the HTML audit-search screen instead renders
against IST for its human reader, which is a presentation choice this JSON
resource does not need to repeat.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
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
from covenant_radar.api.v1.schemas.audit_events import AuditEventRead
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_READ = requires(Permission.VIEW_AUDIT)
_READ_DEP = Depends(_READ)
_DEFAULT_PREFIX = "/api/v1"


def create_audit_events_router(
    session: Session,
    *,
    prefix: str = _DEFAULT_PREFIX,
    cursor_secret: bytes | str | None = None,
) -> APIRouter:
    """Build the protected, paginated audit-event resource router."""

    if not is_database_session(session):
        raise TypeError("create_audit_events_router requires a SQLAlchemy Session.")
    router = APIRouter(prefix=prefix, tags=["audit-events"])

    @router.get("/audit-events", response_model=list[AuditEventRead], name="api_audit_event_list")
    def list_audit_events(
        principal: Principal = _READ_DEP,
        actor_id: Annotated[UUID | None, Query()] = None,
        subject_type: Annotated[str | None, Query(max_length=50)] = None,
        subject_id: Annotated[UUID | None, Query()] = None,
        event_type: Annotated[str | None, Query(max_length=100)] = None,
        from_date: Annotated[date | None, Query()] = None,
        to_date: Annotated[date | None, Query()] = None,
        cursor: Annotated[str | None, Query()] = None,
        page_size: Annotated[int | None, Query(ge=1)] = None,
    ) -> list[AuditEventRead]:
        size = clamp_page_size(page_size, default=DEFAULT_PAGE_SIZE)
        filters = {
            "actor_id": actor_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "event_type": event_type,
            "from_date": from_date,
            "to_date": to_date,
        }
        statement = select(AuditEvent)
        if actor_id is not None:
            statement = statement.where(AuditEvent.actor_id == actor_id)
        if subject_type is not None:
            statement = statement.where(AuditEvent.subject_type == subject_type)
        if subject_id is not None:
            statement = statement.where(AuditEvent.subject_id == subject_id)
        if event_type is not None:
            statement = statement.where(AuditEvent.event_type == event_type)
        if from_date is not None:
            statement = statement.where(AuditEvent.occurred_at >= _utc_start_of_day(from_date))
        if to_date is not None:
            statement = statement.where(
                AuditEvent.occurred_at < _utc_start_of_day(to_date + timedelta(days=1))
            )

        page = paginate(
            session,
            statement,
            primary_column=AuditEvent.sequence,
            id_column=AuditEvent.id,
            primary_of=lambda row: row.sequence,
            primary_parse=int,
            cursor=cursor,
            filters_digest=digest_filters(filters),
            page_size=size,
            secret=cursor_secret,
        )
        return [_read(row) for row in page.items]

    @router.get(
        "/audit-events/{event_id}", response_model=AuditEventRead, name="api_audit_event_detail"
    )
    def get_audit_event(
        event_id: UUID,
        request: Request,
        response: Response,
        principal: Principal = _READ_DEP,
    ) -> AuditEventRead | Response:
        row = (
            session.execute(select(AuditEvent).where(AuditEvent.id == event_id))
            .scalars()
            .one_or_none()
        )
        if row is None:
            raise NotFound(f"Audit event {event_id} was not found.")
        etag = etag_for_id(row.id)
        if is_not_modified(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return _read(row)

    return router


def _utc_start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _read(row: AuditEvent) -> AuditEventRead:
    return AuditEventRead.model_validate(row, from_attributes=True)


__all__ = ["create_audit_events_router"]
