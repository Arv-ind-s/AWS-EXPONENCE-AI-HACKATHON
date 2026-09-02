"""View model for the portfolio queue screen (`T-073`).

`db/repositories/triage.py` (`T-061`) deliberately returns only what its
single scoped, run-consistent query can select in one statement: it does
not join out to the covenant's name and threshold, or to the forecast that
names a crossing date, because those joins are keyed by fields the queue
row already carries and do not affect scope, filtering or pagination. This
module performs those small, read-only, already-scoped lookups — batched by
the ids on the page in hand, exactly as `services/triage.py`'s
``_drivers_for_rows`` batches driver shares for the same reason — and
shapes the result into template-ready rows.

Two designed empty states share the same "no rows" shape but are not the
same case: `QueuePage.no_complete_run()` (`run_id is None`) is the
documented first-use state naming the import step, while a run that exists
but returns no rows for this caller's scope or filters (`run_id` set,
``entries`` empty) is *this* module's "empty scope" state, because the
repository layer has no message to attach to it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal
from uuid import UUID

from markupsafe import Markup
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastDriver, ForecastPath
from covenant_radar.db.models.identity import AppUser, UserPortfolioScope
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.cases.lifecycle import CaseState
from covenant_radar.domain.triage.views import QUEUE_EMPTY_MESSAGE, QueueEntry, QueuePage
from covenant_radar.web.svg.trajectory import (
    TrajectoryPoint,
    render_trajectory_sparkline_svg,
)
from covenant_radar.web.view_models.case import SelectOption, path_grants

NO_FORECAST_TEXT: Final[str] = "No covenant has been tested for this borrower yet."
SUPPRESSED_TEXT: Final[str] = "Confidence fell below the floor required to show a probability."
UNASSIGNED_TEXT: Final[str] = "Unassigned"
NO_CASE_TEXT: Final[str] = "No case opened"
NO_CHANGE_TEXT: Final[str] = "Not yet compared with a prior run."
NO_COMPLETE_RUN_TITLE: Final[str] = "No completed run yet"
EMPTY_SCOPE_TITLE: Final[str] = "No borrowers rank in this view"
EMPTY_SCOPE_MESSAGE: Final[str] = (
    "Nothing in your scope matched the latest completed run. Ask an administrator "
    "to widen your portfolio access, or clear any active filters and reload the queue."
)

_DIRECTION_SYMBOLS: Final[Mapping[str, str]] = {"min": "≥", "max": "≤"}
_CASE_STATE_LABELS: Final[Mapping[str, str]] = {
    "open": "Open",
    "in_progress": "In progress",
    "monitoring": "Monitoring",
    "escalated": "Escalated",
    "closed": "Closed",
}
_PERCENT_QUANTUM: Final[Decimal] = Decimal("1")
QueueState = Literal["ready", "empty", "loading", "error", "degraded"]


@dataclass(frozen=True, slots=True)
class QueueRowView:
    """One rendered queue row, including its persisted dominant driver."""

    row_id: str
    href: str
    borrower_name: str
    borrower_reference: str
    exposure: Decimal | None
    worst_covenant: str
    probability_display: str
    crossing_date: date | None
    band: str
    sma_band: str
    assignee: str
    case_state: str
    what_changed: str
    horizon_displays: tuple[str, ...] = ()
    dominant_driver: str = ""
    confidence_display: str = ""
    urgency_display: str = ""
    why_href: str = ""
    crossing_note: str = ""
    trajectory_svg: Markup | None = None
    trajectory_label: str = "No stored trajectory available."
    rank: int = 0
    detail_id: str = ""
    case_reference: str = ""
    case_href: str = ""


@dataclass(frozen=True, slots=True)
class QueueScreenView:
    """Everything the queue template needs, already shaped and ordered."""

    as_of_date: date | None
    rows: tuple[QueueRowView, ...]
    next_cursor: str | None
    empty: bool
    empty_title: str
    empty_message: str
    state: QueueState = "ready"
    run_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.state not in {"ready", "empty", "loading", "error", "degraded"}:
            raise ValueError(f"Unsupported queue screen state: {self.state!r}.")


@dataclass(frozen=True, slots=True)
class _QueueTrajectory:
    points: tuple[TrajectoryPoint, ...]
    threshold: Decimal


def build_queue_view(
    page: QueuePage,
    session: Session,
    *,
    scope: Scope | None = None,
) -> QueueScreenView:
    """Shape one scoped `QueuePage` into the queue screen's view model."""
    if not isinstance(page, QueuePage):
        raise TypeError("build_queue_view requires a QueuePage.")
    if not is_database_session(session):
        raise TypeError("build_queue_view requires a SQLAlchemy Session.")
    if scope is not None and not isinstance(scope, Scope):
        raise TypeError("build_queue_view scope must be a portfolio Scope or None.")

    if not page.entries:
        if page.run_id is None:
            title, message = NO_COMPLETE_RUN_TITLE, page.message or QUEUE_EMPTY_MESSAGE
        else:
            title, message = EMPTY_SCOPE_TITLE, EMPTY_SCOPE_MESSAGE
        return QueueScreenView(
            as_of_date=page.as_of_date,
            rows=(),
            next_cursor=None,
            empty=True,
            empty_title=title,
            empty_message=message,
            state="empty",
            run_id=page.run_id,
        )

    covenant_labels = _covenant_labels(session, page.entries)
    crossing_dates = _crossing_dates(session, page)
    assignee_names = _assignee_names(session, page.entries)
    trajectories = _queue_trajectories(session, page, scope=scope)
    horizons = _queue_horizon_displays(session, page)
    drivers = _queue_dominant_drivers(session, page)
    forecast_ids = _queue_forecast_ids(session, page)
    rows = tuple(
        _row_view(
            entry,
            covenant_labels,
            crossing_dates,
            assignee_names,
            trajectories,
            horizons,
            drivers,
            forecast_ids,
        )
        for entry in page.entries
    )
    return QueueScreenView(
        as_of_date=page.as_of_date,
        rows=rows,
        next_cursor=page.next_cursor,
        empty=False,
        empty_title="",
        empty_message="",
        state="ready",
        run_id=page.run_id,
    )


