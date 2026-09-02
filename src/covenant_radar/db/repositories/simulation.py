"""Persistence adapter for immutable intervention simulations.

Simulation rows are historical evidence for a decision memo.  A simulation
is therefore write-once: repeating the same simulation against the same
forecast returns the existing row, while running the same parameters against
a newer forecast creates a new row linked to its predecessor.  The database
model predates this repository and has no separate lineage columns; the
linkage is kept in a reserved, non-user parameter field so the existing
schema remains compatible with both supported database engines.

The memo model stores simulation references in a JSON payload rather than a
foreign-key table.  ``retention_reference`` is the single repository helper
for checking those references.  It is scope-enforcing for request callers and
requires the explicitly audited retention-job escape hatch for a purge job.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from math import isfinite
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from covenant_radar.core.errors import Conflict
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastRun,
    Intervention,
    Simulation,
)
from covenant_radar.db.models.workflow import Memo
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import (
    RETENTION_JOB_CALLER,
    Scope,
    UnscopedCaller,
    ownership_path_for,
)
from covenant_radar.domain.interventions.simulate import SimulationResult

_REQUEST_ID_MAX_LENGTH = 40
_SUPERSEDES_PARAMETER = "_supersedes_simulation_id"
_COMPLETE_RUN_STATE = "complete"
#: Read from the column itself so the idempotency comparison follows the
#: stored scale rather than a second copy of it.
_FRACTION_QUANTUM = cast(Any, Simulation.__table__.c.probability.type).quantum


@dataclass(frozen=True, slots=True)
class SimulationLineage:
    """The immutable and derived lineage facts for one simulation."""

    simulation_id: UUID
    supersedes_simulation_id: UUID | None
    superseded_by_simulation_id: UUID | None
    based_on_superseded_run: bool

    def __post_init__(self) -> None:
        _uuid(self.simulation_id, "simulation_id")
        _uuid_or_none(self.supersedes_simulation_id, "supersedes_simulation_id")
        _uuid_or_none(self.superseded_by_simulation_id, "superseded_by_simulation_id")
        if not isinstance(self.based_on_superseded_run, bool):
            raise TypeError("based_on_superseded_run must be a boolean.")


@dataclass(frozen=True, slots=True)
class SimulationRetentionReference:
    """The memo references that protect a simulation from retention purge."""

    simulation_id: UUID
    memo_ids: tuple[UUID, ...]
    exists: bool = True

    def __post_init__(self) -> None:
        _uuid(self.simulation_id, "simulation_id")
        if not isinstance(self.memo_ids, tuple):
            raise TypeError("memo_ids must be a tuple.")
        if any(not isinstance(value, UUID) for value in self.memo_ids):
            raise TypeError("memo_ids must contain UUID values.")
        if len(set(self.memo_ids)) != len(self.memo_ids):
            raise ValueError("memo_ids must not contain duplicates.")
        if not isinstance(self.exists, bool):
            raise TypeError("exists must be a boolean.")

    @property
    def is_referenced(self) -> bool:
        """Whether at least one retained memo cites this simulation."""

        return bool(self.memo_ids)

    @property
    def purgeable(self) -> bool:
        """Whether the retention job may purge the simulation."""

        return self.exists and not self.is_referenced

    @property
    def reason(self) -> str | None:
        """The stable reason a referenced simulation must be retained."""

        if not self.exists:
            return "simulation is absent or outside the supplied scope"
        if not self.is_referenced:
            return None
        return "simulation is referenced by a retained memo"


@dataclass(frozen=True, slots=True)
class SimulationWrite:
    """Result of an idempotent simulation write."""

    simulation: Simulation
    created: bool
    supersedes_simulation_id: UUID | None

    def __post_init__(self) -> None:
        if not isinstance(self.simulation, Simulation):
            raise TypeError("simulation must be a Simulation row.")
        if not isinstance(self.created, bool):
            raise TypeError("created must be a boolean.")
        _uuid_or_none(self.supersedes_simulation_id, "supersedes_simulation_id")


class SimulationRepository(RepositoryBase[Simulation]):
    """Scope-aware repository for persisted counterfactual results."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(
            session,
            Simulation,
            ownership=ownership_path_for(Simulation),
            audit=audit,
        )
        self._forecasts = RepositoryBase(
            session,
            Forecast,
            ownership=ownership_path_for(Forecast),
            audit=audit,
        )
        self._memos = RepositoryBase(
            session,
            Memo,
            ownership=ownership_path_for(Memo),
            audit=audit,
        )

    def get(self, entity_id: UUID, *, scope: Scope) -> Simulation | None:
        """Return one scoped simulation and attach current lineage facts."""

        row = super().get(entity_id, scope=scope)
        return self._annotate(row, scope=scope)

    def find(self, *, scope: Scope, **criteria: object) -> Simulation | None:
        """Return one scoped simulation and attach current lineage facts."""

        row = super().find(scope=scope, **criteria)
        return self._annotate(row, scope=scope)

    def list(self, *, scope: Scope) -> Sequence[Simulation]:
        """Return all scoped simulations with deterministic lineage facts."""

        rows = super().list(scope=scope)
        return tuple(self._annotate_required(row, scope=scope) for row in rows)

    def for_forecast(
        self,
        forecast_id: UUID,
        *,
        scope: Scope,
        include_superseded: bool = True,
    ) -> Sequence[Simulation]:
        """Return simulations for one forecast, including historical rows."""

        _uuid(forecast_id, "forecast_id")
        if not isinstance(include_superseded, bool):
            raise TypeError("include_superseded must be a boolean.")
        statement: Select[tuple[Simulation]] = cast(
            Select[tuple[Simulation]], self._scoped_select(scope)
        )
        statement = statement.where(Simulation.forecast_id == forecast_id).order_by(
            Simulation.created_at,
            Simulation.id,
        )
        rows = tuple(self.session.execute(statement).scalars().all())
        annotated = tuple(self._annotate_required(row, scope=scope) for row in rows)
        if include_superseded:
            return annotated
        return tuple(row for row in annotated if not _attached_lineage(row).based_on_superseded_run)

    list_for_forecast = for_forecast

    def query(
        self,
        *,
        scope: Scope,
        forecast_id: UUID | None = None,
        intervention_id: UUID | None = None,
    ) -> Select[tuple[Simulation]]:
        """Return the scoped, optionally filtered statement, unpaginated.

        The caller composes its own ordering, seek predicate and limit
        (`api/pagination.py`'s ``paginate``) and then calls :meth:`annotate`
        on the rows it keeps; this method exists for callers, such as the
        `C-21` API resource, that page and filter across every forecast
        rather than reading one forecast's full simulation history.
        """
        if forecast_id is not None:
            _uuid(forecast_id, "forecast_id")
        if intervention_id is not None:
            _uuid(intervention_id, "intervention_id")
        statement: Select[tuple[Simulation]] = cast(
            Select[tuple[Simulation]], self._scoped_select(scope)
        )
        if forecast_id is not None:
            statement = statement.where(Simulation.forecast_id == forecast_id)
        if intervention_id is not None:
            statement = statement.where(Simulation.intervention_id == intervention_id)
        return statement

    def annotate(self, row: Simulation, *, scope: Scope) -> Simulation:
        """Attach current lineage facts to a row read outside this repository."""
        return self._annotate_required(row, scope=scope)

    def save_result(
        self,
        result: SimulationResult,
        *,
        forecast_id: UUID,
        intervention_id: UUID,
        scope: Scope,
        occurred_at: datetime,
        request_id: str,
        created_by_id: UUID | None = None,
        actor_id: UUID | None = None,
        simulation_id: UUID | None = None,
    ) -> Simulation:
        """Persist a validated domain result and return its ORM row.

        This convenience method intentionally returns the row, matching the
        other persistence adapters.  Call :meth:`save_with_status` when the
        caller also needs to know whether an insert happened.
        """

        return self.save_with_status(
            result,
            forecast_id=forecast_id,
            intervention_id=intervention_id,
            scope=scope,
            occurred_at=occurred_at,
            request_id=request_id,
            created_by_id=created_by_id,
            actor_id=actor_id,
            simulation_id=simulation_id,
        ).simulation

    persist_result = save_result
    save = save_result
    persist = save_result

    def save_with_status(
        self,
        result: SimulationResult,
        *,
        forecast_id: UUID,
        intervention_id: UUID,
        scope: Scope,
        occurred_at: datetime,
        request_id: str,
        created_by_id: UUID | None = None,
        actor_id: UUID | None = None,
        simulation_id: UUID | None = None,
    ) -> SimulationWrite:
        """Insert one result or verify the existing immutable fact.

        A same-forecast retry is idempotent.  A matching result on a newer
        forecast receives a new UUID and records the predecessor in the
        reserved parameter envelope; no historical row is updated.
        """

        if not isinstance(result, SimulationResult):
            raise TypeError("save_with_status requires a SimulationResult.")
        _uuid(forecast_id, "forecast_id")
        _uuid(intervention_id, "intervention_id")
        if not isinstance(scope, Scope):
            raise TypeError("scope must be a Scope.")
        instant = _aware_utc(occurred_at, "occurred_at")
        request = _request_id(request_id)
        creator = _coalesce_actor(created_by_id, actor_id)
        if simulation_id is not None:
            _uuid(simulation_id, "simulation_id")

        forecast = self._forecasts.get(forecast_id, scope=scope)
        if forecast is None:
            raise ValueError("Simulation forecast is absent or outside the supplied scope.")
        intervention = self.session.get(Intervention, intervention_id)
        if intervention is None:
            raise ValueError("Simulation intervention is absent.")
        if intervention.code != result.intervention_code:
            raise ValueError(
                "Simulation intervention_id does not match the result intervention code."
            )

        parameters = _json_mapping(result.parameters, "parameters")
        if _SUPERSEDES_PARAMETER in parameters:
            raise ValueError(f"{_SUPERSEDES_PARAMETER} is reserved for repository-managed lineage.")
        assumptions = _assumption_payload(result)
        existing = self._same_forecast_result(
            forecast_id,
            intervention_id,
            parameters,
            scope=scope,
        )
        if existing is not None:
            if simulation_id is not None and existing.id != simulation_id:
                raise Conflict("The requested simulation_id belongs to another simulation.")
            if not _same_persisted_result(existing, result, assumptions):
                raise Conflict(
                    "The same forecast, intervention and parameters were persisted with "
                    "different simulation output."
                )
            return SimulationWrite(
                simulation=self._annotate_required(existing, scope=scope),
                created=False,
                supersedes_simulation_id=_supersedes_id(existing),
            )
        if simulation_id is not None and self.session.get(Simulation, simulation_id) is not None:
            raise Conflict("The requested simulation_id is already in use.")

        prior = self._prior_simulation(
            forecast,
            intervention_id,
            parameters,
            scope=scope,
        )
        if prior is not None:
            parameters[_SUPERSEDES_PARAMETER] = str(prior.id)

        row = Simulation(
            id=simulation_id or new_id(),
            forecast_id=forecast_id,
            intervention_id=intervention_id,
            parameters=parameters,
            assumptions=assumptions,
            projected_cross_date=_calendar_date(result.projected_cross_date),
            probability=result.probability,
            delta_days=result.delta_days,
            delta_probability=result.delta_probability,
            created_at=instant,
            updated_at=instant,
            created_by_id=creator,
            updated_by_id=creator,
            request_id=request,
        )
        self.session.add(row)
        self.session.flush()
        lineage = SimulationLineage(
            simulation_id=row.id,
            supersedes_simulation_id=prior.id if prior is not None else None,
            superseded_by_simulation_id=None,
            based_on_superseded_run=False,
        )
        _attach_lineage(row, lineage)
        return SimulationWrite(
            simulation=row,
            created=True,
            supersedes_simulation_id=lineage.supersedes_simulation_id,
        )

    def lineage(self, simulation_id: UUID, *, scope: Scope) -> SimulationLineage | None:
        """Return one simulation's immutable and derived lineage."""

        row = self.get(simulation_id, scope=scope)
        if row is None:
            return None
        return _attached_lineage(row)

    get_lineage = lineage

    def retention_reference(
        self,
        simulation_id: UUID,
        *,
        scope: Scope | None = None,
        caller: UnscopedCaller | None = None,
        reason: str | None = None,
    ) -> SimulationRetentionReference:
        """Resolve memo references that protect a simulation from purge.

        A normal request must provide a portfolio scope.  The only permitted
        scope-free caller is the retention job, which goes through the base
        repository's audited unscoped read path.
        """

        _uuid(simulation_id, "simulation_id")
        if scope is not None:
            if caller is not None:
                raise ValueError("A scoped retention check cannot specify an unscoped caller.")
            target = super().get(simulation_id, scope=scope)
            memos = self._scoped_memos_for_simulation(target, scope=scope)
        else:
            if caller is not RETENTION_JOB_CALLER:
                raise ValueError("A scope-free retention check requires the retention job caller.")
            audit_reason = reason or "check memo references before simulation retention purge"
            target = self.get_unscoped(
                simulation_id,
                caller=RETENTION_JOB_CALLER,
                reason=audit_reason,
            )
            if target is None:
                return SimulationRetentionReference(simulation_id, (), exists=False)
            memos = self._memos.list_unscoped(
                caller=RETENTION_JOB_CALLER,
                reason=audit_reason,
            )
        if target is None:
            return SimulationRetentionReference(simulation_id, (), exists=False)

        memo_ids = tuple(
            sorted(
                (memo.id for memo in memos if _contains_uuid(memo.simulations, simulation_id)),
                key=str,
            )
        )
        return SimulationRetentionReference(simulation_id, memo_ids)

    def _scoped_memos_for_simulation(
        self,
        simulation: Simulation | None,
        *,
        scope: Scope,
    ) -> Sequence[Memo]:
        """Load only the owning borrower's memos for a scoped check."""

        if simulation is None:
            return ()
        borrower_id = self._borrower_id(simulation)
        if borrower_id is None:
            return ()
        statement: Select[tuple[Memo]] = cast(
            Select[tuple[Memo]], self._memos._scoped_select(scope)
        )
        statement = statement.where(Memo.borrower_id == borrower_id).order_by(Memo.id)
        return tuple(self.session.execute(statement).scalars().all())

    def _borrower_id(self, simulation: Simulation) -> UUID | None:
        """Resolve a simulation's borrower through its forecast lineage."""

        statement = (
            select(Borrower.id)
            .select_from(Simulation)
            .join(Forecast, Forecast.id == Simulation.forecast_id)
            .join(CovenantVersion, CovenantVersion.id == Forecast.covenant_version_id)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .join(Facility, Facility.id == Covenant.facility_id)
            .join(Borrower, Borrower.id == Facility.borrower_id)
            .where(Simulation.id == simulation.id)
            .limit(1)
        )
        return self.session.scalar(statement)

    def is_referenced_by_memo(
        self,
        simulation_id: UUID,
        *,
        scope: Scope | None = None,
        caller: UnscopedCaller | None = None,
        reason: str | None = None,
    ) -> bool:
        """Return whether a retained memo cites the simulation."""

        return self.retention_reference(
            simulation_id,
            scope=scope,
            caller=caller,
            reason=reason,
        ).is_referenced

    has_retention_reference = is_referenced_by_memo

    def can_purge(
        self,
        simulation_id: UUID,
        *,
        scope: Scope | None = None,
        caller: UnscopedCaller | None = None,
        reason: str | None = None,
    ) -> bool:
        """Return whether the retention job may remove the simulation."""

        return self.retention_reference(
            simulation_id,
            scope=scope,
            caller=caller,
            reason=reason,
        ).purgeable

    is_purgeable = can_purge

    def _same_forecast_result(
        self,
        forecast_id: UUID,
        intervention_id: UUID,
        parameters: Mapping[str, object],
        *,
        scope: Scope,
    ) -> Simulation | None:
        statement: Select[tuple[Simulation]] = cast(
            Select[tuple[Simulation]], self._scoped_select(scope)
        )
        statement = statement.where(
            Simulation.forecast_id == forecast_id,
            Simulation.intervention_id == intervention_id,
        ).order_by(Simulation.id)
        rows = tuple(self.session.execute(statement).scalars().all())
        matches = [row for row in rows if _input_parameters(row.parameters) == parameters]
        if len(matches) > 1:
            raise Conflict(
                "The same forecast, intervention and parameters have duplicate simulations."
            )
        return matches[0] if matches else None

    def _prior_simulation(
        self,
        forecast: Forecast,
        intervention_id: UUID,
        parameters: Mapping[str, object],
        *,
        scope: Scope,
    ) -> Simulation | None:
        current_run = self.session.get(ForecastRun, forecast.run_id)
        if current_run is None:
            raise ValueError("Simulation forecast run is absent.")
        statement: Select[tuple[Simulation]] = cast(
            Select[tuple[Simulation]], self._scoped_select(scope)
        )
        statement = statement.where(Simulation.intervention_id == intervention_id).order_by(
            Simulation.created_at,
            Simulation.id,
        )
        candidates = tuple(self.session.execute(statement).scalars().all())
        prior: Simulation | None = None
        prior_order: tuple[datetime, str] | None = None
        for candidate in candidates:
            if _input_parameters(candidate.parameters) != parameters:
                continue
            candidate_forecast = self.session.get(Forecast, candidate.forecast_id)
            if candidate_forecast is None:
                continue
            if (
                candidate_forecast.covenant_version_id != forecast.covenant_version_id
                or candidate_forecast.horizon_days != forecast.horizon_days
            ):
                continue
            candidate_run = self.session.get(ForecastRun, candidate_forecast.run_id)
            if candidate_run is None or not _run_precedes(candidate_run, current_run):
                continue
            candidate_order = _run_order(candidate_run)
            if prior_order is None or candidate_order > prior_order:
                prior = candidate
                prior_order = candidate_order
        return prior

    def _annotate(self, row: Simulation | None, *, scope: Scope) -> Simulation | None:
        if row is None:
            return None
        forecast = self.session.get(Forecast, row.forecast_id)
        based_on_superseded_run = False
        if forecast is not None:
            based_on_superseded_run = self._has_newer_forecast(forecast)

        superseded_by: UUID | None = None
        superseded_by = self._superseding_simulation_id(row.id, scope=scope)

        lineage = SimulationLineage(
            simulation_id=row.id,
            supersedes_simulation_id=_supersedes_id(row),
            superseded_by_simulation_id=superseded_by,
            based_on_superseded_run=based_on_superseded_run,
        )
        _attach_lineage(row, lineage)
        return row

    def _superseding_simulation_id(
        self,
        simulation_id: UUID,
        *,
        scope: Scope,
    ) -> UUID | None:
        """Find the unique child link without loading the whole portfolio."""

        statement: Select[tuple[Simulation]] = cast(
            Select[tuple[Simulation]], self._scoped_select(scope)
        )
        if self.session.get_bind().dialect.name == "sqlite":
            criterion = func.json_extract(
                Simulation.parameters,
                f"$.{_SUPERSEDES_PARAMETER}",
            ) == str(simulation_id)
        elif self.session.get_bind().dialect.name == "postgresql":
            criterion = sql_cast(Simulation.parameters, JSONB)[
                _SUPERSEDES_PARAMETER
            ].as_string() == str(simulation_id)
        else:
            return None
        rows = tuple(
            self.session.execute(statement.where(criterion).order_by(Simulation.id).limit(2))
            .scalars()
            .all()
        )
        if len(rows) > 1:
            raise Conflict(f"Simulation {simulation_id} has multiple superseding records.")
        return rows[0].id if rows else None

    def _annotate_required(self, row: Simulation, *, scope: Scope) -> Simulation:
        annotated = self._annotate(row, scope=scope)
        if annotated is None:
            raise RuntimeError("A loaded simulation unexpectedly disappeared during annotation.")
        return annotated

    def _has_newer_forecast(self, forecast: Forecast) -> bool:
        run = self.session.get(ForecastRun, forecast.run_id)
        if run is None:
            return False
        statement = (
            select(Forecast.id)
            .join(ForecastRun, ForecastRun.id == Forecast.run_id)
            .where(
                Forecast.covenant_version_id == forecast.covenant_version_id,
                Forecast.horizon_days == forecast.horizon_days,
                ForecastRun.state == _COMPLETE_RUN_STATE,
                or_(
                    ForecastRun.started_at > run.started_at,
                    and_(
                        ForecastRun.started_at == run.started_at,
                        ForecastRun.id > run.id,
                    ),
                ),
            )
            .limit(1)
        )
        return self.session.scalar(statement) is not None


