"""Scoped, cursor-paginated read-only simulation API (`C-21`).

Reads go through `SimulationRepository` rather than a bare session query,
because its lineage annotation (`supersedes_simulation_id`,
`superseded_by_simulation_id`, `based_on_superseded_run`) resolves other
simulations and forecast runs â€” logic this router must not re-derive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from covenant_radar.api.conditional import etag_for_id, is_not_modified
from covenant_radar.api.deps import requires
from covenant_radar.api.pagination import (
    DEFAULT_PAGE_SIZE,
    clamp_page_size,
    digest_filters,
    paginate,
)
from covenant_radar.api.v1.schemas.simulations import SimulationRead
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.forecast import Simulation
from covenant_radar.db.repositories.simulation import SimulationRepository
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_READ = requires(Permission.RUN_SIMULATION)
_READ_DEP = Depends(_READ)
_DEFAULT_PREFIX = "/api/v1"


def create_simulations_router(
    session: Session,
    *,
    prefix: str = _DEFAULT_PREFIX,
    cursor_secret: bytes | str | None = None,
) -> APIRouter:
    """Build the protected, scoped, paginated simulation resource router."""

    if not is_database_session(session):
        raise TypeError("create_simulations_router requires a SQLAlchemy Session.")
    router = APIRouter(prefix=prefix, tags=["simulations"])
    repository = SimulationRepository(session)

    @router.get("/simulations", response_model=list[SimulationRead], name="api_simulation_list")
    def list_simulations(
        principal: Principal = _READ_DEP,
        forecast_id: Annotated[UUID | None, Query()] = None,
        intervention_id: Annotated[UUID | None, Query()] = None,
        cursor: Annotated[str | None, Query()] = None,
        page_size: Annotated[int | None, Query(ge=1)] = None,
    ) -> list[SimulationRead]:
        scope = resolve_scope(principal, session)
        size = clamp_page_size(page_size, default=DEFAULT_PAGE_SIZE)
        filters = {"forecast_id": forecast_id, "intervention_id": intervention_id}
        statement = repository.query(
            scope=scope, forecast_id=forecast_id, intervention_id=intervention_id
        )
        page = paginate(
            session,
            statement,
            primary_column=Simulation.created_at,
            id_column=Simulation.id,
            primary_of=lambda row: row.created_at,
            primary_parse=datetime.fromisoformat,
            cursor=cursor,
            filters_digest=digest_filters(filters),
            page_size=size,
            secret=cursor_secret,
        )
        annotated = [repository.annotate(row, scope=scope) for row in page.items]
        return [_read(row) for row in annotated]

    @router.get(
        "/simulations/{simulation_id}", response_model=SimulationRead, name="api_simulation_detail"
    )
    def get_simulation(
        simulation_id: UUID,
        request: Request,
        response: Response,
        principal: Principal = _READ_DEP,
    ) -> SimulationRead | Response:
        scope = resolve_scope(principal, session)
        row = repository.get(simulation_id, scope=scope)
        if row is None:
            raise NotFound(f"Simulation {simulation_id} was not found within the current scope.")
        etag = etag_for_id(row.id)
        if is_not_modified(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return _read(row)

    return router


def _read(row: Simulation) -> SimulationRead:
    parameters: dict[str, Any] = {
        key: value for key, value in row.parameters.items() if not key.startswith("_")
    }
    return SimulationRead(
        id=row.id,
        forecast_id=row.forecast_id,
        intervention_id=row.intervention_id,
        parameters=parameters,
        assumptions=row.assumptions,
        projected_cross_date=row.projected_cross_date,
        probability=row.probability,
        delta_days=row.delta_days,
        delta_probability=row.delta_probability,
        created_at=row.created_at,
        supersedes_simulation_id=getattr(row, "supersedes_simulation_id", None),
        superseded_by_simulation_id=getattr(row, "superseded_by_simulation_id", None),
        based_on_superseded_run=getattr(row, "based_on_superseded_run", False),
    )


__all__ = ["create_simulations_router"]