def _row_view(
    entry: QueueEntry,
    covenant_labels: Mapping[UUID, str],
    crossing_dates: Mapping[tuple[UUID, int], date],
    assignee_names: Mapping[UUID, str],
    trajectories: Mapping[UUID, _QueueTrajectory] | None = None,
    horizons: Mapping[UUID, tuple[str, ...]] | None = None,
    drivers: Mapping[tuple[UUID, int], str] | None = None,
    forecast_ids: Mapping[tuple[UUID, int], UUID] | None = None,
) -> QueueRowView:
    worst_covenant, probability_display, crossing_date, crossing_note = _risk_cells(
        entry, covenant_labels, crossing_dates
    )
    trajectory = (
        trajectories.get(entry.worst_covenant_version_id)
        if trajectories is not None and entry.worst_covenant_version_id is not None
        else None
    )
    trajectory_label = f"{entry.legal_name} stored risk trajectory"
    trajectory_svg = (
        render_trajectory_sparkline_svg(
            f"queue-trajectory-{entry.borrower_id}",
            trajectory.points,
            trajectory.threshold,
            label=trajectory_label,
        )
        if trajectory is not None
        else None
    )
    case_reference = (entry.case_reference or "").strip()
    return QueueRowView(
        row_id=f"queue-row-{entry.borrower_id}",
        detail_id=f"queue-detail-{entry.borrower_id}",
        rank=entry.rank,
        case_reference=case_reference,
        case_href=f"/cases/{case_reference}" if case_reference else "",
        href=f"/borrowers/{entry.borrower_reference}",
        borrower_name=entry.legal_name,
        borrower_reference=entry.borrower_reference,
        exposure=entry.exposure,
        worst_covenant=worst_covenant,
        probability_display=probability_display,
        crossing_date=crossing_date,
        band=entry.band,
        sma_band=entry.sma_band or "—",
        assignee=(
            assignee_names.get(entry.assignee_id, UNASSIGNED_TEXT)
            if entry.assignee_id is not None
            else UNASSIGNED_TEXT
        ),
        case_state=(
            _CASE_STATE_LABELS.get(entry.case_state, entry.case_state)
            if entry.case_state
            else NO_CASE_TEXT
        ),
        what_changed=entry.what_changed or NO_CHANGE_TEXT,
        horizon_displays=(horizons or {}).get(entry.worst_covenant_version_id, ()),
        dominant_driver=(
            (drivers or {}).get((entry.worst_covenant_version_id, entry.worst_horizon), "")
            if entry.worst_covenant_version_id is not None and entry.worst_horizon is not None
            else ""
        ),
        confidence_display=_fraction_display(entry.confidence, label="Confidence"),
        urgency_display=_urgency_display(entry.urgency),
        why_href=(
            f"/why/forecast/{forecast_ids[(entry.worst_covenant_version_id, entry.worst_horizon)]}"
            if forecast_ids is not None
            and entry.worst_covenant_version_id is not None
            and entry.worst_horizon is not None
            and (entry.worst_covenant_version_id, entry.worst_horizon) in forecast_ids
            else f"/why/borrower/{entry.borrower_id}"
        ),
        crossing_note=crossing_note,
        trajectory_svg=trajectory_svg,
        trajectory_label=(
            trajectory_label if trajectory is not None else "No stored trajectory is available."
        ),
    )


