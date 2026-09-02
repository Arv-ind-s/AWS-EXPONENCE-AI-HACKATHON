"""Read models for the scoped case-workspace screens.

The browser templates receive immutable, presentation-ready values rather
than ORM instances.  Every child query is anchored to a case that has already
passed the portfolio-scope predicate, and the detail loader keeps all of the
case's evidence in one deterministic read shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastRun,
    Intervention,
    Simulation,
)
from covenant_radar.db.models.identity import AppUser, UserPortfolioScope
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import ActionTaken, Case, CaseComment, CaseEvent, Memo
from covenant_radar.db.repositories.case import CaseRepository
from covenant_radar.db.scoping import Scope, grant_reaches_path
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.cases.lifecycle import permitted_transitions

IST = ZoneInfo("Asia/Kolkata")
_PERCENT_QUANTUM = Decimal("0.1")

_STATE_LABELS = {
    "open": "Open",
    "in_progress": "In progress",
    "monitoring": "Monitoring",
    "escalated": "Escalated",
    "closed": "Closed",
}
_BAND_LABELS = {
    "act": "Act now",
    "amber": "Amber",
    "watch": "Watch",
}
_EVENT_LABELS = {
    "opened": "Case opened",
    "reopened": "Case reopened",
    "band_changed": "Risk band changed",
    "state_changed": "State changed",
    "sla_breached": "SLA breached",
    "assignee_fallback": "Default owner assigned",
    "assigned": "Case assigned",
    "comment_added": "Comment added",
    "action_taken": "Action recorded",
}


@dataclass(frozen=True, slots=True)
class SelectOption:
    """One safe option for a server-rendered select control."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class CaseListRow:
    """One row in the scoped case register."""

    reference: str
    borrower_reference: str
    borrower_name: str
    state: str
    state_label: str
    band: str
    band_label: str
    assignee: str
    due_at: str
    updated_at: str
    overdue: bool
    href: str


@dataclass(frozen=True, slots=True)
class CaseListView:
    """The complete case-list read model."""

    rows: tuple[CaseListRow, ...]
    total: int
    open_count: int
    overdue_count: int
    filters: Mapping[str, str]
    empty: bool


@dataclass(frozen=True, slots=True)
class CaseHistoryItem:
    """An immutable case-history row with a human-readable payload summary."""

    event_type: str
    event_label: str
    actor: str
    occurred_at: str
    detail: str


@dataclass(frozen=True, slots=True)
class CaseCommentView:
    """A case comment, including only the mention handles that were stored."""

    body: str
    author: str
    created_at: str
    mentions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaseActionView:
    """An append-only intervention record."""

    label: str
    detail: str
    source: str
    actor: str
    taken_at: str


@dataclass(frozen=True, slots=True)
class CaseMemoView:
    """A memo linked to the case and the state of the run that produced it."""

    memo_id: str
    run_date: str
    run_state: str
    run_state_label: str
    headline: str
    excerpt: str
    is_superseded: bool


@dataclass(frozen=True, slots=True)
class CaseSimulationView:
    """A persisted what-if result linked through the borrower's forecast."""

    intervention_code: str
    intervention_text: str
    projected_cross_date: str
    probability: str
    delta_days: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CaseDocumentView:
    """A source document linked to the borrower."""

    document_id: str
    filename: str
    doc_type: str
    extraction_state: str
    scan_result: str
    page_count: str
    uploaded_at: str
    href: str


@dataclass(frozen=True, slots=True)
class CaseDetailView:
    """Everything required to render a case workspace."""

    reference: str
    borrower_reference: str
    borrower_name: str
    portfolio_code: str
    state: str
    state_label: str
    band: str
    band_label: str
    assignee: str
    assignee_id: str
    due_at: str
    opened_at: str
    updated_at: str
    overdue: bool
    version: int
    permitted_states: tuple[SelectOption, ...]
    assignable_users: tuple[SelectOption, ...]
    interventions: tuple[SelectOption, ...]
    history: tuple[CaseHistoryItem, ...]
    comments: tuple[CaseCommentView, ...]
    actions: tuple[CaseActionView, ...]
    memos: tuple[CaseMemoView, ...]
    simulations: tuple[CaseSimulationView, ...]
    documents: tuple[CaseDocumentView, ...]


