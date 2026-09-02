"""Statement quarantine review and restatement web routes (`T-026`).

Mirrors `web/routes/documents.py`'s shape: one router factory built around
an injected service, server-rendered Jinja screens, and htmx-free plain
`<form>` posts so every action works with JavaScript disabled. The service
does every validation and persistence decision; this module only moves form
fields in and rendered HTML out.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import DomainError, ValidationError
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.statements import StatementImportService
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 1_100_000
_MAX_RESTATEMENT_UPLOAD_BYTES = 1_000_000
_COLUMN_FIELD_PREFIX = "col__"

_RESOLVE = requires(Permission.RESOLVE_QUARANTINE)
_CORRECT = requires(Permission.CORRECT_SOURCE_DATA)
_INGEST = requires(Permission.INGEST_FINANCIAL_STATEMENTS)
_RESOLVE_DEP = Depends(_RESOLVE)
_CORRECT_DEP = Depends(_CORRECT)
_INGEST_DEP = Depends(_INGEST)

_LABELS = {
    "quarantine_title": "Quarantine review",
    "quarantine_heading": "Rows awaiting resolution",
    "quarantine_empty": "No quarantined rows are awaiting resolution.",
    "row_number": "Row",
    "rule_failed": "Rule failed",
    "message": "Reason quarantined",
    "batch": "Import batch",
    "original_value": "Original value",
    "correction_heading": "Correct and re-submit",
    "reason": "Reason",
    "correct_submit": "Save correction",
    "reject_heading": "Reject",
    "reject_submit": "Reject row",
    "reason_required": "A reason is required.",
    "restate_title": "Restate a financial period",
    "restate_heading": "Restate a financial period",
    "source_type": "Source type",
    "mapping_name": "Mapping name",
    "file": "Corrected extract (one row)",
    "restate_submit": "Submit restatement",
    "restate_help": (
        "Upload one corrected row in the same shape as the original import — "
        "it must name the same borrower and financial period an existing period "
        "already covers."
    ),
    "restate_success": "Restatement recorded.",
    "flagged_tests": "Covenant tests flagged for recomputation",
    "no_flagged_tests": "No covenant test read the superseded period.",
    "back_to_quarantine": "Back to quarantine review",
    "import_title": "Financial statements",
    "import_heading": "Import a financial statement extract",
    "import_submit": "Import extract",
    "import_help": (
        "Upload a periodic financial extract for the borrowers in your scope. "
        "Rows that pass the mapping's rules are accepted; rows that fail one are "
        "quarantined for review rather than being silently dropped."
    ),
    "import_result_heading": "Import result",
    "import_rows": "Rows read",
    "import_accepted": "Accepted",
    "import_quarantined": "Quarantined",
    "import_batch": "Import batch",
    "import_duplicate": (
        "This extract was imported before; the original batch's result is shown."
    ),
}

_SOURCE_TYPES = ("csv", "xlsx", "json")


def create_statements_router(
    service: StatementImportService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the quarantine review and restatement screens over one service."""
    if not isinstance(service, StatementImportService):
        raise TypeError("create_statements_router requires a StatementImportService.")
    router = APIRouter(tags=["statements-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get(
        "/statements/quarantine", response_class=HTMLResponse, name="statements_quarantine_review"
    )
    async def quarantine_review(
        request: Request,
        principal: Principal = _RESOLVE_DEP,
    ) -> HTMLResponse:
        rows = service.list_open_quarantine_rows(principal)
        return _render_quarantine(
            request, fallback_environment, principal=principal, rows=rows, error=""
        )

    @router.post(
        "/statements/quarantine/{quarantine_row_id}/correct",
        response_class=HTMLResponse,
        name="statements_quarantine_correct",
    )
    async def quarantine_correct(
        request: Request,
        quarantine_row_id: UUID,
        principal: Principal = _CORRECT_DEP,
        _viewer: Principal = _RESOLVE_DEP,
    ) -> Response:
        values = await _form_values(request)
        corrected_raw = {
            key[len(_COLUMN_FIELD_PREFIX) :]: (value if value != "" else None)
            for key, value in values.items()
            if key.startswith(_COLUMN_FIELD_PREFIX)
        }
        try:
            service.correct_quarantine_row(
                principal,
                quarantine_row_id,
                corrected_raw=corrected_raw,
                reason=values.get("reason", ""),
            )
        except DomainError as error:
            rows = service.list_open_quarantine_rows(principal)
            return _render_quarantine(
                request,
                fallback_environment,
                principal=principal,
                rows=rows,
                error=error.message,
                status_code=422,
            )
        return RedirectResponse("/statements/quarantine", status_code=303)

    @router.post(
        "/statements/quarantine/{quarantine_row_id}/reject",
        response_class=HTMLResponse,
        name="statements_quarantine_reject",
    )
    async def quarantine_reject(
        request: Request,
        quarantine_row_id: UUID,
        principal: Principal = _RESOLVE_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.reject_quarantine_row(
                principal,
                quarantine_row_id,
                reason=values.get("reason", ""),
            )
        except DomainError as error:
            rows = service.list_open_quarantine_rows(principal)
            return _render_quarantine(
                request,
                fallback_environment,
                principal=principal,
                rows=rows,
                error=error.message,
                status_code=422,
            )
        return RedirectResponse("/statements/quarantine", status_code=303)

    # `base.html` and the borrower workspace have always linked here for the
    # `INGEST_FINANCIAL_STATEMENTS` permission, but no route answered the path
    # — every holder of that permission met a 404 on the one screen their
    # permission is named after.  This is that screen, over the same
    # `StatementImportService.import_statements` the API already exposes.
    @router.get(
        "/financial-statements",
        response_class=HTMLResponse,
        name="financial_statements_import_form",
    )
    async def financial_statements_form(
        request: Request,
        principal: Principal = _INGEST_DEP,
    ) -> HTMLResponse:
        return _render_import(request, fallback_environment, principal=principal, error="")

    @router.post(
        "/financial-statements",
        response_class=HTMLResponse,
        name="financial_statements_import",
    )
    async def financial_statements_import(
        request: Request,
        principal: Principal = _INGEST_DEP,
    ) -> HTMLResponse:
        fields, content = await _restate_form_values(request)
        source_type = fields.get("source_type", "")
        if source_type not in _SOURCE_TYPES:
            return _render_import(
                request,
                fallback_environment,
                principal=principal,
                error=f"source_type must be one of {', '.join(_SOURCE_TYPES)}.",
                status_code=422,
            )
        try:
            report = service.import_statements(
                principal,
                source_type=source_type,
                content=content,
                mapping_name=fields.get("mapping_name", ""),
                source_reference=fields.get("source_reference") or None,
                request_id=getattr(request.state, "request_id", None),
            )
        except DomainError as error:
            return _render_import(
                request,
                fallback_environment,
                principal=principal,
                error=error.message,
                status_code=422,
            )
        return _render_import(
            request,
            fallback_environment,
            principal=principal,
            error="",
            report=report,
        )

    @router.get("/statements/restate", response_class=HTMLResponse, name="statements_restate_form")
    async def restate_form(
        request: Request,
        principal: Principal = _CORRECT_DEP,
    ) -> HTMLResponse:
        return _render_restate(request, fallback_environment, principal=principal, error="")

    @router.post("/statements/restate", response_class=HTMLResponse, name="statements_restate")
    async def restate_submit(
        request: Request,
        principal: Principal = _CORRECT_DEP,
    ) -> HTMLResponse:
        fields, content = await _restate_form_values(request)
        try:
            result = service.restate_period(
                principal,
                source_type=fields.get("source_type", ""),
                content=content,
                mapping_name=fields.get("mapping_name", ""),
                reason=fields.get("reason", ""),
            )
        except DomainError as error:
            return _render_restate(
                request,
                fallback_environment,
                principal=principal,
                error=error.message,
                status_code=422,
            )
        return _render_restate_result(
            request, fallback_environment, principal=principal, result=result
        )

    return router


def _render_quarantine(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    rows: object,
    error: str,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/statements/_quarantine_review.html")
    return HTMLResponse(
        template.render(**_base_context(request, principal, error=error, rows=rows)),
        status_code=status_code,
    )


def _render_import(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    error: str,
    report: object | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/statements/_import.html")
    context = _base_context(request, principal, error=error)
    context["source_type_options"] = _SOURCE_TYPES
    context["report"] = report
    return HTMLResponse(template.render(**context), status_code=status_code)


def _render_restate(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    error: str,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/statements/_restate.html")
    context = _base_context(request, principal, error=error)
    context["source_type_options"] = _SOURCE_TYPES
    return HTMLResponse(template.render(**context), status_code=status_code)


def _render_restate_result(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    result: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/statements/_restate_result.html")
    context = _base_context(request, principal, error="")
    context["result"] = result
    return HTMLResponse(template.render(**context))


def _base_context(
    request: Request,
    principal: Principal,
    *,
    error: str,
    rows: object = (),
) -> dict[str, object]:
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    return {
        "request": request,
        "principal": principal,
        "locale": locale,
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "rows": rows,
        "error": error,
        "column_prefix": _COLUMN_FIELD_PREFIX,
    }


async def _form_values(request: Request) -> dict[str, str]:
    """Parse a text-only form post — the same rules `documents.py` applies:
    a byte cap, CSRF-field exclusion, and no attempt to read a file part."""
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted form is too large.", field="form")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/x-www-form-urlencoded" or not content_type:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError(
                "The submitted form is not valid UTF-8.", field="form"
            ) from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values and key != "csrf_token"}
    form = await request.form()
    values: dict[str, str] = {}
    for key, value in form.multi_items():
        if key != "csrf_token" and isinstance(value, str):
            values[key] = value
    return values


async def _restate_form_values(request: Request) -> tuple[dict[str, str], bytes]:
    """Parse the restatement form's text fields plus its one uploaded file."""
    form = await request.form()
    fields: dict[str, str] = {}
    content: bytes | None = None
    for key, value in form.multi_items():
        if key == "csrf_token":
            continue
        if key == "file":
            if not hasattr(value, "read"):
                raise ValidationError("A corrected extract file is required.", field="file")
            content = await value.read(_MAX_RESTATEMENT_UPLOAD_BYTES + 1)
            if len(content) > _MAX_RESTATEMENT_UPLOAD_BYTES:
                raise ValidationError(
                    f"The uploaded extract exceeds {_MAX_RESTATEMENT_UPLOAD_BYTES} bytes.",
                    field="file",
                )
        elif isinstance(value, str):
            fields[key] = value
    if content is None:
        raise ValidationError("A corrected extract file is required.", field="file")
    return fields, content


__all__ = ["create_statements_router"]
