"""Scoped case register and case-workspace routes (T-110, contract C-14).

The route owns HTTP concerns only.  Lifecycle transitions delegate to the
T-109 service; comments, assignments and actions are persisted as append-only
records here because their schemas are deliberately simple and have no
standalone service contract yet.  Every mutation is anchored to a scoped case
before it reads or writes any child record.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.audit.events import AuditEventType
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import DomainError, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.forecast import Intervention
from covenant_radar.db.models.identity import AppUser, UserPortfolioScope
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import (
    ActionTaken,
    Case,
    CaseComment,
    CaseEvent,
    Notification,
)
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.repositories.case import CaseRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.cases.lifecycle import CaseState, validate_state
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.cases import AuditWriter as CaseAuditWriter
from covenant_radar.services.cases import CaseService
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.case import (
    CaseDetailView,
    CaseListView,
    build_case_detail_view,
    build_case_list_view,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 128 * 1024
_MAX_COMMENT_LENGTH = 4_000
_MAX_ACTION_LENGTH = 2_000
_MENTION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])@([A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?)"
)

_READ_DEP = Depends(requires(Permission.VIEW_CASE))
_UPDATE_DEP = Depends(requires(Permission.UPDATE_CASE))

_LABELS = {
    "list_title": "Case register",
    "list_heading": "Case register",
    "list_eyebrow": "Work the warning",
    "list_subheading": "Every open warning has one owner, one clock and one durable history.",
    "detail_title": "Case workspace",
    "detail_eyebrow": "Case workspace",
    "case_reference": "Case reference",
    "borrower": "Borrower",
    "portfolio": "Portfolio",
    "state": "State",
    "band": "Risk band",
    "assignee": "Assignee",
    "due_at": "SLA due",
    "opened_at": "Opened",
    "updated_at": "Last updated",
    "version": "Record version",
    "overdue": "Overdue",
    "on_track": "Within SLA",
    "all_states": "All states",
    "all_assignees": "All assignees",
    "apply": "Apply filters",
    "clear": "Clear filters",
    "column_case": "Case",
    "column_borrower": "Borrower",
    "column_state": "State",
    "column_band": "Band",
    "column_assignee": "Assignee",
    "column_due": "SLA due",
    "column_updated": "Updated",
    "no_cases_title": "No cases in this view",
    "no_cases_message": "Change the filters or wait for the next scored warning to open a case.",
    "history_title": "Immutable case history",
    "history_empty": "No history events are recorded for this case.",
    "comments_title": "Desk notes",
    "comments_empty": "No comments have been added to this case.",
    "comment_label": "Add a desk note",
    "comment_hint": (
        "Use @username to mention a colleague. Mentions outside this case scope are stored "
        "but never notified."
    ),
    "comment_placeholder": "What changed, and what should the next reviewer know?",
    "comment_submit": "Add note",
    "actions_title": "Actions taken",
    "actions_empty": "No intervention has been recorded yet.",
    "action_catalogue": "Catalogue intervention",
    "action_free_text": "Free-text action",
    "action_select": "Choose an intervention",
    "action_detail": "Additional detail",
    "action_detail_hint": "Required for a free-text action; optional when citing the catalogue.",
    "action_placeholder": "Record the intervention, decision or follow-up.",
    "action_submit": "Log action",
    "linked_memos_title": "Linked warning memos",
    "memos_empty": "No warning memo is linked to this case.",
    "simulations_title": "Linked simulations",
    "simulations_empty": "No persisted simulation is linked to this borrower.",
    "documents_title": "Source documents",
    "documents_empty": "No source documents are linked to this borrower.",
    "assignment_title": "Ownership and state",
    "assign_to": "Assign to",
    "save_assignment": "Save ownership",
    "move_to": "Move to",
    "save_state": "Save state",
    "state_hint": "Only permitted next states are offered.",
    "closure_reason": "Closure reason",
    "closure_placeholder": "Explain why this case is being closed.",
    "closure_hint": "Required when moving to Closed.",
    "back_to_register": "Back to case register",
    "superseded_run": "Superseded run",
    "free_text_source": "Free text",
    "catalogue_source": "Catalogue intervention",
    "mention_notice": (
        "A mentioned colleague is outside this case scope, so no notification was sent. "
        "The mention remains in the note."
    ),
    "form_error": "The case update could not be saved.",
    "document_open": "Open document",
    "sim_probability": "Projected probability",
    "sim_crossing": "Projected crossing",
    "sim_delta": "Days moved",
    "memo_excerpt": "Memo excerpt",
    "run": "Run",
}


def create_cases_router(
    session: Session,
    *,
    case_service: CaseService | None = None,
    audit_writer: object | None = None,
    clock: Clock | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected case register and workspace routes."""

    if not is_database_session(session):
        raise TypeError("create_cases_router requires a SQLAlchemy Session.")
    resolved_clock = clock or (case_service.clock if case_service is not None else SystemClock())
    request_id = get_request_id() or new_request_id()
    audit_candidate = audit_writer or (
        getattr(case_service, "audit", None) if case_service is not None else None
    )
    if audit_candidate is None:
        audit_candidate = AuditRecorder(
            AuditRepository(session), clock=resolved_clock, request_id=request_id
        )
    if not callable(getattr(audit_candidate, "record", None)):
        raise TypeError("create_cases_router audit_writer must expose record().")
    resolved_audit = cast(CaseAuditWriter, audit_candidate)
    if case_service is None:
        case_service = CaseService(
            session,
            audit=resolved_audit,
            clock=resolved_clock,
            request_id=request_id,
        )
    elif case_service.session is not session:
        raise ValueError("create_cases_router case_service and session must be identical.")

    router = APIRouter(tags=["cases-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )
    repository = CaseRepository(session)

    @router.get("/cases", response_class=HTMLResponse, name="case_list")
    def case_list(
        request: Request,
        state: str | None = Query(None, max_length=20),
        assignee: str | None = Query(None, max_length=64),
        overdue: bool = Query(False),
        q: str | None = Query(None, max_length=100),
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        scope = resolve_scope(principal, session)
        normalized_state = _optional_state(state)
        assignee_id = _optional_uuid(assignee, "assignee")
        cases = _query_cases(
            session,
            scope,
            state=normalized_state,
            assignee_id=assignee_id,
            overdue=overdue,
            search=q,
            now=resolved_clock.now(),
        )
        assignee_options = _case_assignee_options(session, scope)
        filter_state = {
            "state": normalized_state or "",
            "assignee": str(assignee_id) if assignee_id else "",
            "overdue": "1" if overdue else "",
            "q": q.strip() if q else "",
        }
        view = build_case_list_view(
            cases,
            session,
            now=resolved_clock.now(),
            filters=filter_state,
            scope=scope,
        )
        return _render_list(
            request,
            fallback_environment,
            principal=principal,
            view=view,
            assignee_options=assignee_options,
            assignee_filter_options_enabled=True,
            error="",
        )

    @router.get("/cases/{reference}", response_class=HTMLResponse, name="case_detail")
    def case_detail(
        request: Request,
        reference: str,
        notice: str | None = Query(None, max_length=40),
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        scope = resolve_scope(principal, session)
        case = _scoped_case(repository, reference, scope)
        view = build_case_detail_view(case, session, scope=scope, now=resolved_clock.now())
        return _render_detail(
            request,
            fallback_environment,
            principal=principal,
            view=view,
            error="",
            mention_notice=notice == "mention_scope",
        )

    @router.post("/cases/{reference}", response_class=HTMLResponse, name="case_mutation")
    async def case_mutation(
        request: Request,
        reference: str,
        principal: Principal = _UPDATE_DEP,
    ) -> Response:
        values = await _form_values(request)
        scope = resolve_scope(principal, session)
        case = _scoped_case(repository, reference, scope)
        action = _text(values.get("action")) or _default_action(values)
        mention_notice = False
        try:
            if action == "state":
                target = _required_text(values.get("state"), "state")
                closure_reason = _text(values.get("closure_reason"))
                case_service.transition_case(
                    principal,
                    case.id,
                    validate_state(target),
                    closure_reason=closure_reason,
                    expected_version=_optional_int(values.get("expected_version")),
                    scope=scope,
                    now=resolved_clock.now(),
                )
            elif action == "assign":
                assignee_id = _required_uuid(values.get("assignee"), "assignee")
                _assign_case(
                    session,
                    repository,
                    case,
                    assignee_id,
                    principal,
                    scope=scope,
                    expected_version=_optional_int(values.get("expected_version")),
                    audit=resolved_audit,
                    clock=resolved_clock,
                )
            elif action == "comment":
                mention_notice = _add_comment(
                    session,
                    repository,
                    case,
                    principal,
                    scope=scope,
                    body=_required_bounded_text(
                        values.get("comment"), "comment", _MAX_COMMENT_LENGTH
                    ),
                    audit=resolved_audit,
                    clock=resolved_clock,
                )
            elif action == "log_action":
                _require_permission(principal, Permission.LOG_ACTION)
                _log_action(
                    session,
                    repository,
                    case,
                    principal,
                    intervention_code=_text(values.get("intervention_code")),
                    free_text=_bounded_text(
                        values.get("action_detail"), "action_detail", _MAX_ACTION_LENGTH
                    ),
                    outcome=_bounded_text(values.get("outcome"), "outcome", _MAX_ACTION_LENGTH),
                    scope=scope,
                    audit=resolved_audit,
                    clock=resolved_clock,
                )
            else:
                raise ValidationError(
                    "Unknown case action; expected state, assign, comment or log_action.",
                    field="action",
                )
        except DomainError as error:
            view = build_case_detail_view(case, session, scope=scope, now=resolved_clock.now())
            return _render_detail(
                request,
                fallback_environment,
                principal=principal,
                view=view,
                error=error.message,
                mention_notice=False,
                status_code=422,
            )
        redirect = f"/cases/{quote(case.reference, safe='')}"
        if mention_notice:
            redirect += "?notice=mention_scope"
        return RedirectResponse(redirect, status_code=303)

    return router


def _query_cases(
    session: Session,
    scope: Scope,
    *,
    state: str | None,
    assignee_id: UUID | None,
    overdue: bool,
    search: str | None,
    now: datetime,
) -> tuple[Case, ...]:
    statement = (
        select(Case)
        .join(Borrower, Borrower.id == Case.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(scope.predicate(Portfolio.path))
    )
    if state is not None:
        statement = statement.where(Case.state == state)
    if assignee_id is not None:
        statement = statement.where(Case.assignee_id == assignee_id)
    if overdue:
        statement = statement.where(
            Case.state != CaseState.CLOSED.value,
            Case.due_at.is_not(None),
            Case.due_at <= now,
        )
    if search and search.strip():
        term = _like_term(search)
        statement = statement.where(
            Borrower.reference.ilike(term, escape="\\")
            | Borrower.legal_name.ilike(term, escape="\\")
            | Case.reference.ilike(term, escape="\\")
        )
    statement = statement.order_by(Case.updated_at.desc(), Case.id.desc())
    return tuple(session.scalars(statement).all())


def _case_assignee_options(
    session: Session, scope: Scope
) -> tuple[tuple[str, str], ...]:
    """Return assignees that occur on cases in the caller's current scope."""
    statement = (
        select(AppUser)
        .join(Case, Case.assignee_id == AppUser.id)
        .join(Borrower, Borrower.id == Case.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(scope.predicate(Portfolio.path))
        .distinct()
        .order_by(AppUser.full_name, AppUser.username, AppUser.id)
    )
    users = tuple(session.scalars(statement).all())
    return (
        ("", _LABELS["all_assignees"]),
        *((str(user.id), f"{user.full_name} (@{user.username})") for user in users),
    )


def _scoped_case(repository: CaseRepository, reference: str, scope: Scope) -> Case:
    case = repository.by_reference(reference, scope=scope)
    if case is None:
        raise NotFound(f"Case {reference!r} was not found within the current scope.")
    return case


def _assign_case(
    session: Session,
    repository: CaseRepository,
    case: Case,
    assignee_id: UUID,
    principal: Principal,
    *,
    scope: Scope,
    expected_version: int | None,
    audit: CaseAuditWriter,
    clock: Clock,
) -> None:
    now = clock.now()
    _aware(now, "clock")
    with session.begin_nested():
        locked = repository.get_for_update(case.id, scope=scope)
        if locked is None:
            raise NotFound(f"Case {case.reference!r} was not found within the current scope.")
        _check_version(locked, expected_version)
        options = build_case_detail_view(locked, session, scope=scope, now=now).assignable_users
        allowed_ids = {UUID(option.value) for option in options}
        if assignee_id not in allowed_ids:
            raise ValidationError(
                "The selected assignee is not an active user in this case's portfolio scope.",
                field="assignee",
            )
        if locked.assignee_id == assignee_id:
            return
        locked.assignee_id = assignee_id
        locked.updated_at = now
        locked.updated_by_id = principal.id
        locked.version += 1
        session.flush()
        _append_event(
            session,
            locked,
            "assigned",
            principal.id,
            now,
            {"assignee_id": str(assignee_id)},
        )
        _audit(audit, locked, "assigned", principal.id, {"assignee_id": str(assignee_id)})


def _add_comment(
    session: Session,
    repository: CaseRepository,
    case: Case,
    principal: Principal,
    *,
    scope: Scope,
    body: str,
    audit: CaseAuditWriter,
    clock: Clock,
) -> bool:
    now = clock.now()
    _aware(now, "clock")
    mentions = tuple(dict.fromkeys(match.group(1) for match in _MENTION_PATTERN.finditer(body)))
    portfolio_path = _case_portfolio_path(session, case, scope)
    users = _mentioned_users(session, mentions)
    outside_scope: list[str] = []
    recipients: list[UUID] = []
    for handle in mentions:
        user = users.get(handle.casefold())
        if user is None:
            continue
        if _user_has_portfolio_scope(session, user.id, portfolio_path):
            recipients.append(user.id)
        else:
            outside_scope.append(handle)

    with session.begin_nested():
        locked = repository.get_for_update(case.id, scope=scope)
        if locked is None:
            raise NotFound(f"Case {case.reference!r} was not found within the current scope.")
        comment = CaseComment(
            id=new_id(),
            case_id=locked.id,
            author_id=principal.id,
            body=body,
            mentions=list(mentions) or None,
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=get_request_id() or new_request_id(),
        )
        session.add(comment)
        session.flush()
        _append_event(
            session,
            locked,
            "comment_added",
            principal.id,
            now,
            {"comment_id": str(comment.id), "mentions": list(mentions)},
        )
        _audit(
            audit,
            locked,
            "comment_added",
            principal.id,
            {"comment_id": str(comment.id), "mention_count": len(mentions)},
        )
        for recipient_id in tuple(dict.fromkeys(recipients)):
            if recipient_id == principal.id:
                continue
            session.add(
                Notification(
                    id=new_id(),
                    recipient_id=recipient_id,
                    channel="in_app",
                    template="case_comment_mention",
                    subject_type="case",
                    subject_id=locked.id,
                    payload={"case_reference": locked.reference, "comment_id": str(comment.id)},
                    state="pending",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                    created_by_id=principal.id,
                    updated_by_id=principal.id,
                    request_id=get_request_id() or new_request_id(),
                )
            )
        session.flush()
    return bool(outside_scope)


def _log_action(
    session: Session,
    repository: CaseRepository,
    case: Case,
    principal: Principal,
    *,
    intervention_code: str | None,
    free_text: str | None,
    outcome: str | None,
    scope: Scope,
    audit: CaseAuditWriter,
    clock: Clock,
) -> None:
    if intervention_code is None and not free_text:
        raise ValidationError(
            "Choose a catalogue intervention or add free text.", field="action_detail"
        )
    intervention = None
    if intervention_code is not None:
        normalized_code = intervention_code.strip().upper()
        if not normalized_code:
            intervention_code = None
        else:
            intervention = session.scalar(
                select(Intervention).where(Intervention.code == normalized_code)
            )
            if intervention is None:
                raise ValidationError(
                    f"Intervention {normalized_code!r} was not found in the catalogue.",
                    field="intervention_code",
                )
    if intervention is None and not free_text:
        raise ValidationError("Free-text action detail is required.", field="action_detail")
    now = clock.now()
    _aware(now, "clock")
    with session.begin_nested():
        locked = repository.get_for_update(case.id, scope=scope)
        if locked is None:
            raise NotFound(f"Case {case.reference!r} was not found within the current scope.")
        action = ActionTaken(
            id=new_id(),
            case_id=locked.id,
            intervention_id=intervention.id if intervention is not None else None,
            free_text=free_text,
            taken_at=now,
            actor_id=principal.id,
            outcome=outcome,
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=get_request_id() or new_request_id(),
        )
        session.add(action)
        session.flush()
        payload = {
            "action_id": str(action.id),
            "source": "catalogue" if intervention is not None else "free_text",
        }
        if intervention is not None:
            payload["intervention_code"] = intervention.code
        _append_event(session, locked, "action_taken", principal.id, now, payload)
        _audit(audit, locked, "action_taken", principal.id, payload)


def _append_event(
    session: Session,
    case: Case,
    event_type: str,
    actor_id: UUID,
    occurred_at: datetime,
    payload: Mapping[str, object],
) -> CaseEvent:
    event = CaseEvent(
        id=new_id(),
        case_id=case.id,
        event_type=event_type,
        actor_id=actor_id,
        payload=dict(payload),
        occurred_at=occurred_at,
        created_at=occurred_at,
        updated_at=occurred_at,
        created_by_id=actor_id,
        updated_by_id=actor_id,
        request_id=get_request_id() or new_request_id(),
    )
    session.add(event)
    session.flush()
    return event


def _audit(
    audit: CaseAuditWriter,
    case: Case,
    action: str,
    actor_id: UUID,
    payload: Mapping[str, object],
) -> None:
    audit.record(
        AuditEventType.CASE_LIFECYCLE_CHANGED.value,
        ("case", case.id),
        {"action": action, **dict(payload)},
        actor=actor_id,
        request_id=get_request_id() or new_request_id(),
    )


def _case_portfolio_path(session: Session, case: Case, scope: Scope) -> str:
    path = session.scalar(
        select(Portfolio.path)
        .join(Borrower, Borrower.portfolio_id == Portfolio.id)
        .where(Borrower.id == case.borrower_id, scope.predicate(Portfolio.path))
    )
    if not isinstance(path, str):
        raise NotFound(f"Case {case.reference!r} was not found within the current scope.")
    return path


def _mentioned_users(session: Session, handles: tuple[str, ...]) -> dict[str, AppUser]:
    if not handles:
        return {}
    rows = session.scalars(
        select(AppUser).where(
            AppUser.is_active.is_(True),
            func.lower(AppUser.username).in_([handle.casefold() for handle in handles]),
        )
    ).all()
    return {user.username.casefold(): user for user in rows}


def _user_has_portfolio_scope(session: Session, user_id: UUID, target_path: str) -> bool:
    rows = session.execute(
        select(Portfolio.path, UserPortfolioScope.include_descendants)
        .join(Portfolio, Portfolio.id == UserPortfolioScope.portfolio_id)
        .where(UserPortfolioScope.user_id == user_id)
    ).all()
    normalized_target = target_path.rstrip("/") + "/"
    return any(
        normalized_target.startswith(path.rstrip("/") + "/")
        if include_descendants
        else normalized_target == path.rstrip("/") + "/"
        for path, include_descendants in rows
    )


def _render_list(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    view: CaseListView,
    assignee_options: tuple[tuple[str, str], ...],
    assignee_filter_options_enabled: bool,
    error: str,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/cases/index.html")
    values = _template_values(
        request, principal, csrf_token=getattr(request.state, "csrf_token", "")
    )
    values.update(
        {
            "view": view,
            "assignee_options": assignee_options,
            "assignee_filter_options_enabled": assignee_filter_options_enabled,
            "error": error,
        }
    )
    return HTMLResponse(template.render(**values), status_code=status_code)


def _render_detail(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    view: CaseDetailView,
    error: str,
    mention_notice: bool,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/cases/detail.html")
    values = _template_values(
        request, principal, csrf_token=getattr(request.state, "csrf_token", "")
    )
    values.update(
        {
            "view": view,
            "error": error,
            "mention_notice": mention_notice,
            "can_update": principal.has(Permission.UPDATE_CASE),
            "can_log_action": principal.has(Permission.UPDATE_CASE)
            and principal.has(Permission.LOG_ACTION),
        }
    )
    return HTMLResponse(template.render(**values), status_code=status_code)


def _template_values(
    request: Request, principal: Principal, *, csrf_token: str
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
        "csrf_token": csrf_token,
    }


async def _form_values(request: Request) -> dict[str, object]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted case update is too large.", field="form")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json":
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError(
                "The submitted case update is not valid JSON.", field="form"
            ) from error
        if not isinstance(value, Mapping):
            raise ValidationError("The submitted case update must be an object.", field="form")
        return {str(key): item for key, item in value.items() if key != "csrf_token"}
    if content_type == "application/x-www-form-urlencoded" or not content_type:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError(
                "The submitted case update is not valid UTF-8.", field="form"
            ) from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values and key != "csrf_token"}
    form = await request.form()
    return {
        key: value
        for key, value in form.multi_items()
        if key != "csrf_token" and isinstance(value, str)
    }


def _default_action(values: Mapping[str, object]) -> str:
    if values.get("comment"):
        return "comment"
    if values.get("intervention_code") or values.get("action_detail"):
        return "log_action"
    if values.get("assignee"):
        return "assign"
    return "state"


def _optional_state(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return validate_state(value).value


def _optional_uuid(value: object, field: str) -> UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _required_uuid(value, field)


def _required_uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return UUID(value.strip())
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} is required.", field=field)


def _optional_int(value: object) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValidationError("expected_version must be an integer.", field="expected_version")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as error:
        raise ValidationError(
            "expected_version must be an integer.", field="expected_version"
        ) from error
    if parsed < 1:
        raise ValidationError(
            "expected_version must be a positive integer.", field="expected_version"
        )
    return parsed


def _check_version(case: Case, expected_version: int | None) -> None:
    if expected_version is not None and case.version != expected_version:
        from covenant_radar.core.errors import Conflict

        raise Conflict(
            f"Case {case.reference} changed; expected version {expected_version}, "
            f"found {case.version}."
        )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(value: object, field: str) -> str:
    result = _text(value)
    if result is None:
        raise ValidationError(f"{field} is required.", field=field)
    return result


def _bounded_text(value: object, field: str, maximum: int) -> str | None:
    result = _text(value)
    if result is not None and len(result) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    return result


def _required_bounded_text(value: object, field: str, maximum: int) -> str:
    result = _bounded_text(value, field, maximum)
    if result is None:
        raise ValidationError(f"{field} is required.", field=field)
    return result


def _require_permission(principal: Principal, permission: Permission) -> None:
    if not principal.has(permission):
        raise HTTPException(status_code=403, detail=f"Missing permission: {permission.value}.")


def _like_term(value: str) -> str:
    escaped = value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = ["create_cases_router"]