def build_case_list_view(
    cases: Sequence[Case],
    session: Session,
    *,
    now: datetime | None = None,
    filters: Mapping[str, str] | None = None,
    scope: Scope | None = None,
) -> CaseListView:
    """Build a stable list view from already scoped case rows."""

    if not is_database_session(session):
        raise TypeError("build_case_list_view requires a SQLAlchemy Session.")
    if now is None:
        now = datetime.now(UTC)
    _aware(now, "now")
    rows = tuple(cases)
    borrower_names = _borrower_names(session, rows, scope=scope)
    assignee_names = _user_names(
        session, {case.assignee_id for case in rows if case.assignee_id is not None}
    )

    rendered = tuple(_case_list_row(case, borrower_names, assignee_names, now=now) for case in rows)
    return CaseListView(
        rows=rendered,
        total=len(rendered),
        open_count=sum(case.state != "closed" for case in rows),
        overdue_count=sum(row.overdue for row in rendered),
        filters=dict(filters or {}),
        empty=not rendered,
    )


def build_case_detail_view(
    case: Case,
    session: Session,
    *,
    scope: Scope,
    now: datetime | None = None,
) -> CaseDetailView:
    """Load one scoped case and its linked workflow records."""

    if not isinstance(case, Case):
        raise TypeError("build_case_detail_view case must be a Case.")
    if not is_database_session(session):
        raise TypeError("build_case_detail_view requires a SQLAlchemy Session.")
    if not isinstance(scope, Scope):
        raise TypeError("build_case_detail_view scope must be a portfolio Scope.")
    if now is None:
        now = datetime.now(UTC)
    _aware(now, "now")

    borrower_row = session.execute(
        select(Borrower, Portfolio.code, Portfolio.path)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(Borrower.id == case.borrower_id, scope.predicate(Portfolio.path))
    ).one_or_none()
    if borrower_row is None:
        raise NotFound(f"Case {case.reference!r} was not found within the current scope.")
    borrower, portfolio_code, portfolio_path = borrower_row

    author_ids: set[UUID] = set()
    comments = _comments(session, case.id, author_ids)
    actions = _actions(session, case.id, author_ids)
    history = _history(session, case.id, scope, author_ids)
    memos = _memos(session, case.id)
    simulations = _simulations(session, case.borrower_id)
    documents = _documents(session, case.borrower_id)
    names = _user_names(session, {case.assignee_id, *author_ids} - {None})

    return CaseDetailView(
        reference=case.reference,
        borrower_reference=borrower.reference,
        borrower_name=borrower.legal_name,
        portfolio_code=portfolio_code,
        state=case.state,
        state_label=_STATE_LABELS.get(case.state, case.state.replace("_", " ").title()),
        band=case.band_at_open or "",
        band_label=_BAND_LABELS.get(case.band_at_open or "", "Not banded"),
        assignee=names.get(case.assignee_id, "Unassigned") if case.assignee_id else "Unassigned",
        assignee_id=str(case.assignee_id) if case.assignee_id else "",
        due_at=_format_instant(case.due_at) if case.due_at else "No SLA recorded",
        opened_at=_format_instant(case.created_at),
        updated_at=_format_instant(case.updated_at),
        overdue=case.state != "closed" and case.due_at is not None and case.due_at <= now,
        version=case.version,
        permitted_states=tuple(
            SelectOption(value=value, label=_STATE_LABELS.get(value, value.title()))
            for value in permitted_transitions(case.state)
        ),
        assignable_users=_assignable_users(session, portfolio_path),
        interventions=_interventions(session),
        history=tuple(_history_view(item, names) for item in history),
        comments=tuple(_comment_view(item, author, names) for item, author in comments),
        actions=tuple(
            _action_view(item, code, text, actor, names) for item, code, text, actor in actions
        ),
        memos=tuple(_memo_view(item, run_date, run_state) for item, run_date, run_state in memos),
        simulations=tuple(
            _simulation_view(item, code, text, run_date)
            for item, code, text, run_date in simulations
        ),
        documents=tuple(_document_view(item) for item in documents),
    )


