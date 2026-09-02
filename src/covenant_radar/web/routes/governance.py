"""Governance workspace for thresholds, model registry, and evaluations.

Reading is open to anyone who may propose or approve a threshold change —
otherwise a proposer could not see the values they are proposing against.
Proposing and approving stay two distinct permissions (`spec §16.1`), and the
approve control is withheld from a proposal's own maker even when they hold
the approve permission, because the distinct-actor rule is a UI promise as
well as a database constraint.
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
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.config.thresholds import DEFAULT_THRESHOLD_PATH, ThresholdStore
from covenant_radar.core.errors import DomainError, NotFound, ValidationError
from covenant_radar.db.repositories.thresholds import SqlAlchemyThresholdRepository
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.errors import status_for_error
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.governance import GovernanceView, load_governance_view

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 64 * 1024
# Reading follows `spec §16.1`'s "Propose a threshold change" row (risk,
# risk head, admin); approving stays its own, narrower permission held only
# by the risk head.
_READ = requires(Permission.PROPOSE_THRESHOLDS)
_PROPOSE = requires(Permission.PROPOSE_THRESHOLDS)
_APPROVE = requires(Permission.APPROVE_THRESHOLDS)
_READ_DEP = Depends(_READ)
_PROPOSE_DEP = Depends(_PROPOSE)
_APPROVE_DEP = Depends(_APPROVE)


def create_governance_router(
    session: Session,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected governance workspace: view, propose, approve."""

    if not is_database_session(session):
        raise TypeError("create_governance_router requires a SQLAlchemy Session.")
    router = APIRouter(tags=["governance-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/governance", response_class=HTMLResponse, name="governance")
    def governance(request: Request, principal: Principal = _READ_DEP) -> HTMLResponse:
        return _render(request, fallback_environment, session, principal=principal)

    @router.post(
        "/governance/thresholds/proposals",
        response_class=HTMLResponse,
        name="governance_propose_threshold",
    )
    async def propose_threshold(
        request: Request,
        principal: Principal = _PROPOSE_DEP,
    ) -> Response:
        values = await _form_values(request)
        store = _store(session)
        try:
            patch = _parse_values(values.get("values", ""))
            note = _optional_text(values.get("note"))
            store.propose(patch, principal, note=note)
        except NotFound:
            raise
        except (DomainError, KeyError) as error:
            message = error.message if isinstance(error, DomainError) else str(error)
            status_code = status_for_error(error) if isinstance(error, DomainError) else 422
            return _render(
                request,
                fallback_environment,
                session,
                principal=principal,
                error=message,
                status_code=status_code,
            )
        return RedirectResponse("/governance", status_code=303)

    @router.post(
        "/governance/thresholds/proposals/{proposal_id}/approve",
        response_class=HTMLResponse,
        name="governance_approve_threshold",
    )
    async def approve_threshold(
        request: Request,
        proposal_id: UUID,
        principal: Principal = _APPROVE_DEP,
    ) -> Response:
        await _form_values(request)
        store = _store(session)
        try:
            store.approve(proposal_id, principal)
        except NotFound:
            raise
        except DomainError as error:
            return _render(
                request,
                fallback_environment,
                session,
                principal=principal,
                error=error.message,
                status_code=status_for_error(error),
            )
        return RedirectResponse("/governance", status_code=303)

    return router


def _store(session: Session) -> ThresholdStore:
    return ThresholdStore(
        repository=SqlAlchemyThresholdRepository(session),
        path=DEFAULT_THRESHOLD_PATH,
    )


def _render(
    request: Request,
    fallback_environment: Environment,
    session: Session,
    *,
    principal: Principal,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/governance/index.html")
    # Touching the store first guarantees the shipped default snapshot exists
    # (`T-012`'s own first-start behaviour) before the read model looks for it,
    # so this screen never depends on another subsystem having run first.
    _store(session).values()
    view: GovernanceView = load_governance_view(session, principal_id=principal.id)
    return HTMLResponse(
        template.render(
            request=request,
            principal=principal,
            view=view,
            can_propose=principal.has(Permission.PROPOSE_THRESHOLDS),
            can_approve=principal.has(Permission.APPROVE_THRESHOLDS),
            error=error,
            locale=request.cookies.get("covenant_radar_locale", "en"),
            theme=theme_for_request(request),
            text_direction="ltr",
            csrf_token=getattr(request.state, "csrf_token", ""),
        ),
        status_code=status_code,
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


def _parse_values(raw: str) -> Mapping[str, object]:
    text = raw.strip()
    if not text:
        raise ValidationError("A proposed value is required.", field="values")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValidationError(f"values must be valid JSON: {error.msg}.", field="values") from error
    if not isinstance(decoded, Mapping):
        raise ValidationError(
            "values must be a JSON object keyed by threshold name.", field="values"
        )
    return decoded


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = ["create_governance_router"]
