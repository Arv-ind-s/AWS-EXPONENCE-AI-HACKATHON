"""Web routes for risk-view override capture (`T-111`, contract `C-12`).

The POST endpoint accepts only the subject and the human's replacement
values.  The service rebuilds the server-side ``shown`` snapshot, so a
browser cannot manufacture the evidence of what the application displayed.
The GET form routes are small server-rendered fragments that the borrower
case file and why-panel can embed without duplicating validation or exposing
an override control to a principal who lacks the risk permission.
"""

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
from covenant_radar.core.errors import DomainError, NotFound, ValidationError
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.overrides import OverrideService, OverrideSubject, RevisedRiskView
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_BODY_BYTES = 1_100_000
_OVERRIDE = requires(Permission.OVERRIDE_RISK_VIEW)
_OVERRIDE_DEP = Depends(_OVERRIDE)

_LABELS = {
    "title": "Override risk view",
    "current_view": "Current displayed view",
    "stage": "Decision stage",
    "user_action": "What you are doing",
    "user_value": "Replacement value (JSON)",
    "reason": "Reason",
    "submit": "Save override",
    "required": "A reason is required.",
    "error": "The override could not be saved.",
    "history": "Override history is retained.",
    "back": "Return to the subject",
    "stage_hint": "Use a stage number from 1 to 7.",
    "value_hint": "Enter a JSON object containing the revised fields.",
    "reason_hint": "Explain why the displayed risk view is being changed.",
    "not_available": "No overrideable risk view is available for this subject.",
}


def create_overrides_router(
    service: OverrideService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected override form and submission routes."""

    if not isinstance(service, OverrideService):
        raise TypeError("create_overrides_router requires an OverrideService.")
    router = APIRouter(tags=["overrides-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.post("/overrides", response_class=HTMLResponse, name="create_override")
    async def create_override(
        request: Request,
        principal: Principal = _OVERRIDE_DEP,
    ) -> Response:
        values = await _request_values(request)
        subject = _subject_payload(values)
        stage = _stage_value(values.get("stage"))
        user_action = _text_value(values.get("user_action")) or ""
        reason = _text_value(values.get("reason")) or ""
        try:
            record = service.record_override(
                principal,
                subject=subject,
                stage=stage,
                user_action=user_action,
                user_value=_json_object(values.get("user_value"), "user_value"),
                reason=reason,
                prompt_version=_text_value(values.get("prompt_version")),
                model_version=_text_value(values.get("model_version")),
                threshold_snapshot_id=_optional_value(values.get("threshold_snapshot_id")),
            )
        except NotFound:
            raise
        except DomainError as error:
            return _render_form(
                request,
                fallback_environment,
                principal=principal,
                subject=subject,
                stage=stage,
                user_action=user_action,
                user_value=values.get("user_value"),
                reason=reason,
                error=error.message,
                status_code=422,
            )
        target = service.redirect_path(principal, (record.subject_type, record.subject_id))
        return RedirectResponse(target, status_code=303)

    @router.get(
        "/overrides/form/{subject_type}/{subject_id}",
        response_class=HTMLResponse,
        name="override_form_fragment",
    )
    async def override_form_fragment(
        request: Request,
        subject_type: str,
        subject_id: UUID,
        stage: int = 1,
        principal: Principal = _OVERRIDE_DEP,
    ) -> HTMLResponse:
        return _form_for_subject(
            request,
            fallback_environment,
            service,
            principal=principal,
            subject=OverrideSubject(subject_type, subject_id),
            stage=stage,
        )

    @router.get(
        "/why/{subject_type}/{subject_id}/override",
        response_class=HTMLResponse,
        name="why_override_form",
    )
    async def why_override_form(
        request: Request,
        subject_type: str,
        subject_id: UUID,
        stage: int = 1,
        principal: Principal = _OVERRIDE_DEP,
    ) -> HTMLResponse:
        """Render the same control at the why-panel surface."""

        return _form_for_subject(
            request,
            fallback_environment,
            service,
            principal=principal,
            subject=OverrideSubject(subject_type, subject_id),
            stage=stage,
        )

    return router


def _form_for_subject(
    request: Request,
    fallback_environment: Environment,
    service: OverrideService,
    *,
    principal: Principal,
    subject: OverrideSubject,
    stage: int,
) -> HTMLResponse:
    try:
        view = service.current_view(principal, subject)
    except NotFound:
        raise
    except DomainError as error:
        return _render_form(
            request,
            fallback_environment,
            principal=principal,
            subject=subject,
            stage=stage,
            user_action="",
            user_value="{}",
            reason="",
            error=error.message,
            status_code=422,
        )
    return _render_form(
        request,
        fallback_environment,
        principal=principal,
        subject=subject,
        stage=stage,
        user_action="",
        user_value="{}",
        reason="",
        error="",
        view=view,
    )


def _render_form(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    subject: OverrideSubject | Mapping[str, object] | str | None,
    stage: object,
    user_action: str,
    user_value: object,
    reason: str,
    error: str,
    view: RevisedRiskView | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("_components/override_form.html")
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    theme = theme_for_request(request)
    values = {
        "request": request,
        "principal": principal,
        "locale": locale,
        "theme": theme,
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "subject": subject,
        "stage": stage,
        "user_action": user_action,
        "user_value": user_value if isinstance(user_value, str) else _dump_json(user_value),
        "reason": reason,
        "error": error,
        "view": view,
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


async def _request_values(request: Request) -> dict[str, object]:
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise ValidationError("The submitted override is too large.", field="payload")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json":
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError(
                "The override payload is not valid JSON.", field="payload"
            ) from error
        if not isinstance(decoded, Mapping):
            raise ValidationError("The override payload must be a JSON object.", field="payload")
        return {str(key): value for key, value in decoded.items() if key != "csrf_token"}
    if content_type == "application/x-www-form-urlencoded" or not content_type:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError(
                "The override payload is not valid UTF-8.", field="payload"
            ) from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values and key != "csrf_token"}
    form = await request.form()
    values: dict[str, object] = {}
    for key, value in form.multi_items():
        if key != "csrf_token" and isinstance(value, str):
            values[key] = value
    return values


def _subject_payload(values: Mapping[str, object]) -> Mapping[str, object] | str:
    subject = values.get("subject")
    if subject is not None and subject != "":
        if isinstance(subject, Mapping | str):
            return subject
        raise ValidationError("The override subject must be an object or text.", field="subject")
    return {
        "subject_type": values.get("subject_type"),
        "subject_id": values.get("subject_id"),
    }


def _json_object(value: object, field: str) -> Mapping[str, object] | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{field} must be valid JSON.", field=field) from error
        if decoded is None:
            return None
        if not isinstance(decoded, Mapping):
            raise ValidationError(f"{field} must be a JSON object.", field=field)
        return decoded
    raise ValidationError(f"{field} must be a JSON object.", field=field)


def _text_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _stage_value(value: object) -> int | str | None:
    return value if isinstance(value, int | str) and not isinstance(value, bool) else None


def _optional_value(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _dump_json(value: object) -> str:
    if value is None:
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"


__all__ = ["create_overrides_router"]