SimulationPersistenceRepository = SimulationRepository
SqlAlchemySimulationRepository = SimulationRepository


def _assumption_payload(result: SimulationResult) -> dict[str, object]:
    result_mapping = _json_value(result.to_mapping(), "result")
    if not isinstance(result_mapping, dict):
        raise TypeError("Simulation result mapping must be a JSON object.")
    intervention = {
        "code": result.intervention.code,
        "text": result.intervention.text,
        "effect_model": result.intervention.model_type.value,
        "effect_parameters": _json_value(
            result.intervention.effect_parameters,
            "effect_parameters",
        ),
        "applicable_covenant_classes": _json_value(
            result.intervention.applicable_covenant_classes,
            "applicable_covenant_classes",
        ),
    }
    return {
        "assumptions": list(result.assumptions),
        "result": result_mapping,
        "intervention": intervention,
    }


def _stored_fraction(value: Decimal | None) -> Decimal | None:
    """Round a computed fraction the way its column stores it.

    ``Simulation.probability`` and ``delta_probability`` are ``FractionValue``
    columns, which quantize to four decimal places on write.  A freshly
    recomputed ``SimulationResult`` carries the full-precision Decimal, so
    comparing it raw against a value that has already been through the column
    is never equal for any probability with more than four decimal places --
    which is nearly all of them.  Quantizing here asks the question the caller
    actually means: would persisting this result produce the row already
    stored?
    """

    if value is None:
        return None
    return value.quantize(_FRACTION_QUANTUM)


