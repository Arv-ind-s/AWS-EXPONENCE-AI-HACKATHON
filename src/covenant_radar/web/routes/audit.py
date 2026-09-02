"""Read-only audit search, warning reconstruction, and evidence export routes.

The search surface reads the append-only audit repository directly so filters
and seek pagination are applied in SQL.  Warning reconstruction and bundle
creation remain service responsibilities; this module only resolves the
caller, chooses the presentation state, and returns browser responses.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.audit.events import AuditEventType
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.errors import Conflict, ExternalServiceError, NotFound, ValidationError
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize
from covenant_radar.services.reconstruction import EvidenceBundleExportResult, ReconstructionService
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.audit import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AuditCursor,
    AuditFilters,
    InvalidAuditCursor,
    build_audit_search_view,
    build_reconstruction_view,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_READ = requires(Permission.VIEW_AUDIT)
_EXPORT = requires(Permission.EXPORT_EVIDENCE)
_READ_DEP = Depends(_READ)
_EXPORT_DEP = Depends(_EXPORT)
_MAX_FILTER_LENGTH = 200
_MAX_EXPORT_FILENAME_LENGTH = 120
MAX_EXPORT_ROWS = 10_000
_SEARCH_EXPORT_EVENT = AuditEventType.EVIDENCE_BUNDLE_EXPORTED.value
_AUDIT_TIMEZONE = ZoneInfo("Asia/Kolkata")


def create_audit_router(
    source: Session | ReconstructionService,
    *,
    reconstruction_service: ReconstructionService | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
    cursor_secret: bytes | str | None = None,
    scope_resolver: Callable[[Principal], Scope] | None = None,
    audit_writer: object | None = None,
) -> APIRouter:
    """Build the protected audit routes over a database session.

    ``source`` accepts either the session used by the web application or an
    existing ``ReconstructionService``.  The latter keeps the route easy to
    wire when the service already has document storage, notification, or
    asynchronous-worker adapters configured.
    """

    if isinstance(source, ReconstructionService):
        session = source.session
        service = reconstruction_service or source
    elif is_database_session(source):
        session = source
        service = reconstruction_service or ReconstructionService(session)
    else:
        raise TypeError(
            "create_audit_router requires a SQLAlchemy Session or ReconstructionService."
        )
    if not isinstance(service, ReconstructionService):
        raise TypeError("reconstruction_service must be a ReconstructionService.")
    if scope_resolver is not None and not callable(scope_resolver):
        raise TypeError("scope_resolver must be callable.")
    if audit_writer is not None and not callable(getattr(audit_writer, "record", None)):
        raise TypeError("audit_writer must provide a callable record method.")

    router = APIRouter(tags=["audit-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )
    repository = AuditRepository(session)
    bundle_results: dict[str, EvidenceBundleExportResult] = {}

    @router.get("/audit", response_class=HTMLResponse, name="audit_search")
    def audit_search(
        request: Request,
        principal: Principal = _READ_DEP,
        page_size: int | None = Query(None, ge=1, le=MAX_PAGE_SIZE),
    ) -> Response:
        return _search_response(
            request,
            principal=principal,
            repository=repository,
            fallback_environment=fallback_environment,
            cursor_secret=cursor_secret,
            audit_writer=audit_writer,
            page_size=page_size or DEFAULT_PAGE_SIZE,
            force_export=False,
        )

    @router.get("/audit/export", response_class=Response, name="audit_search_export")
    def audit_search_export(
        request: Request,
        principal: Principal = _EXPORT_DEP,
        page_size: int | None = Query(None, ge=1, le=MAX_PAGE_SIZE),
    ) -> Response:
        return _search_response(
            request,
            principal=principal,
            repository=repository,
            fallback_environment=fallback_environment,
            cursor_secret=cursor_secret,
            audit_writer=audit_writer,
            page_size=page_size or DEFAULT_PAGE_SIZE,
            force_export=True,
        )

    @router.get(
        "/audit/warnings/{forecast_id}",
        response_class=HTMLResponse,
        name="audit_warning_reconstruction",
    )
    def audit_warning_reconstruction(
        request: Request,
        forecast_id: UUID,
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        scope = _scope_for(principal, session, scope_resolver)
        reconstruction = service.reconstruct(principal, forecast_id, scope=scope)
        chain_status = repository.verify_chain()
        view = build_reconstruction_view(
            reconstruction,
            chain_status=chain_status,
            can_export=principal.has(Permission.EXPORT_EVIDENCE),
        )
        return _render(
            request,
            fallback_environment,
            "screens/audit/reconstruction.html",
            principal=principal,
            view=view,
        )

    @router.post(
        "/audit/warnings/{forecast_id}/bundle",
        response_class=HTMLResponse,
        name="audit_warning_bundle",
    )
    def audit_warning_bundle(
        request: Request,
        forecast_id: UUID,
        principal: Principal = _EXPORT_DEP,
    ) -> HTMLResponse:
        scope = _scope_for(principal, session, scope_resolver)
        result = service.export_bundle(
            principal,
            forecast_id,
            scope=scope,
            request_id=_request_id(request),
        )
        bundle_id = str(result.bundle_id)
        bundle_results[bundle_id] = result
        location = f"/audit/bundles/{quote(bundle_id, safe='')}"
        response = _render(
            request,
            fallback_environment,
            "screens/audit/bundle_status.html",
            principal=principal,
            bundle_id=bundle_id,
            bundle_status=result.status,
            bundle_location=location,
            manifest_hash=result.manifest_hash,
            status_code=202,
        )
        response.headers["Location"] = location
        response.headers["X-Evidence-Bundle-ID"] = bundle_id
        return response

    @router.get(
        "/audit/bundles/{bundle_id}",
        response_class=HTMLResponse,
        name="audit_bundle_download",
    )
    def audit_bundle_download(
        request: Request,
        bundle_id: str,
        principal: Principal = _EXPORT_DEP,
    ) -> Response:
        result = bundle_results.get(bundle_id)
        if result is None:
            raise NotFound("The requested evidence bundle was not found.")
        if result.future is not None:
            if not result.future.done():
                location = f"/audit/bundles/{quote(bundle_id, safe='')}"
                return _render(
                    request,
                    fallback_environment,
                    "screens/audit/bundle_status.html",
                    principal=principal,
                    bundle_id=bundle_id,
                    bundle_status="queued",
                    bundle_location=location,
                    manifest_hash=None,
                    status_code=202,
                )
            try:
                result = result.result()
            except Exception as error:  # noqa: BLE001 - do not expose worker internals
                raise ExternalServiceError(
                    "The evidence bundle could not be produced. Contact an administrator."
                ) from error
            bundle_results[bundle_id] = result
        if result.status != "complete" or result.content is None:
            raise ExternalServiceError("The evidence bundle is not available yet.")
        filename = _safe_filename(result.filename)
        return Response(
            content=result.content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Evidence-Bundle-ID": bundle_id,
                "X-Manifest-Hash": result.manifest_hash or "",
                "X-Request-ID": _request_id(request),
            },
        )

    return router


def _search_response(
    request: Request,
    *,
    principal: Principal,
    repository: AuditRepository,
    fallback_environment: Environment,
    cursor_secret: bytes | str | None,
    audit_writer: object | None,
    page_size: int,
    force_export: bool,
) -> Response:
    query = request.query_params
    filters = _filters_from_query(query)
    requested_page_size = _optional_query_int(query, ("page_size", "limit"))
    if requested_page_size is not None:
        if "page_size" not in query and requested_page_size != page_size:
            page_size = requested_page_size
        elif "page_size" in query and requested_page_size != page_size:
            raise ValidationError(
                "page_size was supplied more than once with different values.", field="page_size"
            )
    requested_export = _export_requested(query, force_export)
    if requested_export:
        authorize(principal, Permission.EXPORT_EVIDENCE)
        export_rows = _all_matching_rows(repository.session, filters)
        _record_search_export(
            request,
            principal=principal,
            audit_writer=audit_writer,
            filters=filters,
            row_count=len(export_rows),
            session=repository.session,
        )
        return _csv_response(page_rows=export_rows, request=request)

    cursor_token = _single_query_value(query, ("cursor",))
    position = _decode_cursor(cursor_token, cursor_secret)
    if position is not None and position.filters_digest != filters.digest():
        raise Conflict("Audit search filters changed; reload the audit search.")

    statement = _audit_statement(filters, position=position, limit=page_size + 1)
    rows = tuple(repository.session.execute(statement).scalars().all())
    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    next_cursor = None
    if has_more:
        next_cursor = AuditCursor(
            sequence=page_rows[-1].sequence,
            filters_digest=filters.digest(),
        ).encode(cursor_secret)
    total_count = _audit_count(repository.session, filters)

    view = build_audit_search_view(
        page_rows,
        filters=filters,
        total_count=total_count,
        next_cursor=next_cursor,
        page_size=page_size,
        chain_status=repository.verify_chain(),
    )
    return _render(
        request,
        fallback_environment,
        "screens/audit/index.html",
        principal=principal,
        view=view,
        can_export=principal.has(Permission.EXPORT_EVIDENCE),
    )


def _audit_statement(
    filters: AuditFilters,
    *,
    position: AuditCursor | None,
    limit: int,
) -> Select[tuple[AuditEvent]]:
    statement: Select[tuple[AuditEvent]] = select(AuditEvent)
    predicates = _filter_predicates(filters)
    if position is not None:
        predicates.append(AuditEvent.sequence < position.sequence)
    if predicates:
        statement = statement.where(and_(*predicates))
    return statement.order_by(AuditEvent.sequence.desc()).limit(limit)


def _audit_count(session: Session, filters: AuditFilters) -> int:
    statement = select(func.count()).select_from(AuditEvent)
    predicates = _filter_predicates(filters)
    if predicates:
        statement = statement.where(and_(*predicates))
    count: object = session.scalar(statement)
    if isinstance(count, bool) or not isinstance(count, int):
        raise ExternalServiceError("The audit search returned an invalid row count.")
    return cast(int, count)


def _all_matching_rows(session: Session, filters: AuditFilters) -> tuple[AuditEvent, ...]:
    statement = _audit_statement(filters, position=None, limit=MAX_EXPORT_ROWS + 1)
    rows = tuple(session.execute(statement).scalars().all())
    if len(rows) > MAX_EXPORT_ROWS:
        raise ValidationError(
            f"The audit export exceeds the maximum of {MAX_EXPORT_ROWS} rows; narrow the filters.",
            field="export",
        )
    return rows


def _filter_predicates(filters: AuditFilters) -> list[object]:
    predicates: list[object] = []
    if filters.actor:
        actor_id = _optional_uuid(filters.actor)
        predicates.append(
            AuditEvent.actor_id == actor_id
            if actor_id is not None
            else AuditEvent.actor_label == filters.actor
        )
    if filters.subject:
        subject_id = _required_uuid_param(filters.subject, "subject")
        predicates.append(AuditEvent.subject_id == subject_id)
    if filters.subject_type:
        predicates.append(AuditEvent.subject_type == filters.subject_type)
    if filters.event_type:
        predicates.append(AuditEvent.event_type == filters.event_type)
    if filters.event_id:
        try:
            event_id = UUID(filters.event_id)
        except ValueError:
            try:
                sequence = int(filters.event_id)
            except ValueError as error:
                raise ValidationError(
                    "event_id must be a UUID or sequence number.", field="event_id"
                ) from error
            if sequence < 1:
                raise ValidationError(
                    "event_id sequence must be positive.", field="event_id"
                ) from None
            predicates.append(AuditEvent.sequence == sequence)
        else:
            predicates.append(AuditEvent.id == event_id)
    if filters.from_date is not None:
        predicates.append(AuditEvent.occurred_at >= _utc_start_of_ist_date(filters.from_date))
    if filters.to_date is not None:
        predicates.append(
            AuditEvent.occurred_at < _utc_start_of_ist_date(filters.to_date + timedelta(days=1))
        )
    return predicates


def _filters_from_query(query: Mapping[str, object]) -> AuditFilters:
    actor = _text_param(query, ("actor", "actor_id"), "actor")
    subject = _text_param(query, ("subject", "subject_id"), "subject")
    subject_type = _text_param(query, ("subject_type",), "subject_type")
    event_type = _text_param(query, ("event_type", "type"), "event_type")
    event_id = _text_param(query, ("event_id", "id"), "event_id")
    from_value = _text_param(query, ("from_date", "from"), "from_date")
    to_value = _text_param(query, ("to_date", "to"), "to_date")
    from_date = _parse_date(from_value, "from_date")
    to_date = _parse_date(to_value, "to_date")
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValidationError("from_date cannot be after to_date.", field="from_date")
    return AuditFilters(
        actor=actor,
        subject=subject,
        subject_type=subject_type,
        event_type=event_type,
        event_id=event_id,
        from_date=from_date,
        to_date=to_date,
    )


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValidationError(f"{field} must be an ISO date.", field=field) from error


def _utc_start_of_ist_date(value: date) -> datetime:
    """Convert a user-facing IST calendar boundary to the stored UTC instant."""

    return datetime.combine(value, time.min, tzinfo=_AUDIT_TIMEZONE).astimezone(UTC)


def _text_param(query: Mapping[str, object], names: tuple[str, ...], field: str) -> str | None:
    value = _single_query_value(query, names)
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_FILTER_LENGTH:
        raise ValidationError(
            f"{field} must be at most {_MAX_FILTER_LENGTH} characters.", field=field
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError(f"{field} contains a control character.", field=field)
    return value


def _optional_query_int(query: Mapping[str, object], names: tuple[str, ...]) -> int | None:
    value = _single_query_value(query, names)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValidationError("page_size must be an integer.", field="page_size") from error
    if not 1 <= parsed <= MAX_PAGE_SIZE:
        raise ValidationError(
            f"page_size must be between 1 and {MAX_PAGE_SIZE}.", field="page_size"
        )
    return parsed


def _single_query_value(query: Mapping[str, object], names: tuple[str, ...]) -> str | None:
    values: list[str] = []
    for name in names:
        getlist = getattr(query, "getlist", None)
        if callable(getlist):
            candidates = getlist(name)
        else:
            raw = query.get(name)
            candidates = [raw] if isinstance(raw, str) else []
        values.extend(value for value in candidates if isinstance(value, str))
    non_empty = [value for value in values if value != ""]
    if len(set(non_empty)) > 1:
        raise ValidationError(
            f"Only one value may be supplied for {', '.join(names)}.", field=names[0]
        )
    return non_empty[0] if non_empty else None


def _export_requested(query: Mapping[str, object], force_export: bool) -> bool:
    if force_export:
        return True
    value = _single_query_value(query, ("export",))
    if value is None:
        return False
    if value.casefold() in {"1", "true", "yes", "csv"}:
        return True
    raise ValidationError("export must be csv when provided.", field="export")


def _decode_cursor(token: str | None, secret: bytes | str | None) -> AuditCursor | None:
    if token is None:
        return None
    try:
        return AuditCursor.decode(token, secret)
    except (InvalidAuditCursor, TypeError, ValueError) as error:
        raise ValidationError(
            "Audit cursor is invalid; reload the audit search.", field="cursor"
        ) from error


def _record_search_export(
    request: Request,
    *,
    principal: Principal,
    audit_writer: object | None,
    filters: AuditFilters,
    row_count: int,
    session: Session,
) -> None:
    writer = audit_writer or getattr(request.app.state, "audit_writer", None)
    if writer is None:
        writer = AuditRecorder(AuditRepository(session))
    record = getattr(writer, "record", None)
    if not callable(record):
        raise ExternalServiceError("The audit export could not be recorded.")
    actor: object = principal.id
    if principal.kind is PrincipalKind.API_KEY:
        actor = f"api-key:{principal.id}"
    elif session.get(AppUser, principal.id) is None:
        actor = f"user:{principal.id}"
    record(
        _SEARCH_EXPORT_EVENT,
        ("audit_search", principal.id),
        {
            "export_kind": "audit_event_search",
            "filters": filters.as_dict(),
            "row_count": row_count,
        },
        actor=actor,
        request_id=_request_id(request),
    )


def _csv_response(*, page_rows: tuple[AuditEvent, ...], request: Request) -> Response:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        (
            "sequence",
            "event_id",
            "occurred_at",
            "actor",
            "event_type",
            "subject_type",
            "subject_id",
            "payload",
            "prev_hash",
            "hash",
        )
    )
    for row in page_rows:
        writer.writerow(
            (
                _csv_cell(row.sequence),
                _csv_cell(row.id),
                _csv_cell(row.occurred_at.astimezone(UTC).isoformat()),
                _csv_cell(row.actor_label or row.actor_id or "System"),
                _csv_cell(row.event_type),
                _csv_cell(row.subject_type),
                _csv_cell(row.subject_id),
                _csv_cell(row.payload),
                _csv_cell(row.prev_hash),
                _csv_cell(row.hash),
            )
        )
    body = output.getvalue().encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="audit-events.csv"',
            "Content-Length": str(len(body)),
            "X-Audit-Row-Count": str(len(page_rows)),
            "X-Request-ID": _request_id(request),
        },
    )


def _csv_cell(value: object) -> str:
    """Return a CSV-safe string, including spreadsheet formula protection."""

    if value is None:
        return ""
    if isinstance(value, Mapping):
        import json

        text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    else:
        text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _scope_for(
    principal: Principal,
    session: Session,
    resolver: Callable[[Principal], Scope] | None,
) -> Scope:
    scope = resolver(principal) if resolver is not None else resolve_scope(principal, session)
    if not isinstance(scope, Scope) or scope.principal_id != principal.id:
        raise ValidationError("The resolved portfolio scope is invalid.", field="scope")
    return scope


def _required_uuid_param(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise ValidationError(f"{field} must be a UUID.", field=field) from error


def _optional_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", "web-audit")
    return value if isinstance(value, str) and value else "web-audit"


def _safe_filename(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_EXPORT_FILENAME_LENGTH
        or any(character in value for character in '\r\n"\\/')
    ):
        raise ExternalServiceError("The evidence bundle returned an unsafe filename.")
    return value


def _render(
    request: Request,
    fallback_environment: Environment,
    template_name: str,
    *,
    principal: Principal | None,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template(template_name)
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    theme = theme_for_request(request)
    labels = {
        "title": "Audit and reconstruction",
        "heading": "Audit event search",
        "actor": "Actor",
        "subject": "Subject ID",
        "subject_type": "Subject type",
        "event_type": "Event type",
        "event_id": "Event ID or sequence",
        "from_date": "From date",
        "to_date": "To date",
        "search": "Search audit events",
        "export_search": "Export filtered events",
        "result_count": "{count} matching events",
        "sequence": "Sequence",
        "event_id_column": "Event ID",
        "occurred_at": "Occurred (IST)",
        "actor_column": "Actor",
        "type_column": "Type",
        "subject_column": "Subject",
        "payload_column": "Payload",
        "open_warning": "Open warning reconstruction",
        "next": "Next page",
        "no_events": "No audit events match these filters.",
        "chain_verified": "Audit chain verified",
        "chain_failed": "Audit chain verification failed",
        "chain_failure_detail": "Integrity failure: {message}",
        "warning_title": "Warning reconstruction",
        "warning_id": "Forecast ID",
        "run_id": "Forecast run",
        "as_of_date": "As of date",
        "horizon": "Horizon",
        "export_bundle": "Export evidence bundle",
        "timeline": "Reconstruction timeline",
        "status": "Status",
        "provenance": "Provenance",
        "no_provenance": "No provenance reference was recorded.",
        "no_detail": "No record was generated for this part.",
        "status_present": "Present",
        "status_not_generated": "Not generated",
        "status_purged": "Purged",
        "status_absent": "Absent",
        "bundle_title": "Evidence bundle export",
        "bundle_queued": "The evidence bundle is being prepared.",
        "bundle_ready": "The evidence bundle is ready to download.",
        "download_bundle": "Download evidence bundle",
        "refresh_bundle": "Refresh bundle status",
        "manifest_hash": "Manifest hash",
    }
    return HTMLResponse(
        template.render(
            request=request,
            principal=principal,
            locale=locale,
            theme=theme,
            text_direction="ltr",
            labels=labels,
            csrf_token=getattr(request.state, "csrf_token", ""),
            **context,
        ),
        status_code=status_code,
    )


__all__ = ["create_audit_router"]