def _fraction_display(value: Decimal | None, *, label: str) -> str:
    """Format a persisted fraction without inventing a fallback value."""

    if value is None:
        return f"{label} unavailable"
    percentage = (value * 100).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{label} {percentage}%"


def _urgency_display(value: Decimal | None) -> str:
    """Format the persisted urgency score without inventing a fallback value.

    Urgency is probability x exposure x confidence (`domain/triage/urgency.py`),
    an unbounded exposure-scaled score rather than a 0-1 fraction, so it must
    not be run through the percentage formatter used for confidence.
    """

    if value is None:
        return "Urgency unavailable"
    rounded = value.quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    return f"Urgency {rounded}"


def _queue_horizon_displays(
    session: Session,
    page: QueuePage,
) -> dict[UUID, tuple[str, ...]]:
    """Read all persisted 30/60/90 values for the page's worst covenant."""

    if page.run_id is None:
        return {}
    version_ids = tuple(
        sorted(
            {
                entry.worst_covenant_version_id
                for entry in page.entries
                if entry.worst_covenant_version_id is not None
            },
            key=str,
        )
    )
    if not version_ids:
        return {}
    statement = (
        select(
            Forecast.covenant_version_id,
            Forecast.horizon_days,
            Forecast.probability,
            Forecast.below_confidence_floor,
        )
        .where(
            Forecast.run_id == page.run_id,
            Forecast.covenant_version_id.in_(version_ids),
            Forecast.horizon_days.in_((30, 60, 90)),
        )
        .order_by(Forecast.covenant_version_id, Forecast.horizon_days)
    )
    grouped: dict[UUID, list[str]] = {}
    for version_id, horizon, probability, suppressed in session.execute(statement).all():
        if suppressed or probability is None:
            display = f"{horizon}d —"
        else:
            percentage = (probability * 100).quantize(
                _PERCENT_QUANTUM, rounding=ROUND_HALF_UP
            )
            display = f"{horizon}d {percentage}%"
        grouped.setdefault(version_id, []).append(display)
    return {version_id: tuple(values) for version_id, values in grouped.items()}


def _queue_dominant_drivers(
    session: Session,
    page: QueuePage,
) -> dict[tuple[UUID, int], str]:
    """Read the named, highest-share driver for each visible worst outcome."""

    if page.run_id is None:
        return {}
    keys = {
        (entry.worst_covenant_version_id, entry.worst_horizon)
        for entry in page.entries
        if entry.worst_covenant_version_id is not None and entry.worst_horizon is not None
    }
    if not keys:
        return {}
    version_ids = tuple(sorted({version_id for version_id, _ in keys}, key=str))
    horizons = tuple(sorted({horizon for _, horizon in keys}))
    statement = (
        select(
            Forecast.covenant_version_id,
            Forecast.horizon_days,
            ForecastDriver.name,
            ForecastDriver.is_other,
        )
        .join(ForecastDriver, ForecastDriver.forecast_id == Forecast.id)
        .where(
            Forecast.run_id == page.run_id,
            Forecast.covenant_version_id.in_(version_ids),
            Forecast.horizon_days.in_(horizons),
        )
        .order_by(
            Forecast.covenant_version_id,
            Forecast.horizon_days,
            ForecastDriver.share.desc(),
            ForecastDriver.name,
        )
    )
    result: dict[tuple[UUID, int], str] = {}
    for version_id, horizon, name, is_other in session.execute(statement).all():
        key = (version_id, horizon)
        if (
            key not in keys
            or key in result
            or is_other
            or name.strip().lower() in {"other", "neutral"}
        ):
            continue
        result[key] = name.strip()
    return result


def _queue_forecast_ids(
    session: Session,
    page: QueuePage,
) -> dict[tuple[UUID, int], UUID]:
    """Return forecast identities so row-level Why? opens stage-4 evidence."""

    if page.run_id is None:
        return {}
    keys = {
        (entry.worst_covenant_version_id, entry.worst_horizon)
        for entry in page.entries
        if entry.worst_covenant_version_id is not None and entry.worst_horizon is not None
    }
    if not keys:
        return {}
    version_ids = tuple(sorted({version_id for version_id, _ in keys}, key=str))
    horizons = tuple(sorted({horizon for _, horizon in keys}))
    statement = select(
        Forecast.id,
        Forecast.covenant_version_id,
        Forecast.horizon_days,
    ).where(
        Forecast.run_id == page.run_id,
        Forecast.covenant_version_id.in_(version_ids),
        Forecast.horizon_days.in_(horizons),
    )
    return {
        (version_id, horizon): forecast_id
        for forecast_id, version_id, horizon in session.execute(statement).all()
        if (version_id, horizon) in keys
    }


