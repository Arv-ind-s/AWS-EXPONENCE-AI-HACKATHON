"""Administrative browser routes for the intervention catalogue."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import DomainError, ValidationError
from covenant_radar.security.rbac import Principal
from covenant_radar.services.catalogue import (
    CATALOGUE_APPROVE_PERMISSION,
    CATALOGUE_PROPOSE_PERMISSION,
    CatalogueService,
)
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 128 * 1024
_READ = requires(CATALOGUE_PROPOSE_PERMISSION)
_WRITE = requires(CATALOGUE_PROPOSE_PERMISSION)
_APPROVE = requires(CATALOGUE_APPROVE_PERMISSION)
_READ_DEP = Depends(_READ)
_WRITE_DEP = Depends(_WRITE)
_APPROVE_DEP = Depends(_APPROVE)

_LABELS = {
    "title": "Action catalogue",
    "heading": "Bank-owned intervention catalogue",
    "new": "Add intervention",
    "save": "Save intervention",
    "retire": "Retire",
    "retired": "Retired",
    "active": "Active",
    "pending": "Pending approval",
    "code": "Identifier",
    "role_tag": "Role tag",
    "text": "Display text",
    "effect_model": "Effect model",
    "effect_parameters": "Effect parameters (JSON)",
    "assumptions": "Assumptions (one per line)",
    "classes": "Applicable covenant classes (comma-separated)",
    "requires_approval": "Requires approval when recommended",
    "actions": "Actions",
    "approve": "Approve",
    "reject": "Reject",
    "reason": "Reason",
    "empty": "No intervention catalogue entries are configured.",
}


def create_catalogue_router(
    service: CatalogueService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build protected catalogue and approval screens over one service."""

    if not isinstance(service, CatalogueService):
        raise TypeError("create_catalogue_router requires a CatalogueService.")
    router = APIRouter(tags=["catalogue-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/admin/catalogue", response_class=HTMLResponse, name="catalogue_list")
    async def catalogue_list(
        request: Request,
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        return _render(
            request,
            fallback_environment,
            principal=principal,
            rows=service.list(active_only=None),
            form={},
        )

    @router.post("/admin/catalogue", response_class=HTMLResponse, name="catalogue_save")
    async def catalogue_save(
        request: Request,
        principal: Principal = _WRITE_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            result = service.save(principal, _entry_values(values))
        except DomainError as error:
            return _render(
                request,
                fallback_environment,
                principal=principal,
                rows=service.list(active_only=None),
                form=values,
                error=error.message,
                status_code=422,
            )
        destination = "/admin/catalogue"
        if result.pending:
            destination += "?pending=1"
        return RedirectResponse(destination, status_code=303)

    @router.get(
        "/admin/catalogue/approvals",
        response_class=HTMLResponse,
        name="catalogue_approval_list",
    )
    async def catalogue_approval_list(
        request: Request,
        principal: Principal = _APPROVE_DEP,
    ) -> HTMLResponse:
        return _render(
            request,
            fallback_environment,
            principal=principal,
            rows=service.list(active_only=None),
            pending=service.pending_approvals(principal),
            form={},
        )

    @router.post(
        "/admin/catalogue/approvals/{request_id}",
        response_class=HTMLResponse,
        name="catalogue_approval_decide",
    )
    async def catalogue_approval_decide(
        request: Request,
        request_id: UUID,
        principal: Principal = _APPROVE_DEP,
    ) -> Response:
        values = await _form_values(request)
        decision = values.get("decision")
        try:
            if decision not in {"approve", "reject"}:
                raise ValidationError("decision must be approve or reject.", field="decision")
            service.decide_approval(
                principal,
                request_id,
                approved=decision == "approve",
                reason=values.get("reason"),
            )
        except DomainError as error:
            return _render(
                request,
                fallback_environment,
                principal=principal,
                rows=service.list(active_only=None),
                pending=service.pending_approvals(principal),
                form=values,
                error=error.message,
                status_code=422,
            )
        return RedirectResponse("/admin/catalogue/approvals", status_code=303)

    @router.post(
        "/admin/catalogue/{code}/retire",
        response_class=HTMLResponse,
        name="catalogue_retire",
    )
    async def catalogue_retire(
        request: Request,
        code: str,
        principal: Principal = _WRITE_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            expected_version = _optional_int(values.get("expected_version"))
            result = service.retire(principal, code, expected_version=expected_version)
        except DomainError as error:
            return _render(
                request,
                fallback_environment,
                principal=principal,
                rows=service.list(active_only=None),
                form=values,
                error=error.message,
                status_code=422,
            )
        destination = "/admin/catalogue"
        if result.pending:
            destination += "?pending=1"
        return RedirectResponse(destination, status_code=303)

    return router


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    rows: object,
    form: Mapping[str, object],
    pending: object = (),
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/admin/_catalogue.html")
    values = {
        "request": request,
        "principal": principal,
        "locale": request.cookies.get("covenant_radar_locale", "en"),
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "rows": rows,
        "pending": pending,
        "form": form,
        "error": error,
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


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


def _entry_values(values: Mapping[str, str]) -> dict[str, object]:
    raw_parameters = values.get("effect_parameters", "").strip()
    if not raw_parameters:
        parameters: object = {}
    else:
        try:
            parameters = json.loads(raw_parameters)
        except json.JSONDecodeError as error:
            raise ValidationError(
                f"effect_parameters is not valid JSON: {error.msg}.",
                field="intervention.effect_parameters",
            ) from error
    if not isinstance(parameters, Mapping):
        raise ValidationError(
            "effect_parameters must be a JSON object.",
            field="intervention.effect_parameters",
        )
    classes = tuple(
        item.strip()
        for item in values.get("applicable_covenant_classes", "").replace("\n", ",").split(",")
        if item.strip()
    )
    assumptions = tuple(
        item.strip() for item in values.get("assumptions", "").splitlines() if item.strip()
    )
    return {
        "id": values.get("id", values.get("code", "")),
        "role_tag": values.get("role_tag", ""),
        "text": values.get("text", ""),
        "effect_model": values.get("effect_model", ""),
        "effect_parameters": dict(parameters),
        "applicable_covenant_classes": classes,
        "assumptions": assumptions,
        "requires_approval": values.get("requires_approval") in {"1", "true", "on"},
        "is_active": True,
    }


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValidationError("expected_version must be an integer.", field="version") from error
    if parsed < 0:
        raise ValidationError("expected_version must be non-negative.", field="version")
    return parsed


__all__ = ["create_catalogue_router"]