def _same_persisted_result(
    row: Simulation,
    result: SimulationResult,
    assumptions: Mapping[str, object],
) -> bool:
    return (
        _calendar_date(row.projected_cross_date) == _calendar_date(result.projected_cross_date)
        and _stored_fraction(row.probability) == _stored_fraction(result.probability)
        and row.delta_days == result.delta_days
        and _stored_fraction(row.delta_probability) == _stored_fraction(result.delta_probability)
        and _canonical(row.assumptions) == _canonical(assumptions)
    )


def _supersedes_id(row: Simulation) -> UUID | None:
    raw = row.parameters.get(_SUPERSEDES_PARAMETER) if isinstance(row.parameters, Mapping) else None
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"Stored {_SUPERSEDES_PARAMETER} must be a UUID string.")
    try:
        return UUID(raw)
    except ValueError as error:
        raise ValueError(f"Stored {_SUPERSEDES_PARAMETER} must be a UUID string.") from error


def _input_parameters(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Stored simulation parameters must be a JSON object.")
    return {str(key): item for key, item in value.items() if str(key) != _SUPERSEDES_PARAMETER}


def _contains_uuid(value: object, target: UUID) -> bool:
    if isinstance(value, str):
        try:
            return UUID(value) == target
        except ValueError:
            return False
    if isinstance(value, UUID):
        return value == target
    if isinstance(value, Mapping):
        return any(_contains_uuid(item, target) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(_contains_uuid(item, target) for item in value)
    return False


def _json_mapping(value: Mapping[str, object], field: str) -> dict[str, object]:
    converted = _json_value(value, field)
    if not isinstance(converted, dict):
        raise TypeError(f"{field} must be a JSON object.")
    json.dumps(converted, ensure_ascii=False, allow_nan=False, sort_keys=True)
    return converted


def _json_value(value: object, field: str) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field} contains a non-finite Decimal.")
        return format(value, "f")
    if isinstance(value, datetime):
        return _aware_utc(value, field).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value, field)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item, f"{field}.{key}") for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item, f"{field}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, set | frozenset):
        return sorted((_json_value(item, field) for item in value), key=str)
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field} contains a non-finite float.")
        return value
    if value is None or isinstance(value, str | int | bool):
        return value
    raise TypeError(f"{field} contains unsupported value {type(value).__name__}.")


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _canonical(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_canonical(item) for item in value)
    return value


