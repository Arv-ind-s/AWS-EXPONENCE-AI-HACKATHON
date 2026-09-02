"""Browser endpoints for scoped bulk case actions and list exports."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.errors import ValidationError
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.bulk import BulkService
from covenant_radar.services.export import ExportResult, ExportService
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 256 * 1024
_MAX_SELECTION = 5_000
_BULK_DEP = Depends(requires(Permission.VIEW_CASE))
_EXPORT_DEP = Depends(requires(Permission.EXPORT_EVIDENCE))

_LABELS = {
    "bulk_title": "Bulk operation report",
    "bulk_heading": "Bulk operation report",
    "bulk_action": "Action",
    "bulk_requested": "Requested",
    "bulk_succeeded": "Succeeded",
    "bulk_failed": "Failed",
    "bulk_excluded": "Excluded",
    "bulk_item": "Item",
    "bulk_status": "Status",
    "bulk_reason": "Reason",
    "bulk_done": "The operation is complete. Every selected item has an outcome.",
    "export_title": "Export status",
    "export_heading": "Export status",
    "export_queued": "Your export is being prepared. This page can be refreshed safely.",
    "export_ready": "Your export is ready to download.",
    "export_running": "Your export is being prepared.",
    "export_failed": "The export could not be prepared.",
    "export_expired": "The download link has expired.",
    "download": "Download export",
    "row_count": "Rows",
    "format": "Format",
    "expires": "Link expires",
    "back_queue": "Back to queue",
}


def create_bulk_router(
    service_or_session: BulkService | Session,
    *,
    bulk_service: BulkService | None = None,
    export_service: ExportService | None = None,
    audit_writer: object | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build protected bulk and export routes.

    The factory accepts a service for isolated tests or a database session
    for composition roots that already provide an export service and store.
    An export store is never created implicitly in a web worker.
    """

    if bulk_service is not None:
        if not isinstance(bulk_service, BulkService):
            raise TypeError("bulk_service must be a BulkService.")
        resolved_bulk = bulk_service
    elif isinstance(service_or_session, BulkService):
        resolved_bulk = service_or_session
    elif is_database_session(service_or_session):
        audit = audit_writer or AuditRecorder(AuditRepository(cast(Session, service_or_session)))
        resolved_bulk = BulkService(cast(Session, service_or_session), audit=audit)
    else:
        raise TypeError("create_bulk_router requires a BulkService or SQLAlchemy Session.")

    if export_service is None or not isinstance(export_service, ExportService):
        raise TypeError("create_bulk_router requires an ExportService.")
    if export_service.session is not None and export_service.session is not resolved_bulk.session:
        raise ValueError("bulk_service and export_service must use the same session.")

    router = APIRouter(tags=["bulk-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.post("/bulk", response_class=HTMLResponse, name="bulk_operation")
    async def bulk_operation(
        request: Request,
        principal: Principal = _BULK_DEP,
    ) -> Response:
        values = await _form_values(request)
        selected = _selection(values)
        action = _text(values.get("action"))
        value = _bulk_value(action, values)
        report = resolved_bulk.execute(
            principal,
            selected,
            action,
            value=value,
            filters=_filters_from_values(values),
        )
        if _wants_json(request):
            return JSONResponse(report.as_dict())
        return _render(
            request,
            fallback_environment,
            "screens/exports/bulk_result.html",
            principal=principal,
            report=report,
        )

    @router.post("/exports", response_class=Response, name="export_create")
    async def export_create(
        request: Request,
        principal: Principal = _EXPORT_DEP,
    ) -> Response:
        values = await _form_values(request)
        result = export_service.export_cases(
            principal,
            case_ids=_selection(values),
            filters=_filters_from_values(values),
            format=_text(values.get("format")) or "csv",
        )
        if result.content is not None:
            return _file_response(result)
        return _export_result_response(request, fallback_environment, principal, result)

    @router.get("/exports/{export_id}", response_class=Response, name="export_status")
    def export_status(
        request: Request,
        export_id: UUID,
        principal: Principal = _EXPORT_DEP,
    ) -> Response:
        result = export_service.status(principal, export_id)
        return _export_result_response(request, fallback_environment, principal, result)

    @router.get("/exports/{export_id}/download", response_class=Response, name="export_download")
    def export_download(
        export_id: UUID,
        principal: Principal = _EXPORT_DEP,
    ) -> Response:
        download = export_service.download(principal, export_id)
        media_type = (
            "text/csv; charset=utf-8"
            if download.format == "csv"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return Response(
            content=download.content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{download.filename}"',
                "X-Export-SHA256": download.content_hash,
            },
        )

    return router


async def _form_values(request: Request) -> dict[str, object]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_FORM_BYTES:
                raise ValidationError("The bulk form is too large.", field="form")
        except ValueError as error:
            raise ValidationError("The form content length is invalid.", field="form") from error
    form = await request.form()
    values: dict[str, object] = {}
    for key, value in form.multi_items():
        if key in values:
            prior = values[key]
            if isinstance(prior, list):
                prior.append(value)
            else:
                values[key] = [prior, value]
        else:
            values[key] = value
    return values


def _selection(values: Mapping[str, object]) -> tuple[str, ...]:
    raw = values.get("selected_ids", values.get("case_ids", ()))
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as error:
                raise ValidationError(
                    "selected_ids must be valid JSON.", field="selected_ids"
                ) from error
        else:
            raw = (raw,)
    if not isinstance(raw, Sequence) or isinstance(raw, bytes | bytearray):
        raise ValidationError("selected_ids must be a sequence.", field="selected_ids")
    if len(raw) > _MAX_SELECTION:
        raise ValidationError(
            f"At most {_MAX_SELECTION} items may be selected.", field="selected_ids"
        )
    result: list[str] = []
    for value in raw:
        if isinstance(value, UUID):
            result.append(str(value))
        elif isinstance(value, str) and value.strip():
            result.append(value.strip())
        else:
            raise ValidationError("Every selected id must be non-empty text.", field="selected_ids")
    return tuple(result)


def _bulk_value(action: str, values: Mapping[str, object]) -> object:
    if action == "assign":
        return _text(values.get("assignee")) or _text(values.get("assignee_id"))
    if action == "state":
        return {
            "state": _text(values.get("state")),
            "reason": _text(values.get("reason")) or None,
        }
    if action == "watchlist":
        return {"enabled": True}
    return None


def _filters_from_values(values: Mapping[str, object]) -> dict[str, object]:
    excluded = {
        "selected_ids",
        "case_ids",
        "csrf_token",
        "action",
        "assignee",
        "assignee_id",
        "state",
        "reason",
        "format",
    }
    result: dict[str, object] = {}
    for key, value in values.items():
        if key in excluded:
            continue
        if isinstance(value, list):
            result[key] = [str(item) for item in value]
        else:
            result[key] = str(value)
    return result


def _export_result_response(
    request: Request,
    fallback_environment: Environment,
    principal: Principal,
    result: ExportResult,
) -> Response:
    if _wants_json(request):
        return JSONResponse(result.as_dict(), status_code=202 if result.queued else 200)
    is_fragment = (
        request.headers.get("HX-Request", "").lower() == "true"
        and request.headers.get("HX-Target") == "export-status-region"
    )
    return _render(
        request,
        fallback_environment,
        "_components/export_status.html" if is_fragment else "screens/exports/status.html",
        principal=principal,
        result=result,
        status_code=202 if result.queued else 200,
    )


def _file_response(result: ExportResult) -> Response:
    assert result.content is not None
    media_type = (
        "text/csv; charset=utf-8"
        if result.format == "csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    return Response(
        content=result.content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="covenant-radar-export.{result.format}"',
            "X-Export-SHA256": result.content_hash or "",
        },
    )


def _render(
    request: Request,
    fallback_environment: Environment,
    template_name: str,
    *,
    principal: Principal,
    status_code: int = status.HTTP_200_OK,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template(template_name)
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    values = {
        "request": request,
        "principal": principal,
        "locale": locale,
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        **context,
    }
    response = HTMLResponse(template.render(**values), status_code=status_code)
    response.headers["Vary"] = "HX-Request, HX-Target"
    return response


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "").lower()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


__all__ = ["create_bulk_router"]
