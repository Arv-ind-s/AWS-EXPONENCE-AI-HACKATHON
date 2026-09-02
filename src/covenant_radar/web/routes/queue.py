"""Browser screen for the portfolio queue (`T-073`, contract `C-01`).

`C-01` names one route — `GET /` — serving the ranked queue every user opens
first. This module resolves the caller's scope, reads the current page
through `db/repositories/triage.py` (`T-061`) and hands it to
`web/view_models/queue.py` to shape for the template. `T-074` extends this
route with band, portfolio, industry, assignee, SMA band and case-state
filters, saved views, and URL reflection of active filters.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import urlencode
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import NotFound, ValidationError
from covenant_radar.db.repositories.saved_view import SavedViewRepository
from covenant_radar.db.repositories.triage import TriageRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.triage.views import QueueFilters
from covenant_radar.i18n.formatting import format_indian_currency
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.queue import (
    assignable_users,
    build_queue_view,
    case_state_options,
)

_LOGGER = structlog.get_logger(__name__)
_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_READ = requires(Permission.VIEW_QUEUE)
_READ_DEP = Depends(_READ)

_LABELS = {
    "title": "Portfolio queue",
    "heading": "Portfolio queue",
    "ranked_by": "Ranked by urgency",
    "column_rank": "#",
    "column_borrower": "Borrower",
    "column_exposure": "Exposure",
    "column_worst_covenant": "Worst covenant",
    "column_dated_risk": "Dated risk",
    "column_trajectory": "Trajectory",
    "column_band": "Band",
    "column_sma_band": "SMA band",
    "column_assignee": "Assignee",
    "column_case_state": "Case state",
    "column_what_changed": "What changed",
    "by": "by",
    "loading": "Loading portfolio queue",
    "error_title": "Unable to load the portfolio queue",
    "error_message": "Reload the queue. If the problem continues, contact an administrator.",
    "retry": "Reload queue",
    "degraded_capability": "Forecast display",
    "degraded_message": "Ranked rows and locally stored covenant facts remain available.",
    "snapshot_title": "Portfolio snapshot",
    "summary_all": "All",
    "summary_act": "Act now",
    "summary_amber": "Amber",
    "summary_watch": "Watch",
    "summary_changed": "Changed today",
    "summary_exposure": "Exposure in view",
    "summary_no_exposure": "Unavailable",
    "summary_as_of": "Latest completed run",
    "signal_family": "Signal family",
    # Freshness — one line, one fact.
    "freshness_checked": "checked",
    "freshness_check_now": "Check now",
    "freshness_newer": "A newer run has finished.",
    "freshness_review": "Review latest",
    # Filters.
    "filters_legend": "Narrow the queue",
    "filters_apply": "Apply filters",
    "filters_active": "Active filters",
    "filters_clear": "Clear all",
    "filters_remove": "Remove filter",
    "filters_saved_view": "Saved view",
    "filters_saved_view_none": "No saved view",
    "filters_save_view": "Save this view",
    "filters_save_view_name": "Name for this view",
    "filters_save_view_placeholder": "e.g. Act now, unassigned",
    "filters_save_view_submit": "Save",
    # Row detail and actions.
    "detail_show": "Show detail for",
    "detail_hide": "Hide detail for",
    "detail_horizons": "Escalation probability by horizon",
    "detail_covenant": "Worst covenant",
    "detail_case": "Case",
    "detail_changed": "What changed",
    "open_case": "Open case",
    "open_borrower": "Open borrower",
    "ai_explanation": "AI explanation",
    "no_case": "No case opened",
    "why": "Why this score",
    # Selection and bulk actions.
    "selection_label": "selected",
    "selection_clear": "Clear selection",
    "selection_assign": "Assign to",
    "selection_assign_submit": "Assign",
    "selection_state": "Set state",
    "selection_state_submit": "Set state",
    "selection_watchlist": "Add to watchlist",
    "selection_export": "Export CSV",
    "selection_none": "Select rows to assign, change state or export.",
    # Glossary.
    "glossary_title": "How to read this queue",
}

# One place decides how a filter value reads to a person, so the chip, the
# select and the tile can never describe the same value differently.
_BAND_LABELS: Final[Mapping[str, str]] = {
    "act": "Act now",
    "amber": "Amber",
    "watch": "Watch",
}
_CASE_STATE_LABELS: Final[Mapping[str, str]] = {
    "open": "Open",
    "in_progress": "In progress",
    "monitoring": "Monitoring",
    "escalated": "Escalated",
    "closed": "Closed",
    "none": "No case opened",
}
_SIGNAL_FAMILY_LABELS: Final[Mapping[str, str]] = {
    "account_activity": "Account activity",
    "payment": "Payment behaviour",
    "utilisation": "Facility utilisation",
    "treasury": "Treasury flows",
    "concentration": "Concentration exposure",
    "industry": "Industry conditions",
    "news": "News deterioration",
}
_FILTER_FIELDS: Final[tuple[tuple[str, str, Mapping[str, str]], ...]] = (
    ("band", "Band", _BAND_LABELS),
    ("sma_band", "SMA band", {}),
    ("case_state", "Case state", _CASE_STATE_LABELS),
    ("signal_family", "Signal family", _SIGNAL_FAMILY_LABELS),
    ("portfolio", "Portfolio", {}),
    ("industry", "Industry", {}),
    ("assignee", "Assignee", {}),
)


def create_queue_router(
    session: Session,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
    cursor_secret: bytes | str | None = None,
) -> APIRouter:
    """Build the protected portfolio queue route over one database session."""
    if not is_database_session(session):
        raise TypeError("create_queue_router requires a SQLAlchemy Session.")
    router = APIRouter(tags=["queue-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )
    triage_repo = TriageRepository(session, cursor_secret=cursor_secret)
    views_repo = SavedViewRepository(session)

    @router.get("/", response_class=HTMLResponse, name="queue")
    def queue(
        request: Request,
        cursor: str | None = None,
        band: str | None = Query(None, description="Filter by band (act, amber, watch)"),
        portfolio: str | None = Query(None, description="Filter by portfolio ID or code"),
        industry: str | None = Query(None, description="Filter by industry code"),
        assignee: str | None = Query(None, description="Filter by assignee user ID"),
        sma_band: str | None = Query(None, description="Filter by SMA band"),
        case_state: str | None = Query(None, description="Filter by case state"),
        signal_family: str | None = Query(None, description="Filter by signal family"),
        view_id: str | None = Query(None, description="Load a saved view by ID"),
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        scope = resolve_scope(principal, session)

        # Build filters from query parameters or saved view
        try:
            filters = QueueFilters(
                band=_unset(band),
                portfolio=_unset(portfolio),
                industry=_unset(industry),
                assignee=_unset(assignee),
                sma_band=_unset(sma_band),
                case_state=_unset(case_state),
                signal_family=_unset(signal_family),
            )
        except ValueError as error:
            raise ValidationError(str(error)) from error

        # If a saved view is requested, load and apply it
        if view_id:
            try:
                saved_view = views_repo.get_by_id(UUID(view_id), principal.id)
                if saved_view is None:
                    raise NotFound(f"Saved view {view_id} not found.")
                # Apply the saved view's filters within this user's scope
                scoped_view = views_repo.apply_within_scope(saved_view, scope)
                filters = scoped_view.filters
            except ValueError as error:
                raise ValidationError(f"Invalid view ID: {error}") from error

        # Query the triage repository with filters
        page = triage_repo.query(scope, filters=filters, cursor=cursor)
        view = build_queue_view(page, session, scope=scope)

        # Prepare filter state for template
        filter_state = {
            "band": filters.band,
            "portfolio": filters.portfolio,
            "industry": filters.industry,
            "assignee": filters.assignee,
            "sma_band": filters.sma_band,
            "case_state": filters.case_state,
            "signal_family": filters.signal_family,
        }
        # Keep the executive strip in lock-step with the visible work queue.
        # This is particularly important for the signal-family filter: a
        # reviewer should never see counts that describe a different slice
        # than the rows below it.
        portfolio_summary = triage_repo.summary(scope, filters=filters)
        # The band tiles are navigation, not a readout of the current slice.
        # Counting them with every filter *except* band keeps every band
        # reachable: with band=act applied, an "Amber 12" tile still says 12
        # and still leads somewhere, where a self-filtered count would read
        # 0 and strand the reader in the slice they just entered.
        band_facet = triage_repo.summary(scope, filters=_without_band(filters))
        summary = {
            "total": band_facet.total,
            "act": band_facet.act,
            "amber": band_facet.amber,
            "watch": band_facet.watch,
            "visible": portfolio_summary.total,
            "what_changed": portfolio_summary.what_changed,
            "exposure_total": (
                format_indian_currency(portfolio_summary.exposure_total)
                if portfolio_summary.exposure_total is not None
                else _LABELS["summary_no_exposure"]
            ),
            "as_of_date": view.as_of_date,
            # Each band tile draws its share of the book beneath its count.
            # A count alone does not say whether 31 amber borrowers is most
            # of the portfolio or a rounding error in it. Computed here
            # rather than in the template so the tile stays a dumb renderer,
            # and floored to whole percent because the bar cannot draw
            # finer than that anyway.
            "shares": _band_shares(band_facet),
        }

        return _render(
            request,
            fallback_environment,
            principal=principal,
            view=view,
            filters=filter_state,
            summary=summary,
            active_filters=_active_filters(filter_state),
            band_hrefs=_band_hrefs(filter_state),
            clear_href=_href({}),
            saved_views=_saved_views(views_repo, principal, scope, filter_state),
            assignable_users=assignable_users(session, scope),
            case_states=case_state_options(),
            can_update_case=principal.has(Permission.UPDATE_CASE),
            can_export=principal.has(Permission.EXPORT_EVIDENCE),
        )

    return router


def _without_band(filters: QueueFilters) -> QueueFilters:
    """Return the same filters with the band cleared."""

    values = dict(filters.to_dict())
    values["band"] = None
    return QueueFilters.from_value(values)


def _href(values: Mapping[str, object]) -> str:
    """Build a queue URL from the filter values that are actually set."""

    query = urlencode(
        {key: str(value) for key, value in sorted(values.items()) if value not in (None, "")}
    )
    return f"/?{query}" if query else "/"


def _active_filters(filter_state: Mapping[str, object]) -> tuple[dict[str, str], ...]:
    """Describe each applied filter and the URL that removes just that one.

    Every chip is a plain link, so a reader can undo one decision at a time
    without JavaScript and without re-deriving the rest of the query.
    """

    chips: list[dict[str, str]] = []
    for name, label, value_labels in _FILTER_FIELDS:
        value = filter_state.get(name)
        if value in (None, ""):
            continue
        text = str(value)
        remaining = {key: item for key, item in filter_state.items() if key != name}
        chips.append(
            {
                "name": name,
                "label": label,
                "value": text,
                "value_label": value_labels.get(text, text),
                "remove_href": _href(remaining),
            }
        )
    return tuple(chips)


def _band_shares(facet: object) -> dict[str, int]:
    """Return each band's whole-percent share of the unfiltered book.

    Floored, not rounded: the bar is three user units tall and a hundred
    wide, so a fractional percent is a precision the mark cannot carry.  An
    empty book yields zero everywhere rather than dividing by it.
    """

    total = int(getattr(facet, "total", 0) or 0)
    if total <= 0:
        return {"act": 0, "amber": 0, "watch": 0}
    return {
        band: int(getattr(facet, band, 0) or 0) * 100 // total
        for band in ("act", "amber", "watch")
    }


def _band_hrefs(filter_state: Mapping[str, object]) -> dict[str, str]:
    """Return the tile URL for each band, preserving every other filter."""

    others = {key: value for key, value in filter_state.items() if key != "band"}
    return {
        "all": _href(others),
        "act": _href({**others, "band": "act"}),
        "amber": _href({**others, "band": "amber"}),
        "watch": _href({**others, "band": "watch"}),
    }


def _saved_views(
    views_repo: SavedViewRepository,
    principal: Principal,
    scope: Scope,
    filter_state: Mapping[str, object],
) -> tuple[dict[str, str], ...]:
    """List the caller's saved views as ordinary filter URLs.

    A saved view is a filter set, so the picker navigates to the filters
    themselves rather than to an opaque `view_id`. The reader then gets a
    bookmarkable URL, the active-filter chips that describe what the view
    actually did, and band tiles that still work inside it — none of which
    an opaque id can give them. `?view_id=` remains accepted on the route
    for links saved before this change.
    """

    try:
        records = views_repo.list_for_user(principal.id)
    except (TypeError, ValueError):
        # Blanking the whole picker used to be silent, which is how an
        # undecodable stored document hid every saved view a user had without
        # leaving a trace anywhere. Say so.
        _LOGGER.warning("saved_views_unreadable", principal_id=str(principal.id), exc_info=True)
        return ()
    current = {key: _text(value) for key, value in filter_state.items()}
    views: list[dict[str, str]] = []
    for record in records:
        # One unreadable view must not cost the reader the rest of them.
        try:
            values = views_repo.apply_within_scope(record, scope).filters.to_dict()
        except (TypeError, ValueError):
            _LOGGER.warning("saved_view_skipped", view_name=record.name, exc_info=True)
            continue
        applied = {key: _text(value) for key, value in values.items()}
        views.append(
            {
                "name": record.name,
                "href": _href(values),
                "active": "true" if applied == current else "false",
            }
        )
    return tuple(views)


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _unset(value: str | None) -> str | None:
    """Read a blank filter value as "no filter".

    Every filter select on the queue offers "All" as `value=""`, so the form
    and its HTMX polls both submit `band=` rather than dropping the key.
    `QueueFilters` rejects empty text, so an unfiltered poll would otherwise
    fail the whole screen.
    """
    if value is None:
        return None
    return value.strip() or None


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template_name = "screens/queue/index.html"
    is_fragment = request.headers.get("HX-Request", "").strip().lower() == "true"
    if is_fragment:
        target = request.headers.get("HX-Target", "").strip()
        template_name = {
            # `queue-workspace` is the target every filter interaction uses:
            # counts, chips and rows are re-rendered from one read, so the
            # strip can never describe a different slice than the rows below.
            # The two narrower targets remain for the background polls, which
            # refresh one region without disturbing the other.
            "queue-workspace": "screens/queue/_workspace.html",
            "queue-summary": "screens/queue/_summary.html",
            "queue-ledger": "screens/queue/_ledger.html",
        }.get(target, template_name)
    template = environment.get_template(template_name)
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
        **context,
    }
    response = HTMLResponse(template.render(**values), status_code=status_code)
    if is_fragment:
        response.headers["Vary"] = "HX-Request, HX-Target"
    return response


__all__ = ["create_queue_router"]
