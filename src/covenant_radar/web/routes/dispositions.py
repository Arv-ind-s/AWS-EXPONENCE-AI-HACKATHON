"""Web routes for scoped warning dispositions (T-112, contract C-13)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import ValidationError
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.dispositions import (
    DISPOSITION_REASON_CODES,
    DispositionService,
    DispositionSubject,
)
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_BODY_BYTES = 1_100_000
_DISPOSITION = requires(Permission.RECORD_DISPOSITION)
_DISPOSITION_DEP = Depends(_DISPOSITION)
_SURFACES = frozenset({"case-file", "memo"})


def create_dispositions_router(
    service: DispositionService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected disposition endpoint and reusable form fragments."""

    if not isinstance(service, DispositionService):
        raise TypeError("create_dispositions_router requires a DispositionService.")
    router = APIRouter(tags=["dispositions-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.post("/dispositions", name="create_disposition")
    async def create_disposition(
        request: Request,
        principal: Principal = _DISPOSITION_DEP,
    ) -> Response:
        values = await _request_values(request)
        subject = _subject_payload(values)
        service.record_disposition(
            principal,
            subject=subject,
            outcome=_optional_text(values.get("outcome"), "outcome"),
            reason_code=_optional_text(values.get("reason_code"), "reason_code"),
            note=_optional_text(values.get("note"), "note"),
        )
        return Response(status_code=204)

    @router.get(
        "/dispositions/form/{subject_type}/{subject_id}",
        response_class=HTMLResponse,
        name="disposition_form_fragment",
    )
    def disposition_form_fragment(
        request: Request,
        subject_type: str,
        subject_id: UUID,
        surface: str = Query(default="case-file"),
        principal: Principal = _DISPOSITION_DEP,
    ) -> HTMLResponse:
        resolved_surface = _surface(surface)
        subject = DispositionSubject(subject_type, subject_id)
        history = service.list_dispositions(principal, subject)
        return _render_form(
            request,
            fallback_environment,
            subject=subject,
            surface=resolved_surface,
            history=history,
        )

    return router


def _render_form(
    request: Request,
    fallback_environment: Environment,
    *,
    subject: DispositionSubject,
    surface: str,
    history: tuple[object, ...],
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.from_string(
        '{% from "_components/feedback_control.html" import disposition_control %}'
        "{{ disposition_control("
        '"disposition-control", "/dispositions", subject.subject_type, '
        "subject.subject_id, csrf_token=csrf_token, surface=surface, "
        "reason_codes=reason_codes, history=history) }}"
    )
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    rendered = template.render(
        request=request,
        principal=getattr(request.state, "principal", None),
        locale=locale,
        theme=theme_for_request(request),
        text_direction="ltr",
        csrf_token=getattr(request.state, "csrf_token", ""),
        subject=subject,
        surface=surface,
        reason_codes=DISPOSITION_REASON_CODES,
        history=history,
    )
    return HTMLResponse(rendered)


async def _request_values(request: Request) -> dict[str, object]:
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise ValidationError("The submitted disposition is too large.", field="payload")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json":
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError(
                "The disposition payload is not valid JSON.", field="payload"
            ) from error
        if not isinstance(decoded, Mapping):
            raise ValidationError("The disposition payload must be a JSON object.", field="payload")
        return {str(key): value for key, value in decoded.items() if key != "csrf_token"}
    if content_type == "application/x-www-form-urlencoded" or not content_type:
        try:
            decoded_text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError(
                "The disposition payload is not valid UTF-8.", field="payload"
            ) from error
        parsed = parse_qs(decoded_text, keep_blank_values=True)
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
        raise ValidationError("The disposition subject must be an object or text.", field="subject")
    return {
        "subject_type": values.get("subject_type"),
        "subject_id": values.get("subject_id"),
    }


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text.", field=field)
    return value if value.strip() else None


def _surface(value: object) -> str:
    if not isinstance(value, str) or value not in _SURFACES:
        raise ValidationError("Disposition surface must be case-file or memo.", field="surface")
    return value


__all__ = ["create_dispositions_router"]
