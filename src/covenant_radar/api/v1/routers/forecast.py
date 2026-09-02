"""Scoped read-only forecast-path API (T-077, contract C-03).

The route is deliberately a thin persistence adapter.  It resolves the
requested covenant inside the caller's portfolio scope, selects the latest
complete run that has a stored path for that covenant, and reads one stored
path row plus its persisted forecast facts.  No projection, interpolation,
provider call, or write is allowed on this request path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.api.conditional import etag_for_id, is_not_modified
from covenant_radar.api.deps import requires
from covenant_radar.api.pagination import (
    DEFAULT_PAGE_SIZE,
    clamp_page_size,
    digest_filters,
    paginate,
)
from covenant_radar.api.v1.schemas.forecast import (
    ForecastDriverRead,
    ForecastPathRead,
    ForecastRead,
)
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastPath, ForecastRun
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.driver import DriverRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_READ = requires(Permission.VIEW_BORROWER)
_READ_DEP = Depends(_READ)
_LIST_READ = requires(Permission.VIEW_FORECAST)
_LIST_READ_DEP = Depends(_LIST_READ)
_DEFAULT_PREFIX = "/api/v1"


def create_forecast_router(
    session: Session,
    *,
    prefix: str = _DEFAULT_PREFIX,
    cursor_secret: bytes | str | None = None,
) -> APIRouter:
    """Build the protected C-03 forecast-path router, plus the C-21 forecast
    list/detail resource, over one session."""

    if not is_database_session(session):
        raise TypeError("create_forecast_router requires a SQLAlchemy Session.")
    router = APIRouter(prefix=prefix, tags=["forecasts"])
    forecast_ownership = ownership_path_for(Forecast)

    @router.get("/forecasts", response_model=list[ForecastRead], name="api_forecast_list")
    def list_forecasts(
        principal: Principal = _LIST_READ_DEP,
        covenant_version_id: Annotated[UUID | None, Query()] = None,
        run_id: Annotated[UUID | None, Query()] = None,
        cursor: Annotated[str | None, Query()] = None,
        page_size: Annotated[int | None, Query(ge=1)] = None,
    ) -> list[ForecastRead]:
        scope = resolve_scope(principal, session)
        size = clamp_page_size(page_size, default=DEFAULT_PAGE_SIZE)
        filters = {"covenant_version_id": covenant_version_id, "run_id": run_id}
        statement = forecast_ownership.apply(select(Forecast)).where(
            scope.predicate(forecast_ownership.path_column)
        )
        if covenant_version_id is not None:
            statement = statement.where(Forecast.covenant_version_id == covenant_version_id)
        if run_id is not None:
            statement = statement.where(Forecast.run_id == run_id)

        page = paginate(
            session,
            statement,
            primary_column=Forecast.created_at,
            id_column=Forecast.id,
            primary_of=lambda row: row.created_at,
            primary_parse=datetime.fromisoformat,
            cursor=cursor,
            filters_digest=digest_filters(filters),
            page_size=size,
            secret=cursor_secret,
        )
        return [ForecastRead.model_validate(row, from_attributes=True) for row in page.items]

    @router.get("/forecasts/{forecast_id}", response_model=ForecastRead, name="api_forecast_detail")
    def get_forecast(
        forecast_id: UUID,
        request: Request,
        response: Response,
        principal: Principal = _LIST_READ_DEP,
    ) -> ForecastRead | Response:
        scope = resolve_scope(principal, session)
        statement = forecast_ownership.apply(select(Forecast)).where(
            Forecast.id == forecast_id, scope.predicate(forecast_ownership.path_column)
        )
        row = session.execute(statement).scalars().one_or_none()
        if row is None:
            raise NotFound(f"Forecast {forecast_id} was not found within the current scope.")
        etag = etag_for_id(row.id)
        if is_not_modified(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return ForecastRead.model_validate(row, from_attributes=True)

    @router.get(
        "/forecasts/{covenant_ref}/path",
        response_model=ForecastPathRead,
        name="api_forecast_path",
    )
    def get_forecast_path(
        covenant_ref: str,
        day: Annotated[int, Query(ge=0, description="Stored path day offset.")],
        principal: Principal = _READ_DEP,
    ) -> ForecastPathRead:
        scope = resolve_scope(principal, session)
        version = _covenant_version(covenant_ref, scope, session)
        if version is None:
            raise NotFound(f"Covenant {covenant_ref!r} was not found within the current scope.")

        run = _latest_complete_run(version, scope, session)
        if run is None:
            raise NotFound(
                f"No completed forecast path is available for covenant {covenant_ref!r}."
            )

        maximum_day = _maximum_day(run, version, scope, session)
        if maximum_day is None:
            raise NotFound(
                f"No completed forecast path is available for covenant {covenant_ref!r}."
            )
        if day > maximum_day:
            raise HTTPException(
                status_code=422,
                detail=f"day must be between 0 and {maximum_day} for this stored path.",
            )

        path = _path_at(run, version, day, scope, session)
        if path is None:
            # A gap is a missing persisted fact, not permission to interpolate
            # or re-model it.  Returning 404 keeps the response generic and
            # tells the client to retain the previous value.
            raise NotFound(
                f"Stored forecast path day {day} was not found for covenant {covenant_ref!r}."
            )

        forecasts = _forecasts(run, version, scope, session)
        summary_forecast = _summary_forecast(forecasts, day)
        drivers = (
            DriverRepository(session).for_forecast(summary_forecast.id, scope=scope)
            if summary_forecast is not None
            else ()
        )

        suppressed = bool(summary_forecast and summary_forecast.below_confidence_floor)
        return ForecastPathRead(
            day=path.day_offset,
            projected_value=path.projected_value,
            headroom_pct=path.headroom_pct,
            probability=(
                None if suppressed or summary_forecast is None else summary_forecast.probability
            ),
            confidence=summary_forecast.confidence if summary_forecast is not None else None,
            below_confidence_floor=suppressed,
            crossing_date=(
                summary_forecast.projected_cross_date if summary_forecast is not None else None
            ),
            drivers=[
                ForecastDriverRead(
                    name=driver.name,
                    share=driver.share,
                    evidence_id=driver.evidence_id,
                    is_other=driver.is_other,
                )
                for driver in drivers
            ],
        )

    return router


def _covenant_version(
    reference: str,
    scope: Scope,
    session: Session,
) -> CovenantVersion | None:
    if not isinstance(reference, str) or not reference.strip():
        return None
    statement = (
        select(CovenantVersion)
        .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
        .join(Facility, Facility.id == Covenant.facility_id)
        .join(Borrower, Borrower.id == Facility.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            Covenant.reference == reference.strip(),
            scope.predicate(Portfolio.path),
        )
        .order_by(CovenantVersion.version_no.desc(), CovenantVersion.id.desc())
    )
    versions = session.execute(statement).scalars().all()
    live = [version for version in versions if version.status == "live"]
    if live:
        return live[0]
    return versions[0] if versions else None


def _latest_complete_run(
    version: CovenantVersion,
    scope: Scope,
    session: Session,
) -> ForecastRun | None:
    ownership = ownership_path_for(ForecastPath)
    visible_run_ids: Select[Any] = select(ForecastPath.run_id)
    visible_run_ids = ownership.apply(visible_run_ids).where(
        ForecastPath.covenant_version_id == version.id,
        scope.predicate(ownership.path_column),
    )
    statement = (
        select(ForecastRun)
        .where(
            ForecastRun.state == "complete",
            ForecastRun.id.in_(visible_run_ids),
        )
        .order_by(
            ForecastRun.as_of_date.desc(),
            ForecastRun.finished_at.desc().nullslast(),
            ForecastRun.id.desc(),
        )
        .limit(1)
    )
    return session.execute(statement).scalars().one_or_none()


def _maximum_day(
    run: ForecastRun,
    version: CovenantVersion,
    scope: Scope,
    session: Session,
) -> int | None:
    ownership = ownership_path_for(ForecastPath)
    statement = (
        ownership.apply(select(ForecastPath.day_offset))
        .where(
            ForecastPath.run_id == run.id,
            ForecastPath.covenant_version_id == version.id,
            scope.predicate(ownership.path_column),
        )
        .order_by(ForecastPath.day_offset.desc())
        .limit(1)
    )
    maximum_day = cast(int | None, session.execute(statement).scalar_one_or_none())
    return maximum_day if maximum_day is not None and maximum_day >= 0 else None


def _path_at(
    run: ForecastRun,
    version: CovenantVersion,
    day: int,
    scope: Scope,
    session: Session,
) -> ForecastPath | None:
    ownership = ownership_path_for(ForecastPath)
    statement = ownership.apply(select(ForecastPath)).where(
        ForecastPath.run_id == run.id,
        ForecastPath.covenant_version_id == version.id,
        ForecastPath.day_offset == day,
        scope.predicate(ownership.path_column),
    )
    return session.execute(statement).scalars().one_or_none()


def _forecasts(
    run: ForecastRun,
    version: CovenantVersion,
    scope: Scope,
    session: Session,
) -> tuple[Forecast, ...]:
    ownership = ownership_path_for(Forecast)
    statement = (
        ownership.apply(select(Forecast))
        .where(
            Forecast.run_id == run.id,
            Forecast.covenant_version_id == version.id,
            scope.predicate(ownership.path_column),
        )
        .order_by(Forecast.horizon_days, Forecast.id)
    )
    return tuple(session.execute(statement).scalars().all())


def _summary_forecast(forecasts: Sequence[Forecast], day: int) -> Forecast | None:
    """Select an existing horizon that covers the selected day.

    The selection is only over stored summaries.  It intentionally does not
    derive a probability for a day for which no forecast row was persisted.
    """

    return next(
        (forecast for forecast in forecasts if forecast.horizon_days >= day),
        forecasts[-1] if forecasts else None,
    )


__all__ = ["create_forecast_router"]
