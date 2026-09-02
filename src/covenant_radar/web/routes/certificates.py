"""Browser screen and form actions for the compliance certificate
workflow (`T-039`, `spec §R-09`)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import DomainError, ValidationError
from covenant_radar.db.models.signal import CertificateRequest
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.certificates import CertificateService
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 16 * 1024
_READ = requires(Permission.VIEW_COVENANT)
_RECEIVE = requires(Permission.UPLOAD_DOCUMENT)
_REVIEW = requires(Permission.RECORD_WAIVER)
_READ_DEP = Depends(_READ)
_RECEIVE_DEP = Depends(_RECEIVE)
_REVIEW_DEP = Depends(_REVIEW)

_LABELS = {
    "title": "Certificates",
    "heading": "Compliance certificate requests",
    "empty": "No open certificate requests are in this scope.",
    "borrower": "Borrower",
    "due_date": "Due date",
    "state": "Status",
    "requested_at": "Requested",
    "received_at": "Received",
    "actions": "Actions",
    "receive": "Receive",
    "accept": "Accept",
    "reject": "Reject",
    "document_id": "Document id",
    "reason": "Reason",
    "requested": "Requested",
    "received": "Received",
    "under_review": "Under review",
    "accepted": "Accepted",
    "rejected": "Rejected",
    "overdue": "Overdue",
}


def create_certificates_router(
    service: CertificateService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected certificate list and review-action screens."""
    if not isinstance(service, CertificateService):
        raise TypeError("create_certificates_router requires a CertificateService.")
    router = APIRouter(tags=["certificates-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/certificates", response_class=HTMLResponse, name="certificate_list")
    async def certificate_list(
        request: Request, principal: Principal = _READ_DEP
    ) -> HTMLResponse:
        rows = service.list_open(principal)
        return _render(
            request, fallback_environment, principal=principal, rows=rows, table_rows=_rows(rows)
        )

    @router.post(
        "/certificates/{request_id}/receive",
        response_class=HTMLResponse,
        name="certificate_receive_submit",
    )
    async def certificate_receive_submit(
        request: Request,
        request_id: UUID,
        principal: Principal = _RECEIVE_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            document_id = _required_uuid(values.get("document_id"), "document_id")
            service.receive(principal, request_id, document_id=document_id)
        except DomainError as error:
            open_rows = service.list_open(principal)
            return _render(
                request,
                fallback_environment,
                principal=principal,
                rows=open_rows,
                table_rows=_rows(open_rows),
                error=error.message,
                status_code=422,
            )
        return RedirectResponse("/certificates", status_code=303)

    @router.post(
        "/certificates/{request_id}/accept",
        response_class=HTMLResponse,
        name="certificate_accept_submit",
    )
    async def certificate_accept_submit(
        request: Request,
        request_id: UUID,
        principal: Principal = _REVIEW_DEP,
    ) -> Response:
        try:
            service.accept(principal, request_id)
        except DomainError as error:
            open_rows = service.list_open(principal)
            return _render(
                request,
                fallback_environment,
                principal=principal,
                rows=open_rows,
                table_rows=_rows(open_rows),
                error=error.message,
                status_code=422,
            )
        return RedirectResponse("/certificates", status_code=303)

    @router.post(
        "/certificates/{request_id}/reject",
        response_class=HTMLResponse,
        name="certificate_reject_submit",
    )
    async def certificate_reject_submit(
        request: Request,
        request_id: UUID,
        principal: Principal = _REVIEW_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.reject(principal, request_id, reason=values.get("reason", ""))
        except DomainError as error:
            open_rows = service.list_open(principal)
            return _render(
                request,
                fallback_environment,
                principal=principal,
                rows=open_rows,
                table_rows=_rows(open_rows),
                error=error.message,
                status_code=422,
            )
        return RedirectResponse("/certificates", status_code=303)

    return router


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    rows: object,
    table_rows: object = (),
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/certificates/index.html")
    values = {
        "request": request,
        "principal": principal,
        "locale": request.cookies.get("covenant_radar_locale", "en"),
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "rows": rows,
        "table_rows": table_rows,
        "error": error,
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


def _rows(rows: Sequence[CertificateRequest]) -> list[dict[str, object]]:
    return [
        {
            "id": str(row.id),
            "borrower": str(row.borrower_id),
            "due_date": row.due_date.isoformat(),
            "state": _LABELS.get(row.state, row.state),
            "requested_at": row.requested_at.isoformat() if row.requested_at else "",
            "received_at": row.received_at.isoformat() if row.received_at else "",
        }
        for row in rows
    ]


async def _form_values(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted form is too large.", field="form")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/x-www-form-urlencoded" or not content_type:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError("The submitted form is not valid UTF-8.", field="form") from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values and key != "csrf_token"}
    form = await request.form()
    values: dict[str, str] = {}
    for key, value in form.multi_items():
        if key != "csrf_token" and isinstance(value, str):
            values[key] = value
    return values


def _required_uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return UUID(value.strip())
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} is required.", field=field)


__all__ = ["create_certificates_router"]