def _case_list_row(
    case: Case,
    borrower_names: Mapping[UUID, tuple[str, str]],
    assignee_names: Mapping[UUID, str],
    *,
    now: datetime,
) -> CaseListRow:
    borrower_reference, borrower_name = borrower_names.get(
        case.borrower_id, ("Unknown borrower", "Unknown borrower")
    )
    overdue = case.state != "closed" and case.due_at is not None and case.due_at <= now
    return CaseListRow(
        reference=case.reference,
        borrower_reference=borrower_reference,
        borrower_name=borrower_name,
        state=case.state,
        state_label=_STATE_LABELS.get(case.state, case.state.replace("_", " ").title()),
        band=case.band_at_open or "",
        band_label=_BAND_LABELS.get(case.band_at_open or "", "Not banded"),
        assignee=assignee_names.get(case.assignee_id, "Unassigned")
        if case.assignee_id
        else "Unassigned",
        due_at=_format_instant(case.due_at) if case.due_at else "No SLA recorded",
        updated_at=_format_instant(case.updated_at),
        overdue=overdue,
        href=f"/cases/{quote(case.reference, safe='')}",
    )


def _borrower_names(
    session: Session,
    cases: Sequence[Case],
    *,
    scope: Scope | None,
) -> dict[UUID, tuple[str, str]]:
    ids = {case.borrower_id for case in cases}
    if not ids:
        return {}
    statement = (
        select(Borrower.id, Borrower.reference, Borrower.legal_name)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(Borrower.id.in_(ids))
    )
    if scope is not None:
        statement = statement.where(scope.predicate(Portfolio.path))
    return {
        borrower_id: (reference, legal_name)
        for borrower_id, reference, legal_name in session.execute(statement).tuples().all()
    }


def _user_names(session: Session, ids: set[UUID | None]) -> dict[UUID, str]:
    clean_ids = {value for value in ids if isinstance(value, UUID)}
    if not clean_ids:
        return {}
    return dict(
        session.execute(select(AppUser.id, AppUser.full_name).where(AppUser.id.in_(clean_ids)))
        .tuples()
        .all()
    )


def _comments(
    session: Session, case_id: UUID, author_ids: set[UUID]
) -> tuple[tuple[CaseComment, UUID], ...]:
    rows = session.execute(
        select(CaseComment, AppUser.id)
        .join(AppUser, AppUser.id == CaseComment.author_id)
        .where(CaseComment.case_id == case_id)
        .order_by(CaseComment.created_at, CaseComment.id)
    ).all()
    author_ids.update(author_id for _comment, author_id in rows)
    return tuple(rows)


def _actions(
    session: Session, case_id: UUID, actor_ids: set[UUID]
) -> tuple[tuple[ActionTaken, str | None, str | None, UUID | None], ...]:
    rows = session.execute(
        select(ActionTaken, Intervention.code, Intervention.text, ActionTaken.actor_id)
        .outerjoin(Intervention, Intervention.id == ActionTaken.intervention_id)
        .where(ActionTaken.case_id == case_id)
        .order_by(ActionTaken.taken_at, ActionTaken.id)
    ).all()
    actor_ids.update(actor_id for _action, _code, _text, actor_id in rows if actor_id is not None)
    return tuple(rows)


def _history(
    session: Session, case_id: UUID, scope: Scope, actor_ids: set[UUID]
) -> tuple[CaseEvent, ...]:
    rows = CaseRepository(session).events_for(case_id, scope=scope)
    actor_ids.update(event.actor_id for event in rows if event.actor_id is not None)
    return rows


