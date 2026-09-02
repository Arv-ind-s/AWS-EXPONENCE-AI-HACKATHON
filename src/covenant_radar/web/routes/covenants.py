"""Browser screens and form actions for the covenant registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import parse_qs, quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pydantic import ValidationError as PydanticValidationError

from covenant_radar.api.deps import requires
from covenant_radar.api.v1.schemas.covenants import (
    ApprovalDecisionRequest,
    CovenantAmendRequest,
    CovenantCreateRequest,
    WaiverCreateRequest,
)
from covenant_radar.core.errors import DomainError, ValidationError
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.registry import RegistryService
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 128 * 1024
_READ = requires(Permission.VIEW_COVENANT)
_REGISTER = requires(Permission.REGISTER_COVENANT)
_APPROVE = requires(Permission.APPROVE_COVENANT)
_WAIVER = requires(Permission.RECORD_WAIVER)
_READ_DEP = Depends(_READ)
_REGISTER_DEP = Depends(_REGISTER)
_APPROVE_DEP = Depends(_APPROVE)
_WAIVER_DEP = Depends(_WAIVER)

_LABELS = {
    "title": "Covenants",
    "heading": "Covenant registry",
    "new": "Register covenant",
    "empty": "No covenants are available in this scope.",
    "reference": "Reference",
    "name": "Name",
    "class": "Class",
    "facility": "Facility",
    "status": "Status",
    "active": "Active",
    "retired": "Retired",
    "versions": "Versions",
    "actions": "Actions",
    "open": "Open",
    "save": "Save covenant",
    "amend": "Amend covenant",
    "retire": "Retire covenant",
    "waiver": "Request waiver",
    "approvals": "Pending approvals",
    "approve": "Approve",
    "reject": "Reject",
    "reason": "Reason",
    "definition": "Ratio definition",
    "formula": "Custom formula",
    "threshold": "Threshold",
    "direction": "Direction",
    "unit": "Unit",
    "frequency": "Frequency",
    "test_basis": "Test basis",
    "effective_from": "Effective from",
    "effective_to": "Effective to",
    "warning_headroom": "Warning headroom (%)",
    "cure_days": "Cure days",
    "grace_days": "Grace days",
    "form_error": "The form needs correction",
    "required": "Required",
    "maker": "Maker",
    "operation": "Operation",
    "created": "Created",
}


def create_covenants_router(
    service: RegistryService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build protected covenant registry screens over one service."""
    if not isinstance(service, RegistryService):
        raise TypeError("create_covenants_router requires a RegistryService.")
    router = APIRouter(tags=["covenants-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/covenants", response_class=HTMLResponse, name="covenant_list")
    async def covenant_list(request: Request, principal: Principal = _READ_DEP) -> HTMLResponse:
        rows = service.list_covenants(principal, active_only=None)
        return _render(
            request,
            fallback_environment,
            "screens/covenants/covenants.html",
            principal=principal,
            rows=rows,
            table_rows=_covenant_rows(rows),
        )

    @router.get("/covenants/new", response_class=HTMLResponse, name="covenant_create_page")
    async def covenant_create_page(
        request: Request, principal: Principal = _REGISTER_DEP
    ) -> HTMLResponse:
        return _render(
            request,
            fallback_environment,
            "screens/covenants/covenant_form.html",
            principal=principal,
            mode="create",
            action="/covenants/new",
            form={},
        )

    @router.post("/covenants/new", response_class=HTMLResponse, name="covenant_create_submit")
    async def covenant_create_submit(
        request: Request, principal: Principal = _REGISTER_DEP
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = CovenantCreateRequest.model_validate(
                _none_for_empty(values, _OPTIONAL_TERM_FIELDS)
            )
            result = service.register(
                principal,
                facility_id=payload.facility_id,
                reference=payload.reference,
                name=payload.name,
                covenant_class=payload.covenant_class,
                terms=payload.to_domain(),
            )
        except (DomainError, PydanticValidationError) as error:
            return _form_error(
                request,
                fallback_environment,
                "screens/covenants/covenant_form.html",
                principal=principal,
                mode="create",
                action="/covenants/new",
                form=values,
                error=_error_message(error, resource="covenant"),
            )
        return RedirectResponse(f"/covenants/{result.covenant.reference}", status_code=303)

    @router.get("/covenants/approvals", response_class=HTMLResponse, name="covenant_approval_list")
    async def covenant_approval_list(
        request: Request, principal: Principal = _APPROVE_DEP
    ) -> HTMLResponse:
        return _render(
            request,
            fallback_environment,
            "screens/covenants/approvals.html",
            principal=principal,
            rows=service.pending_approvals(principal),
        )

    @router.post(
        "/covenants/approvals/{request_id}",
        response_class=HTMLResponse,
        name="covenant_approval_decide",
    )
    async def covenant_approval_decide(
        request: Request,
        request_id: UUID,
        principal: Principal = _APPROVE_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = ApprovalDecisionRequest.model_validate(values)
            service.decide_approval(
                principal,
                request_id,
                approved=payload.is_approved,
                reason=payload.reason,
            )
        except (DomainError, PydanticValidationError) as error:
            return _form_error(
                request,
                fallback_environment,
                "screens/covenants/approvals.html",
                principal=principal,
                rows=service.pending_approvals(principal),
                error=_error_message(error, resource="approval"),
            )
        return RedirectResponse("/covenants/approvals", status_code=303)

    @router.get("/covenants/{reference}", response_class=HTMLResponse, name="covenant_detail")
    async def covenant_detail(
        request: Request, reference: str, principal: Principal = _READ_DEP
    ) -> HTMLResponse:
        covenant = service.get_covenant(principal, reference)
        versions = service.list_versions(principal, covenant.id)
        return _render(
            request,
            fallback_environment,
            "screens/covenants/covenant_detail.html",
            principal=principal,
            covenant=covenant,
            versions=versions,
            amend_form=_form_for_version(versions[-1]) if versions else {},
        )

    @router.post(
        "/covenants/{reference}/amend",
        response_class=HTMLResponse,
        name="covenant_amend_submit",
    )
    async def covenant_amend_submit(
        request: Request, reference: str, principal: Principal = _REGISTER_DEP
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = CovenantAmendRequest.model_validate(
                _none_for_empty(values, _OPTIONAL_TERM_FIELDS)
            )
            service.amend(principal, reference, terms=payload.to_domain())
        except (DomainError, PydanticValidationError) as error:
            covenant = service.get_covenant(principal, reference)
            versions = service.list_versions(principal, covenant.id)
            return _form_error(
                request,
                fallback_environment,
                "screens/covenants/covenant_detail.html",
                principal=principal,
                covenant=covenant,
                versions=versions,
                amend_form=values,
                error=_error_message(error, resource="covenant"),
            )
        return RedirectResponse(f"/covenants/{reference}", status_code=303)

    @router.post(
        "/covenants/{reference}/retire",
        response_class=HTMLResponse,
        name="covenant_retire_submit",
    )
    async def covenant_retire_submit(
        reference: str, principal: Principal = _REGISTER_DEP
    ) -> Response:
        service.retire(principal, reference)
        return RedirectResponse(f"/covenants/{reference}", status_code=303)

    @router.post(
        "/covenants/{reference}/waivers",
        response_class=HTMLResponse,
        name="covenant_waiver_submit",
    )
    async def covenant_waiver_submit(
        request: Request, reference: str, principal: Principal = _WAIVER_DEP
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = WaiverCreateRequest.model_validate(values)
            service.request_waiver(
                principal,
                reference,
                from_date=payload.from_date,
                to_date=payload.to_date,
                reason=payload.reason,
                waiver_scope=payload.waiver_scope,
                document_id=payload.document_id,
            )
        except (DomainError, PydanticValidationError) as error:
            covenant = service.get_covenant(principal, reference)
            versions = service.list_versions(principal, covenant.id)
            return _form_error(
                request,
                fallback_environment,
                "screens/covenants/covenant_detail.html",
                principal=principal,
                covenant=covenant,
                versions=versions,
                amend_form=_form_for_version(versions[-1]) if versions else {},
                error=_error_message(error, resource="waiver"),
            )
        return RedirectResponse(f"/covenants/{reference}", status_code=303)

    return router


_OPTIONAL_TERM_FIELDS = (
    "definition_ref",
    "custom_formula",
    "effective_to",
    "warning_headroom_pct",
    "cure_days",
    "grace_days",
    "source_document_id",
    "source_span_id",
)


def _render(
    request: Request,
    fallback_environment: Environment,
    template_name: str,
    *,
    principal: Principal,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template(template_name)
    values = {
        "request": request,
        "principal": principal,
        "locale": request.cookies.get("covenant_radar_locale", "en"),
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "form": {},
        "error": "",
        "rows": (),
        **context,
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


def _form_error(
    request: Request,
    environment: Environment,
    template_name: str,
    *,
    principal: Principal,
    error: str,
    **context: object,
) -> HTMLResponse:
    return _render(
        request,
        environment,
        template_name,
        status_code=422,
        principal=principal,
        error=error,
        **context,
    )


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


def _none_for_empty(values: Mapping[str, str], fields: Sequence[str]) -> dict[str, str | None]:
    normalized: dict[str, str | None] = dict(values)
    for field in fields:
        if not normalized.get(field, "").strip():
            normalized[field] = None
    return normalized


def _error_message(error: DomainError | PydanticValidationError, *, resource: str) -> str:
    if isinstance(error, DomainError):
        return error.message
    first = error.errors()[0] if error.errors() else {"msg": "invalid value"}
    return f"{resource}: {first.get('msg', 'invalid value')}."


def _form_for_version(version: CovenantVersion) -> dict[str, object]:
    return {
        "definition_ref": version.definition_ref or "",
        "custom_formula": version.custom_formula or "",
        "threshold": version.threshold,
        "direction": version.direction,
        "unit": version.unit,
        "frequency": version.frequency,
        "test_basis": version.test_basis,
        "effective_from": version.effective_from,
        "effective_to": version.effective_to or "",
        "warning_headroom_pct": version.warning_headroom_pct or "",
        "cure_days": version.cure_days if version.cure_days is not None else "",
        "grace_days": version.grace_days if version.grace_days is not None else "",
    }


def _covenant_rows(rows: Sequence[Covenant]) -> list[dict[str, object]]:
    return [
        {
            "id": row.reference,
            "reference": row.reference,
            "name": row.name,
            "class": row.covenant_class,
            "facility": str(row.facility_id),
            "status": "active" if row.is_active else "retired",
            "actions": Markup('<a class="button" href="/covenants/{href}">{label}</a>').format(
                href=quote(row.reference, safe=""), label=_LABELS["open"]
            ),
        }
        for row in rows
    ]


__all__ = ["create_covenants_router"]