def _queue_trajectories(
    session: Session,
    page: QueuePage,
    *,
    scope: Scope | None,
) -> dict[UUID, _QueueTrajectory]:
    """Load valid stored paths for the page's already-scoped covenant ids.

    The route passes the same request scope used for the queue query. A
    missing scope fails closed, and the explicit ownership joins keep this
    helper safe when called directly by another read-model consumer.
    """

    if page.run_id is None or scope is None:
        return {}
    version_ids = tuple(
        sorted(
            {
                entry.worst_covenant_version_id
                for entry in page.entries
                if entry.worst_covenant_version_id is not None
            },
            key=str,
        )
    )
    if not version_ids:
        return {}
    statement = (
        select(ForecastPath, CovenantVersion.threshold)
        .join(CovenantVersion, CovenantVersion.id == ForecastPath.covenant_version_id)
        .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
        .join(Facility, Facility.id == Covenant.facility_id)
        .join(Borrower, Borrower.id == Facility.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            ForecastPath.run_id == page.run_id,
            ForecastPath.covenant_version_id.in_(version_ids),
        )
        .order_by(ForecastPath.covenant_version_id, ForecastPath.day_offset)
    )
    if scope is not None:
        statement = statement.where(scope.predicate(Portfolio.path))

    grouped: dict[UUID, list[tuple[ForecastPath, Decimal | None]]] = {}
    for path, threshold in session.execute(statement).tuples().all():
        grouped.setdefault(path.covenant_version_id, []).append((path, threshold))

    result: dict[UUID, _QueueTrajectory] = {}
    for version_id, rows in grouped.items():
        threshold_value: Decimal | None = rows[0][1]
        points = _trajectory_points(tuple(path for path, _ in rows))
        if threshold_value is None or not points or not threshold_value.is_finite():
            continue
        result[version_id] = _QueueTrajectory(points=points, threshold=threshold_value)
    return result


def _trajectory_points(rows: Sequence[ForecastPath]) -> tuple[TrajectoryPoint, ...]:
    if not rows or any(row.projected_value is None for row in rows):
        return ()
    try:
        points = tuple(
            TrajectoryPoint(day=row.day_offset, value=row.projected_value)
            for row in rows
            if row.projected_value is not None
        )
    except (TypeError, ValueError):
        return ()
    if not points or points[0].day != 0:
        return ()
    if any(
        current.day <= previous.day for previous, current in zip(points, points[1:], strict=False)
    ):
        return ()
    return points


def _risk_cells(
    entry: QueueEntry,
    covenant_labels: Mapping[UUID, str],
    crossing_dates: Mapping[tuple[UUID, int], date],
) -> tuple[str, str, date | None, str]:
    if entry.worst_covenant_version_id is None:
        return NO_FORECAST_TEXT, NO_FORECAST_TEXT, None, ""
    worst_covenant = covenant_labels.get(entry.worst_covenant_version_id, "—")
    if entry.probability is None:
        return worst_covenant, SUPPRESSED_TEXT, None, ""
    percent = (entry.probability * 100).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    probability_display = f"{percent}%"
    crossing_date = (
        crossing_dates.get((entry.worst_covenant_version_id, entry.worst_horizon))
        if entry.worst_horizon is not None
        else None
    )
    # A probability can land in Amber (or Act) from the blended
    # distance/velocity/pressure signal even when the fitted trend never
    # actually crosses the threshold inside its own horizon window
    # (`domain/forecast/crossing.py`) — that is a real, explainable outcome,
    # not a missing fact, so the row says so instead of leaving a silent gap
    # next to the probability (mirrors `_crossing_display` on the case file).
    crossing_note = (
        ""
        if crossing_date is not None or entry.worst_horizon is None
        else f"no crossing projected within {entry.worst_horizon}d"
    )
    return worst_covenant, probability_display, crossing_date, crossing_note