def _memos(session: Session, case_id: UUID) -> tuple[tuple[Memo, object | None, str | None], ...]:
    return tuple(
        session.execute(
            select(Memo, ForecastRun.as_of_date, ForecastRun.state)
            .outerjoin(ForecastRun, ForecastRun.id == Memo.run_id)
            .where(Memo.case_id == case_id)
            .order_by(Memo.created_at.desc(), Memo.id.desc())
        ).all()
    )


def _simulations(
    session: Session, borrower_id: UUID
) -> tuple[tuple[Simulation, str, str, object | None], ...]:
    return tuple(
        session.execute(
            select(Simulation, Intervention.code, Intervention.text, ForecastRun.as_of_date)
            .join(Forecast, Forecast.id == Simulation.forecast_id)
            .join(ForecastRun, ForecastRun.id == Forecast.run_id)
            .join(Intervention, Intervention.id == Simulation.intervention_id)
            # The covenant path is the authoritative borrower ownership chain.
            # These explicit joins avoid relying on ORM relationship configuration.
            .join(CovenantVersion, CovenantVersion.id == Forecast.covenant_version_id)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .join(Facility, Facility.id == Covenant.facility_id)
            .where(Facility.borrower_id == borrower_id)
            .order_by(Simulation.created_at.desc(), Simulation.id.desc())
        ).all()
    )


def _documents(session: Session, borrower_id: UUID) -> tuple[Document, ...]:
    return tuple(
        session.scalars(
            select(Document)
            .where(Document.borrower_id == borrower_id)
            .order_by(Document.created_at.desc(), Document.id.desc())
        ).all()
    )


def _assignable_users(session: Session, portfolio_path: str) -> tuple[SelectOption, ...]:
    rows = session.execute(
        select(
            AppUser.id,
            AppUser.full_name,
            AppUser.username,
            Portfolio.path,
            UserPortfolioScope.include_descendants,
        )
        .join(UserPortfolioScope, UserPortfolioScope.user_id == AppUser.id)
        .join(Portfolio, Portfolio.id == UserPortfolioScope.portfolio_id)
        .where(AppUser.is_active.is_(True))
        .order_by(AppUser.full_name, AppUser.id)
    ).all()
    result: list[SelectOption] = []
    seen: set[UUID] = set()
    for user_id, full_name, username, granted_path, include_descendants in rows:
        if not path_grants(granted_path, portfolio_path, bool(include_descendants)):
            continue
        if user_id in seen:
            continue
        result.append(SelectOption(str(user_id), f"{full_name} · @{username}"))
        seen.add(user_id)
    return tuple(result)


def path_grants(granted_path: str, target_path: str, include_descendants: bool) -> bool:
    """Whether one portfolio grant reaches one target portfolio path.

    The rule itself lives in `db/scoping.py`, because `services/bulk.py` must
    enforce the same test on assignment that this one uses to decide who is
    offered.  Kept as a name here for the two view models that already call it.
    """

    return grant_reaches_path(granted_path, target_path, include_descendants)


def _interventions(session: Session) -> tuple[SelectOption, ...]:
    return tuple(
        SelectOption(str(row.code), f"{row.code} · {row.text}")
        for row in session.scalars(
            select(Intervention)
            .where(Intervention.is_active.is_(True))
            .order_by(Intervention.code, Intervention.id)
        ).all()
    )


def _history_view(event: CaseEvent, names: Mapping[UUID, str]) -> CaseHistoryItem:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    detail = _payload_summary(payload)
    return CaseHistoryItem(
        event_type=event.event_type,
        event_label=_EVENT_LABELS.get(event.event_type, event.event_type.replace("_", " ").title()),
        actor=names.get(event.actor_id, "System") if event.actor_id else "System",
        occurred_at=_format_instant(event.occurred_at),
        detail=detail,
    )