def _attach_lineage(row: Simulation, lineage: SimulationLineage) -> None:
    """Attach read-only-in-practice metadata without changing the schema."""

    object.__setattr__(row, "_simulation_lineage", lineage)
    object.__setattr__(row, "supersedes_simulation_id", lineage.supersedes_simulation_id)
    object.__setattr__(row, "superseded_by_simulation_id", lineage.superseded_by_simulation_id)
    object.__setattr__(row, "based_on_superseded_run", lineage.based_on_superseded_run)
    object.__setattr__(row, "is_based_on_superseded_run", lineage.based_on_superseded_run)


def _attached_lineage(row: Simulation) -> SimulationLineage:
    try:
        return cast(SimulationLineage, row.__dict__["_simulation_lineage"])
    except KeyError as error:
        raise RuntimeError("Simulation lineage was not attached before it was read.") from error


def _run_order(run: ForecastRun) -> tuple[datetime, str]:
    return _aware_utc(run.started_at, "forecast_run.started_at"), str(run.id)


def _run_precedes(candidate: ForecastRun, current: ForecastRun) -> bool:
    return _run_order(candidate) < _run_order(current)


def _coalesce_actor(first: UUID | None, second: UUID | None) -> UUID | None:
    _uuid_or_none(first, "created_by_id")
    _uuid_or_none(second, "actor_id")
    if first is not None and second is not None and first != second:
        raise ValueError("created_by_id and actor_id must identify the same creator.")
    return first if first is not None else second


def _calendar_date(value: date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("projected_cross_date must be a calendar date or None.")
    return value


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


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


def _uuid(value: object, field: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID.")
    return value


def _uuid_or_none(value: object, field: str) -> None:
    if value is not None and not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID or None.")


__all__ = [
    "SimulationLineage",
    "SimulationPersistenceRepository",
    "SimulationRepository",
    "SimulationRetentionReference",
    "SimulationWrite",
    "SqlAlchemySimulationRepository",
]