def _covenant_labels(session: Session, entries: Sequence[QueueEntry]) -> dict[UUID, str]:
    version_ids = {
        entry.worst_covenant_version_id
        for entry in entries
        if entry.worst_covenant_version_id is not None
    }
    if not version_ids:
        return {}
    statement = (
        select(
            CovenantVersion.id,
            Covenant.name,
            CovenantVersion.threshold,
            CovenantVersion.direction,
            CovenantVersion.unit,
        )
        .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
        .where(CovenantVersion.id.in_(version_ids))
    )
    labels: dict[UUID, str] = {}
    for version_id, name, threshold, direction, unit in session.execute(statement).all():
        symbol = _DIRECTION_SYMBOLS.get(direction, direction)
        labels[version_id] = f"{name} {symbol} {_number_with_unit(threshold, unit)}"
    return labels


def _number_with_unit(value: Decimal, unit: str) -> str:
    """Strip a stored ratio's excess decimal scale for display (e.g. 3.00000000x -> 3x)."""

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{rendered}{unit}"


def _crossing_dates(session: Session, page: QueuePage) -> dict[tuple[UUID, int], date]:
    if page.run_id is None:
        return {}
    keys = {
        (entry.worst_covenant_version_id, entry.worst_horizon)
        for entry in page.entries
        if entry.worst_covenant_version_id is not None and entry.worst_horizon is not None
    }
    if not keys:
        return {}
    statement = select(
        Forecast.covenant_version_id,
        Forecast.horizon_days,
        Forecast.projected_cross_date,
    ).where(
        Forecast.run_id == page.run_id,
        tuple_(Forecast.covenant_version_id, Forecast.horizon_days).in_(keys),
    )
    return {
        (covenant_version_id, horizon_days): crossing
        for covenant_version_id, horizon_days, crossing in session.execute(statement).all()
        if crossing is not None
    }


def assignable_users(session: Session, scope: Scope | None) -> tuple[SelectOption, ...]:
    """Return the active users who may be assigned a case in this scope.

    The queue spans many portfolios, so the per-case rule in
    `web/view_models/case.py` cannot be applied to one path. A user is
    offered when their own grant reaches any portfolio the caller can see,
    which is the same `path_grants` rule evaluated against the caller's
    scope rather than against a single case's portfolio. A missing scope
    fails closed and offers nobody.
    """

    if not is_database_session(session):
        raise TypeError("assignable_users requires a SQLAlchemy Session.")
    if scope is None:
        return ()
    if not isinstance(scope, Scope):
        raise TypeError("assignable_users scope must be a portfolio Scope or None.")
    if not scope.paths:
        return ()

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
        if user_id in seen:
            continue
        if not _grant_reaches_scope(granted_path, bool(include_descendants), scope):
            continue
        result.append(SelectOption(str(user_id), f"{full_name} · @{username}"))
        seen.add(user_id)
    return tuple(result)


def _grant_reaches_scope(granted_path: str, include_descendants: bool, scope: Scope) -> bool:
    """Whether one user's grant overlaps anything the caller can see."""

    grant = granted_path.rstrip("/") + "/"
    for path in scope.exact_paths:
        if path_grants(grant, path, include_descendants):
            return True
    for path in scope.descendant_paths:
        # The caller sees this portfolio and its whole subtree, so a user
        # granted anywhere inside that subtree is a legitimate assignee,
        # as is one whose own subtree contains it.
        if path_grants(grant, path, include_descendants) or grant.startswith(path):
            return True
    return False


def case_state_options() -> tuple[SelectOption, ...]:
    """Every case state a bulk request may target.

    A selection can hold cases in different states, and only the service
    knows which transitions each one permits, so the control offers the
    whole vocabulary and `BulkService` reports per-item outcomes rather
    than the screen guessing and hiding a legal option.
    """

    return tuple(
        SelectOption(state.value, _CASE_STATE_LABELS.get(state.value, state.value.title()))
        for state in CaseState
    )


def _assignee_names(session: Session, entries: Sequence[QueueEntry]) -> dict[UUID, str]:
    assignee_ids = {entry.assignee_id for entry in entries if entry.assignee_id is not None}
    if not assignee_ids:
        return {}
    statement = select(AppUser.id, AppUser.full_name).where(AppUser.id.in_(assignee_ids))
    return dict(session.execute(statement).tuples().all())


__all__ = [
    "EMPTY_SCOPE_MESSAGE",
    "EMPTY_SCOPE_TITLE",
    "NO_CASE_TEXT",
    "NO_CHANGE_TEXT",
    "NO_COMPLETE_RUN_TITLE",
    "NO_FORECAST_TEXT",
    "SUPPRESSED_TEXT",
    "UNASSIGNED_TEXT",
    "QueueRowView",
    "QueueState",
    "QueueScreenView",
    "assignable_users",
    "build_queue_view",
    "case_state_options",
]