def _comment_view(
    comment: CaseComment, author_id: UUID, names: Mapping[UUID, str]
) -> CaseCommentView:
    values = comment.mentions if isinstance(comment.mentions, list | tuple) else []
    mentions = tuple(str(value) for value in values if isinstance(value, str))
    return CaseCommentView(
        body=comment.body,
        author=names.get(author_id, "Case participant"),
        created_at=_format_instant(comment.created_at),
        mentions=mentions,
    )


def _action_view(
    action: ActionTaken,
    code: str | None,
    text: str | None,
    actor_id: UUID | None,
    names: Mapping[UUID, str],
) -> CaseActionView:
    if code is not None:
        label = code
        detail = text or "Catalogue intervention"
        source = "Catalogue intervention"
    else:
        label = "Free-text action"
        detail = action.free_text or "No action detail recorded."
        source = "Free text"
    return CaseActionView(
        label=label,
        detail=detail,
        source=source,
        actor=names.get(actor_id, "Case participant") if actor_id else "Case participant",
        taken_at=_format_instant(action.taken_at),
    )


def _memo_view(memo: Memo, run_date: object | None, run_state: str | None) -> CaseMemoView:
    slots = memo.slots if isinstance(memo.slots, Mapping) else {}
    headline = _string_value(slots.get("headline"), "Grounded warning memo")
    excerpt = " ".join(memo.drafted_text.split())
    if len(excerpt) > 220:
        excerpt = f"{excerpt[:217].rstrip()}…"
    superseded = run_state == "superseded"
    return CaseMemoView(
        memo_id=str(memo.id),
        run_date=_format_date(run_date),
        run_state=run_state or "not linked",
        run_state_label="Superseded run" if superseded else (run_state or "Not linked").title(),
        headline=headline,
        excerpt=excerpt,
        is_superseded=superseded,
    )


def _simulation_view(
    simulation: Simulation,
    code: str,
    text: str,
    run_date: object | None,
) -> CaseSimulationView:
    return CaseSimulationView(
        intervention_code=code,
        intervention_text=text,
        projected_cross_date=_format_date(simulation.projected_cross_date),
        probability=_format_percent(simulation.probability),
        delta_days=str(simulation.delta_days) if simulation.delta_days is not None else "—",
        created_at=(
            _format_date(run_date)
            if run_date is not None
            else _format_instant(simulation.created_at)
        ),
    )


def _document_view(document: Document) -> CaseDocumentView:
    return CaseDocumentView(
        document_id=str(document.id),
        filename=document.filename,
        doc_type=document.doc_type.replace("_", " ").title(),
        extraction_state=document.extraction_state.replace("_", " ").title(),
        scan_result=document.scan_result.title(),
        page_count=str(document.page_count) if document.page_count is not None else "—",
        uploaded_at=_format_instant(document.created_at),
        href=f"/documents/{quote(str(document.id), safe='')}/view",
    )


def _payload_summary(payload: Mapping[str, object]) -> str:
    values = []
    for key, value in payload.items():
        if key in {"case_id", "run_id", "assignee_id", "prior_case_id"}:
            continue
        rendered = str(value)
        if len(rendered) > 90:
            rendered = f"{rendered[:87].rstrip()}…"
        values.append(f"{key.replace('_', ' ')}: {rendered}")
        if len(values) == 3:
            break
    return " · ".join(values) or "No additional detail recorded."


def _string_value(value: object, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _format_instant(value: datetime) -> str:
    instant = _aware(value, "instant")
    return instant.astimezone(IST).strftime("%d %b %Y · %H:%M IST")


def _format_date(value: object | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return _format_instant(value)
    if hasattr(value, "strftime"):
        return value.strftime("%d %b %Y")
    return str(value)


def _format_percent(value: Decimal | None) -> str:
    if value is None:
        return "—"
    percent = (value * 100).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{percent}%"


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "CaseActionView",
    "CaseCommentView",
    "CaseDetailView",
    "CaseDocumentView",
    "CaseHistoryItem",
    "CaseListRow",
    "CaseListView",
    "CaseMemoView",
    "CaseSimulationView",
    "SelectOption",
    "build_case_detail_view",
    "build_case_list_view",
    "path_grants",
]
