"""Application boundary for deterministic intervention simulations.

The arithmetic remains delegated to the pure domain module.  T-064 adds an
explicit persistence operation around the immutable result so callers can
choose between an offline calculation and a transaction-backed simulation
artefact without changing the calculation itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.db.models.forecast import Simulation
from covenant_radar.db.repositories.simulation import SimulationRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.forecast import Projection, Weights
from covenant_radar.domain.interventions.effects import InterventionFacts
from covenant_radar.domain.interventions.simulate import (
    SimulationComparison,
    SimulationResult,
    compare,
    simulate,
)

_REQUEST_ID_MAX_LENGTH = 40
_SIMULATION_CREATED_EVENT = "simulation_created"


class SimulationAuditWriter(Protocol):
    """The append-only audit boundary supplied by the application."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an event in the caller's transaction."""


class SimulationService:
    """Simulation facade with an optional transaction-backed persistence seam."""

    def __init__(
        self,
        session: Session | None = None,
        *,
        audit: SimulationAuditWriter | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
        repository: SimulationRepository | None = None,
    ) -> None:
        if session is not None and not is_database_session(session):
            raise TypeError("SimulationService session must be a SQLAlchemy Session or None.")
        if repository is not None and not isinstance(repository, SimulationRepository):
            raise TypeError("SimulationService repository must be a SimulationRepository.")
        if repository is not None and session is not None and repository.session is not session:
            raise ValueError("SimulationService session and repository must use the same session.")
        if audit is not None and not callable(getattr(audit, "record", None)):
            raise TypeError("SimulationService audit must provide a callable record method.")
        self.session = (
            session
            if session is not None
            else (repository.session if repository is not None else None)
        )
        self.repository = repository or (
            SimulationRepository(self.session) if self.session is not None else None
        )
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = _request_id(
            request_id if request_id is not None else get_request_id() or new_request_id()
        )

    def simulate(
        self,
        projection: Projection,
        intervention: InterventionFacts,
        parameters: Mapping[str, object] | None = None,
        *,
        covenant_class: str | None = None,
        weights: Weights | Mapping[str, object] | None = None,
        as_of_date: date | str | None = None,
    ) -> SimulationResult:
        """Run one simulation with optional keyword conveniences.

        Keyword conveniences are merged into the explicit parameter mapping;
        conflicting values are rejected by the domain boundary rather than
        allowing one caller spelling to silently win.
        """

        effective = _parameters(parameters)
        _put_if_supplied(effective, "covenant_class", covenant_class)
        _put_if_supplied(effective, "weights", weights)
        _put_if_supplied(effective, "as_of_date", as_of_date)
        return simulate(projection, intervention, effective)

    run = simulate

    def persist(
        self,
        result: SimulationResult,
        *,
        forecast_id: UUID,
        intervention_id: UUID,
        scope: Scope,
        created_by_id: UUID | None = None,
        actor_id: UUID | None = None,
        request_id: str | None = None,
        occurred_at: datetime | None = None,
        simulation_id: UUID | None = None,
    ) -> Simulation:
        """Persist one simulation and audit a newly created artefact.

        The repository flushes but never commits.  The audit row is written
        after that flush and therefore shares the caller's transaction.
        """

        if self.repository is None:
            raise RuntimeError("Simulation persistence requires a SQLAlchemy session.")
        if self.audit is None:
            raise RuntimeError("Simulation persistence requires an append-only audit writer.")
        effective_request_id = _request_id(self.request_id if request_id is None else request_id)
        effective_time = self.clock.now() if occurred_at is None else occurred_at
        write = self.repository.save_with_status(
            result,
            forecast_id=forecast_id,
            intervention_id=intervention_id,
            scope=scope,
            occurred_at=effective_time,
            request_id=effective_request_id,
            created_by_id=created_by_id,
            actor_id=actor_id,
            simulation_id=simulation_id,
        )
        if write.created:
            assumptions = write.simulation.assumptions
            result_payload = assumptions.get("result") if isinstance(assumptions, Mapping) else None
            content_hash = (
                result_payload.get("content_hash") if isinstance(result_payload, Mapping) else None
            )
            self.audit.record(
                _SIMULATION_CREATED_EVENT,
                ("simulation", write.simulation.id),
                {
                    "forecast_id": str(forecast_id),
                    "intervention_id": str(intervention_id),
                    "content_hash": content_hash,
                    "supersedes_simulation_id": (
                        str(write.supersedes_simulation_id)
                        if write.supersedes_simulation_id is not None
                        else None
                    ),
                },
                actor=created_by_id if created_by_id is not None else actor_id,
                request_id=effective_request_id,
            )
        return write.simulation

    persist_simulation = persist

    def simulate_and_persist(
        self,
        projection: Projection,
        intervention: InterventionFacts,
        parameters: Mapping[str, object] | None = None,
        *,
        forecast_id: UUID,
        intervention_id: UUID,
        scope: Scope,
        covenant_class: str | None = None,
        weights: Weights | Mapping[str, object] | None = None,
        as_of_date: date | str | None = None,
        created_by_id: UUID | None = None,
        actor_id: UUID | None = None,
        request_id: str | None = None,
        occurred_at: datetime | None = None,
        simulation_id: UUID | None = None,
    ) -> Simulation:
        """Calculate and persist one result in the caller's transaction."""

        result = self.simulate(
            projection,
            intervention,
            parameters,
            covenant_class=covenant_class,
            weights=weights,
            as_of_date=as_of_date,
        )
        return self.persist(
            result,
            forecast_id=forecast_id,
            intervention_id=intervention_id,
            scope=scope,
            created_by_id=created_by_id,
            actor_id=actor_id,
            request_id=request_id,
            occurred_at=occurred_at,
            simulation_id=simulation_id,
        )

    run_and_persist = simulate_and_persist

    def compare(
        self,
        projection: Projection,
        interventions: Iterable[InterventionFacts],
        parameters: Mapping[str, object] | None = None,
        *,
        covenant_class: str | None = None,
        weights: Weights | Mapping[str, object] | None = None,
        as_of_date: date | str | None = None,
    ) -> SimulationComparison:
        """Compare the supplied interventions against one shared baseline."""

        effective = _parameters(parameters)
        _put_if_supplied(effective, "covenant_class", covenant_class)
        _put_if_supplied(effective, "weights", weights)
        _put_if_supplied(effective, "as_of_date", as_of_date)
        return compare(projection, interventions, effective)

    compare_interventions = compare


def _parameters(parameters: Mapping[str, object] | None) -> dict[str, object]:
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise TypeError("simulation parameters must be a mapping.")
    return dict(parameters)


def _put_if_supplied(target: dict[str, object], name: str, value: object) -> None:
    if value is None:
        return
    if name in target and target[name] != value:
        raise ValueError(f"{name} was supplied more than once with different values.")
    target[name] = value


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(
            f"Simulation request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
        )
    if not value.strip():
        raise ValueError("Simulation request_id must not be blank.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Simulation request_id contains a control character.")
    return value.strip()


__all__ = ["SimulationService"]
