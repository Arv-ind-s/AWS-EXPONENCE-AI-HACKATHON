"""Deterministic forecast scoring and persistence orchestration.

This service is the write boundary for forecast facts.  It computes the pure
forecast stages from :mod:`covenant_radar.domain.forecast`, writes every
covenant (including explicitly uncomputable ones), and never exposes a
probability except through the persisted :class:`Forecast` rows returned in a
``ScoringResult``.  A run is complete only when its expected covenant count
has been attempted; partial runs remain clearly unavailable to a latest-run
reader and can be resumed by id.

The service deliberately does not commit.  The application's unit of work
owns the transaction boundary, while each covenant is protected by a
savepoint so a failed row cannot leave a half-written path in the enclosing
transaction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import Conflict, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.forecast import Forecast, ForecastDriver, ForecastPath, ForecastRun
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.db.repositories.driver import DriverRepository
from covenant_radar.db.repositories.forecast import (
    COMPLETE,
    INCOMPLETE,
    RUNNING,
    ForecastRepository,
)
from covenant_radar.db.repositories.trace import TraceRepository
from covenant_radar.domain.covenants.headroom import signed_headroom
from covenant_radar.domain.forecast import (
    ConfidenceResult,
    Direction,
    FeatureSnapshot,
    ForecastPredictor,
    Observation,
    PressureResult,
    ProbabilityResult,
    Projection,
    ThresholdChange,
    Weights,
    confidence,
    first_crossing,
    probability,
    project,
)
from covenant_radar.domain.forecast.attribution import DriverShare, attribute
from covenant_radar.domain.trace import TraceRecord, stage_record

_SCORING_RULE_VERSION = "forecast.scoring.v1"
_REQUEST_ID_MAX_LENGTH = 40
_MODEL_VERSION_MAX_LENGTH = 50
_MAX_HORIZON_DAYS = 3660
_ZERO = Decimal("0")
_ONE = Decimal("1")
_PERCENT = Decimal("100")
_FORECAST_TRACE_RULE_VERSION = "forecast.trend_pressure.v1"
_ATTRIBUTION_UNAVAILABLE_REASON = "attribution is unavailable because the T5 threshold is missing"
_OTHER_DRIVER_NAME = "other"
_NEUTRAL_DRIVER_NAME = "neutral"

#: The ML model runs beside the deterministic one and is recorded in full, but
#: the deterministic probability is what the queue, the band and the case are
#: built from.  This is the default because an artifact sitting on disk is not
#: an approval: promoting a challenger is a model-register decision.
SHADOW_PREDICTOR_MODE: Final[str] = "shadow"
#: The model's probability replaces the deterministic one.  Only
#: `services.nightly_runtime` selects this, and only for an artifact whose
#: component carries an approved `model_registration` row.
CHAMPION_PREDICTOR_MODE: Final[str] = "champion"
PREDICTOR_MODES: Final[frozenset[str]] = frozenset(
    {SHADOW_PREDICTOR_MODE, CHAMPION_PREDICTOR_MODE}
)


class AuditWriter(Protocol):
    """The append-only audit boundary supplied by the caller."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's current transaction."""


@dataclass(frozen=True, slots=True)
class ForecastCandidate:
    """All deterministic facts needed to score one covenant version.

    ``completeness`` and ``evidence_support`` are already-normalised inputs
    from the preceding data/evidence stages.  The defaults represent a
    complete, fully supported candidate; a missing series or data-as-of date
    still fails closed as uncomputable rather than inventing a value.
    """

    covenant_version_id: UUID
    threshold: Decimal
    direction: Direction | str
    series: Sequence[Observation | Mapping[str, object] | object] = ()
    pressure: Decimal | PressureResult = _ZERO
    completeness: Decimal = _ONE
    evidence_support: Decimal = _ONE
    data_as_of: date | None = None
    computable: bool = True
    not_computable_reason: str | None = None
    already_breached: bool = False
    recent_periods: int | None = None
    period_days: int | Decimal | None = None
    threshold_changes: Sequence[object] = ()
    formula_inputs: Mapping[str, object] = field(default_factory=dict)
    probability_weights: Weights | Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.covenant_version_id, UUID):
            raise TypeError("covenant_version_id must be a UUID.")
        threshold = _decimal(self.threshold, "threshold")
        if threshold == _ZERO:
            raise ValueError("threshold must not be zero.")
        direction = Direction.from_value(self.direction)
        if not isinstance(self.computable, bool):
            raise TypeError("computable must be a boolean.")
        if not isinstance(self.already_breached, bool):
            raise TypeError("already_breached must be a boolean.")
        if self.data_as_of is not None:
            _calendar_date(self.data_as_of, "data_as_of")
        if self.not_computable_reason is not None:
            _bounded_text(self.not_computable_reason, "not_computable_reason", 500)
        if not isinstance(self.formula_inputs, Mapping):
            raise TypeError("formula_inputs must be a mapping.")
        if self.recent_periods is not None and (
            isinstance(self.recent_periods, bool)
            or not isinstance(self.recent_periods, int)
            or self.recent_periods <= 0
        ):
            raise ValueError("recent_periods must be a positive integer or None.")
        if self.period_days is not None:
            _positive_decimal_or_int(self.period_days, "period_days")
        object.__setattr__(self, "threshold", threshold)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "series", tuple(self.series))
        object.__setattr__(self, "threshold_changes", tuple(self.threshold_changes))
        object.__setattr__(self, "completeness", _fraction(self.completeness, "completeness"))
        object.__setattr__(
            self,
            "evidence_support",
            _fraction(self.evidence_support, "evidence_support"),
        )

    @classmethod
    def from_value(
        cls, value: ForecastCandidate | Mapping[str, object] | object
    ) -> ForecastCandidate:
        """Normalise the common adapter shapes used by scoring jobs."""

        if isinstance(value, cls):
            return value
        covenant_id = _read_any(value, "covenant_version_id", "version_id", "id")
        threshold = _read_any(value, "threshold")
        direction = _read_any(value, "direction")
        series = _read_any(value, "series", "observations", "history", default=())
        pressure = _read_any(value, "pressure", "evidence_pressure", default=_ZERO)
        completeness = _read_any(value, "completeness", default=_ONE)
        support = _read_any(value, "evidence_support", "support", default=_ONE)
        return cls(
            covenant_version_id=cast(UUID, covenant_id),
            threshold=cast(Decimal, threshold),
            direction=cast(Direction | str, direction),
            series=cast(Sequence[Observation | Mapping[str, object] | object], series),
            pressure=cast(Decimal | PressureResult, pressure),
            completeness=cast(Decimal, completeness),
            evidence_support=cast(Decimal, support),
            data_as_of=cast(
                date | None,
                _read_any(value, "data_as_of", "data_as_of_date", "last_data_date", default=None),
            ),
            computable=cast(bool, _read_any(value, "computable", default=True)),
            not_computable_reason=cast(
                str | None,
                _read_any(value, "not_computable_reason", "reason", default=None),
            ),
            already_breached=cast(bool, _read_any(value, "already_breached", default=False)),
            recent_periods=cast(int | None, _read_any(value, "recent_periods", default=None)),
            period_days=cast(int | Decimal | None, _read_any(value, "period_days", default=None)),
            threshold_changes=cast(
                Sequence[object],
                _read_any(value, "threshold_changes", "threshold_schedule", default=()),
            ),
            formula_inputs=cast(
                Mapping[str, object], _read_any(value, "formula_inputs", default={})
            ),
            probability_weights=cast(
                Weights | Mapping[str, object] | None,
                _read_any(value, "probability_weights", "weights", default=None),
            ),
        )


@dataclass(frozen=True, slots=True)
class _ForecastComputation:
    """The computed facts needed to persist one horizon explanation."""

    row: Forecast
    projection: Projection | None
    probability_result: ProbabilityResult | None
    confidence_result: ConfidenceResult
    crossing_result: object | None
    computable: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class _DriverMetadata:
    """Persistence metadata retained beside a normalized driver share."""

    evidence_id: UUID | None
    driver_type: str
    link_status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class ScoringResult:
    """The persisted view of one scoring pass.

    ``probabilities`` is derived from ``forecasts`` and is intentionally not
    an independent computation or a second source of truth.
    """

    run: ForecastRun
    forecasts: tuple[Forecast, ...]
    paths: tuple[ForecastPath, ...]
    content_hash: str
    resumed: bool

    @property
    def run_id(self) -> UUID:
        return self.run.id

    @property
    def state(self) -> str:
        return self.run.state

    @property
    def probabilities(self) -> tuple[Decimal, ...]:
        """Return only probabilities that already exist on stored forecasts."""

        return tuple(row.probability for row in self.forecasts if row.probability is not None)

    @property
    def complete(self) -> bool:
        return self.run.state == COMPLETE


class ForecastScoringService:
    """Compute and persist one deterministic, resumable forecast run."""

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        threshold_store: object | None = None,
        thresholds: object | None = None,
        weights: Weights | Mapping[str, object] | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
        repository: ForecastRepository | None = None,
        driver_repository: DriverRepository | None = None,
        trace_repository: TraceRepository | None = None,
        predictor: ForecastPredictor | None = None,
        predictor_mode: str = SHADOW_PREDICTOR_MODE,
    ) -> None:
        if not isinstance(session, Session):
            raise TypeError("ForecastScoringService requires a SQLAlchemy Session.")
        if predictor_mode not in PREDICTOR_MODES:
            raise ValueError(
                f"predictor_mode must be one of {sorted(PREDICTOR_MODES)}, "
                f"not {predictor_mode!r}."
            )
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("ForecastScoringService requires an append-only audit writer.")
        if threshold_store is not None and thresholds is not None:
            raise TypeError("Pass threshold_store or thresholds, not both.")
        if repository is not None and not isinstance(repository, ForecastRepository):
            raise TypeError("repository must be a ForecastRepository.")
        if driver_repository is not None and not isinstance(driver_repository, DriverRepository):
            raise TypeError("driver_repository must be a DriverRepository.")
        if trace_repository is not None and not isinstance(trace_repository, TraceRepository):
            raise TypeError("trace_repository must be a TraceRepository.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("Forecast scoring clock must expose now().")
        self.session = session
        self.audit = audit
        self.thresholds = threshold_store if threshold_store is not None else thresholds
        self.weights = _normalise_weights(weights) if weights is not None else None
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        _bounded_text(self.request_id, "request_id", _REQUEST_ID_MAX_LENGTH)
        self.repository = repository or ForecastRepository(session)
        self.drivers = driver_repository or DriverRepository(session)
        self.traces = trace_repository or TraceRepository(
            session,
            clock=self.clock,
            request_id=self.request_id,
        )
        self.predictor = predictor
        self.predictor_mode = predictor_mode

    def score(
        self,
        candidates: Iterable[ForecastCandidate | Mapping[str, object] | object],
        *,
        as_of_date: date,
        horizons: Sequence[int] | None = None,
        configuration: object | None = None,
        run_id: UUID | None = None,
        resume_run_id: UUID | None = None,
        threshold_snapshot_id: UUID | None = None,
        model_version: str | None = None,
        rule_versions: str | Mapping[str, object] | None = None,
        job_run_id: UUID | None = None,
        actor_id: UUID | None = None,
        request_id: str | None = None,
        interrupt_after: int | None = None,
        stop_after: int | None = None,
        weights: Weights | Mapping[str, object] | None = None,
        threshold_store: object | None = None,
    ) -> ScoringResult:
        """Score all supplied candidates and persist a complete or partial run.

        ``interrupt_after``/``stop_after`` are deterministic job-runner
        controls, useful for checkpointing and integration verification.  An
        interrupted pass returns an ``incomplete`` result; it never presents
        the partial rows as a complete day's result.
        """

        scoring_date = _calendar_date(as_of_date, "as_of_date")
        effective_run_id = _coalesce_uuid(run_id, resume_run_id, "run_id")
        interruption = _coalesce_int(
            interrupt_after,
            stop_after,
            "interrupt_after",
        )
        if interruption is not None and interruption < 0:
            raise ValueError("interrupt_after must be a non-negative integer or None.")
        values = _normalise_candidates(candidates)
        _unique_candidate_ids(values)

        configured_horizons = _horizons_from_configuration(horizons, configuration)
        effective_weights = _resolve_weights(weights, configuration, self.weights)
        effective_thresholds = threshold_store if threshold_store is not None else self.thresholds
        snapshot_id = threshold_snapshot_id or _snapshot_id(effective_thresholds)
        if snapshot_id is None:
            raise ValidationError(
                "A threshold_snapshot_id or threshold store is required for a forecast run.",
                field="threshold_snapshot_id",
            )
        if not isinstance(snapshot_id, UUID):
            raise TypeError("threshold_snapshot_id must be a UUID.")
        effective_model_version = model_version or _SCORING_RULE_VERSION
        _bounded_text(effective_model_version, "model_version", _MODEL_VERSION_MAX_LENGTH)
        effective_rule_versions = _normalise_rule_versions(rule_versions, effective_model_version)
        effective_request_id = request_id or self.request_id
        _bounded_text(effective_request_id, "request_id", _REQUEST_ID_MAX_LENGTH)
        now = self._now()

        resumed = effective_run_id is not None
        if effective_run_id is None:
            run = self.repository.create_run(
                as_of_date=scoring_date,
                threshold_snapshot_id=snapshot_id,
                model_version=effective_model_version,
                covenant_count=len(values),
                started_at=now,
                job_run_id=job_run_id,
                actor_id=actor_id,
                request_id=effective_request_id,
            )
        else:
            loaded_run = self.repository.get_run(effective_run_id, for_update=True)
            if loaded_run is None:
                raise ValidationError(
                    f"Forecast run {effective_run_id} was not found.", field="run_id"
                )
            run = loaded_run
            _validate_resume_metadata(
                run,
                as_of_date=scoring_date,
                threshold_snapshot_id=snapshot_id,
                model_version=effective_model_version,
                candidate_count=len(values),
            )
            committed_horizons = self.repository.horizons_for_run(run.id)
            if committed_horizons and committed_horizons != set(configured_horizons):
                raise Conflict(
                    "A forecast run cannot be resumed with a different horizon configuration."
                )
            if run.state != COMPLETE:
                self.repository.begin_resume(run, updated_at=now)

        if interruption == 0:
            self.repository.mark_incomplete(run, finished_at=now)
            return self._result(
                run, resumed=resumed, actor_id=actor_id, request_id=effective_request_id
            )

        processed = 0
        try:
            for candidate in values:
                if interruption is not None and processed >= interruption:
                    break
                self._persist_candidate(
                    run,
                    candidate,
                    scoring_date=scoring_date,
                    horizons=configured_horizons,
                    weights=effective_weights,
                    thresholds=effective_thresholds,
                    rule_versions=effective_rule_versions,
                    model_version=effective_model_version,
                    actor_id=actor_id,
                    request_id=effective_request_id,
                    now=now,
                )
                processed += 1
        except Exception:
            # The caller can still commit the incomplete checkpoint or roll
            # back the whole work unit.  Either way, a complete run can never
            # be observed after a computation/persistence failure.
            if run.state != COMPLETE:
                self.repository.mark_incomplete(run, finished_at=self._now())
            raise

        attempted_count = len(self.repository.attempted_covenant_ids(run.id))
        if attempted_count == run.covenant_count:
            if run.state != COMPLETE:
                self.repository.mark_complete(
                    run,
                    finished_at=self._now(),
                    attempted_count=attempted_count,
                )
        else:
            self.repository.mark_incomplete(run, finished_at=self._now())
        return self._result(
            run, resumed=resumed, actor_id=actor_id, request_id=effective_request_id
        )

    run = score
    score_run = score

    def _persist_candidate(
        self,
        run: ForecastRun,
        candidate: ForecastCandidate,
        *,
        scoring_date: date,
        horizons: tuple[int, ...],
        weights: Weights | None,
        thresholds: object | None,
        rule_versions: Mapping[str, object],
        model_version: str,
        actor_id: UUID | None,
        request_id: str,
        now: datetime,
    ) -> None:
        """Compute one covenant and atomically stage all of its rows."""

        projection: Projection | None = None
        staleness_days = _staleness(candidate.data_as_of, scoring_date)
        computable, reason = _candidate_computability(candidate)
        if computable:
            observations = tuple(Observation.from_value(value) for value in candidate.series)
            if not observations:
                computable = False
                reason = "no observations available for forecast projection"
            elif candidate.data_as_of is None:
                computable = False
                reason = "data_as_of is required to record forecast staleness"
            else:
                projection = project(
                    observations,
                    candidate.pressure,
                    horizons[-1],
                    candidate.threshold,
                    candidate.direction,
                    recent_periods=candidate.recent_periods,
                    period_days=candidate.period_days,
                )
                if projection.current_value is None:
                    computable = False
                    reason = projection.reason or "forecast projection has no usable value"

        confidence_result = _confidence_result(
            candidate,
            staleness_days,
            thresholds,
            computable=computable,
        )
        path_rows = _path_rows(
            run,
            candidate,
            projection,
            horizon=horizons[-1],
            actor_id=actor_id,
            request_id=request_id,
            now=now,
        )
        forecast_computations = _forecast_rows(
            run,
            candidate,
            projection,
            horizons=horizons,
            weights=weights or _candidate_weights(candidate),
            confidence_result=confidence_result,
            staleness_days=staleness_days,
            scoring_date=scoring_date,
            computable=computable,
            not_computable_reason=reason,
            rule_versions=rule_versions,
            model_version=model_version,
            actor_id=actor_id,
            request_id=request_id,
            now=now,
            predictor=self.predictor,
            predictor_mode=self.predictor_mode,
            feature_snapshot=_feature_snapshot(candidate, projection, staleness_days),
        )
        with self.session.begin_nested():
            for path_row in path_rows:
                self.repository.save_path(path_row)
            for computation in forecast_computations:
                forecast_row = self.repository.save_forecast(computation.row)
                self._persist_explanation(
                    run,
                    candidate,
                    computation,
                    forecast_row,
                    thresholds=thresholds,
                    rule_versions=rule_versions,
                    actor_id=actor_id,
                    request_id=request_id,
                    now=now,
                )
            self.audit.record(
                AuditEventType.FORECAST_CANDIDATE_SCORED.value,
                ("covenant_version", candidate.covenant_version_id),
                {
                    "run_id": str(run.id),
                    "covenant_version_id": str(candidate.covenant_version_id),
                    "computable": computable,
                    "not_computable_reason": reason,
                    "staleness_days": staleness_days,
                    "horizons": list(horizons),
                },
                actor=actor_id,
                request_id=request_id,
            )

    def _persist_explanation(
        self,
        run: ForecastRun,
        candidate: ForecastCandidate,
        computation: _ForecastComputation,
        forecast_row: Forecast,
        *,
        thresholds: object | None,
        rule_versions: Mapping[str, object],
        actor_id: UUID | None,
        request_id: str,
        now: datetime,
    ) -> None:
        driver_rows, driver_details = self._driver_rows(
            forecast_row,
            computation,
            thresholds=thresholds,
            actor_id=actor_id,
            request_id=request_id,
            now=now,
        )
        self.drivers.save_many(driver_rows)
        trace = _stage4_trace(
            run,
            candidate,
            computation,
            forecast_row,
            driver_details,
            thresholds=thresholds,
            rule_versions=rule_versions,
        )
        self._write_trace_once(forecast_row.id, trace, actor_id=actor_id, request_id=request_id)

    def _driver_rows(
        self,
        forecast_row: Forecast,
        computation: _ForecastComputation,
        *,
        thresholds: object | None,
        actor_id: UUID | None,
        request_id: str,
        now: datetime,
    ) -> tuple[tuple[ForecastDriver, ...], tuple[dict[str, object], ...]]:
        shares, metadata = _attribution(
            computation,
            thresholds=thresholds,
            session=self.session,
        )
        persisted_shares = _quantized_driver_shares(shares)
        rows: list[ForecastDriver] = []
        details: list[dict[str, object]] = []
        for share in persisted_shares:
            driver_metadata = metadata.get(share.name)
            if driver_metadata is None:
                raise RuntimeError(f"Attribution metadata is missing for driver {share.name!r}.")
            rows.append(
                ForecastDriver(
                    id=new_id(),
                    forecast_id=forecast_row.id,
                    name=share.name,
                    share=share.share,
                    evidence_id=driver_metadata.evidence_id,
                    is_other=share.name == _OTHER_DRIVER_NAME,
                    created_at=now,
                    updated_at=now,
                    created_by_id=actor_id,
                    updated_by_id=actor_id,
                    request_id=request_id,
                )
            )
            details.append(
                {
                    "name": share.name,
                    "share": share.share,
                    "evidence_id": driver_metadata.evidence_id,
                    "type": driver_metadata.driver_type,
                    "link_status": driver_metadata.link_status,
                    "is_other": share.name == _OTHER_DRIVER_NAME,
                    "reason": share.reason or driver_metadata.reason,
                }
            )
        return tuple(rows), tuple(details)

    def _write_trace_once(
        self,
        forecast_id: UUID,
        record: TraceRecord,
        *,
        actor_id: UUID | None,
        request_id: str,
    ) -> None:
        """Keep a resumable forecast from producing duplicate explanations."""

        history = self.traces.history(("forecast", forecast_id), stage=4)
        if history:
            current = history[-1]
            if not _same_trace(current, record):
                raise Conflict(f"Forecast {forecast_id} already has a different stage-4 trace row.")
            return
        self.traces.write(
            ("forecast", forecast_id),
            record,
            actor_id=actor_id,
            request_id=request_id,
        )

    def _result(
        self,
        run: ForecastRun,
        *,
        resumed: bool,
        actor_id: UUID | None,
        request_id: str,
    ) -> ScoringResult:
        forecast_rows = tuple(
            self.session.execute(
                select(Forecast)
                .where(Forecast.run_id == run.id)
                .order_by(Forecast.covenant_version_id, Forecast.horizon_days)
            )
            .scalars()
            .all()
        )
        path_rows = tuple(
            self.session.execute(
                select(ForecastPath)
                .where(ForecastPath.run_id == run.id)
                .order_by(ForecastPath.covenant_version_id, ForecastPath.day_offset)
            )
            .scalars()
            .all()
        )
        # The per-candidate summary this scoring pass produced, across every
        # covenant it attempted, so the run's shape survives even when every
        # individual `forecast_candidate_scored` event is read separately.
        self.audit.record(
            AuditEventType.FORECAST_RUN_SCORED.value,
            ("forecast_run", run.id),
            {
                "as_of_date": run.as_of_date.isoformat(),
                "threshold_snapshot_id": str(run.threshold_snapshot_id),
                "model_version": run.model_version,
                "covenant_count": run.covenant_count,
                "attempted_count": len(
                    {row.covenant_version_id for row in forecast_rows}
                ),
                "state": run.state,
                "resumed": resumed,
            },
            actor=actor_id,
            request_id=request_id,
        )
        return ScoringResult(
            run=run,
            forecasts=forecast_rows,
            paths=path_rows,
            content_hash=_run_content_hash(run, forecast_rows, path_rows),
            resumed=resumed,
        )

    def _now(self) -> datetime:
        value = self.clock.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Forecast scoring clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)


def _path_rows(
    run: ForecastRun,
    candidate: ForecastCandidate,
    projection: Projection | None,
    *,
    horizon: int,
    actor_id: UUID | None,
    request_id: str,
    now: datetime,
) -> tuple[ForecastPath, ...]:
    points = projection.path if projection is not None else ()
    by_day = {point.day: point for point in points}
    rows: list[ForecastPath] = []
    for day in range(horizon + 1):
        point = by_day.get(day)
        value = point.value if point is not None else None
        headroom = (
            signed_headroom(value, candidate.threshold, candidate.direction)
            if value is not None
            else None
        )
        rows.append(
            ForecastPath(
                id=new_id(),
                run_id=run.id,
                covenant_version_id=candidate.covenant_version_id,
                day_offset=day,
                projected_value=_quantize(value, "0.00000001"),
                headroom_pct=_quantize(headroom, "0.0001"),
                created_at=now,
                updated_at=now,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                request_id=request_id,
            )
        )
    return tuple(rows)


def _feature_snapshot(
    candidate: ForecastCandidate,
    projection: Projection | None,
    staleness_days: int | None,
) -> FeatureSnapshot | None:
    """Build the allow-listed point-in-time feature vector for the ML port.

    Values are all already available to the deterministic scorer at the
    scoring instant.  In particular, no borrower/covenant identifiers,
    document text or subsequent dispositions can enter this vector.
    """

    if projection is None or projection.current_value is None:
        return None
    direction = cast(Direction, candidate.direction)
    pressure = projection.pressure
    return FeatureSnapshot(
        {
            "current_value": projection.current_value,
            "threshold": candidate.threshold,
            "signed_headroom": signed_headroom(
                projection.current_value, candidate.threshold, direction
            ),
            "slope": projection.slope,
            "net_per_day_drift": projection.net_per_day_drift,
            "evidence_pressure": pressure,
            "completeness": candidate.completeness,
            "evidence_support": candidate.evidence_support,
            "staleness_days": Decimal(staleness_days or 0),
            "observation_count": Decimal(len(projection.usable_observations)),
            "direction_max": Decimal("1") if direction is Direction.MAX else Decimal("0"),
        }
    )


def _forecast_rows(
    run: ForecastRun,
    candidate: ForecastCandidate,
    projection: Projection | None,
    *,
    horizons: tuple[int, ...],
    weights: Weights | Mapping[str, object] | None,
    confidence_result: ConfidenceResult,
    staleness_days: int | None,
    scoring_date: date,
    computable: bool,
    not_computable_reason: str | None,
    rule_versions: Mapping[str, object],
    model_version: str,
    actor_id: UUID | None,
    request_id: str,
    now: datetime,
    predictor: ForecastPredictor | None,
    predictor_mode: str,
    feature_snapshot: FeatureSnapshot | None,
) -> tuple[_ForecastComputation, ...]:
    effective_weights = _normalise_weights(weights) if weights is not None else None
    if computable and effective_weights is None:
        raise ValidationError(
            "Probability weights are required for a computable forecast.",
            field="probability_weights",
        )
    computations: list[_ForecastComputation] = []
    for horizon in horizons:
        probability_result = None
        crossing_result = None
        endpoint = None
        if computable and projection is not None:
            horizon_projection = (
                projection
                if horizon == projection.horizon_days
                else project(
                    projection.usable_observations,
                    projection.requested_pressure,
                    horizon,
                    candidate.threshold,
                    candidate.direction,
                    recent_periods=candidate.recent_periods,
                    period_days=candidate.period_days,
                )
            )
            endpoint = horizon_projection.path[-1].value
            if endpoint is not None and effective_weights is not None:
                direction = cast(Direction, candidate.direction)
                distance = _distance_to_boundary(endpoint, candidate.threshold, direction)
                velocity = (
                    horizon_projection.net_per_day_drift
                    if direction is Direction.MAX
                    else -horizon_projection.net_per_day_drift
                )
                crossing_result = first_crossing(
                    horizon_projection,
                    as_of_date=scoring_date,
                    threshold_changes=cast(
                        Sequence[ThresholdChange | Mapping[str, object] | Sequence[object]],
                        candidate.threshold_changes,
                    ),
                )
                probability_result = probability(
                    distance,
                    velocity,
                    horizon_projection.pressure,
                    horizon,
                    effective_weights,
                    already_breached=(
                        candidate.already_breached or crossing_result.crossing_day == 0
                    ),
                )
        shown_probability = (
            probability_result.probability
            if probability_result is not None and not confidence_result.probability_suppressed
            else None
        )
        probability_source = "deterministic"
        fallback_reason: str | None = None
        prediction = None
        challenger_probability: Decimal | None = None
        if shown_probability is not None and predictor is not None:
            if feature_snapshot is None:
                fallback_reason = (
                    "ML feature snapshot is unavailable; deterministic probability retained"
                )
            elif probability_result is not None and probability_result.already_breached:
                # An already-crossed covenant is an observed fact, not a
                # forecast.  The deterministic stage clamps it to the
                # configured maximum; letting a model that predicts a *future*
                # crossing overwrite that would report a live breach as a near
                # -zero probability and silently drop it out of the act band.
                fallback_reason = (
                    "covenant is already in breach; deterministic maximum retained over "
                    "the model probability"
                )
            else:
                try:
                    prediction = predictor.predict(feature_snapshot, horizon_days=horizon)
                    challenger_probability = prediction.probability
                except Exception as error:  # Prediction must never break the scoring run.
                    fallback_reason = f"ML prediction unavailable: {type(error).__name__}"
        if challenger_probability is not None:
            if predictor_mode == CHAMPION_PREDICTOR_MODE:
                shown_probability = challenger_probability
                probability_source = "ml"
            else:
                # `spec §R-14`: the deterministic model is what the screen,
                # the band and the case are built from.  A challenger runs
                # beside it and is recorded in full — probability, drivers and
                # artifact checksum — but never silently becomes the number a
                # credit officer acts on.  Promoting it is a registry decision
                # (`predictor_mode`), not a side effect of the artifact file
                # happening to be present on disk.
                probability_source = "deterministic"
                fallback_reason = (
                    "ML challenger runs in shadow mode; deterministic probability retained"
                )
        formula: dict[str, object] = {
            "scoring_rule_version": _SCORING_RULE_VERSION,
            "trace_rule_version": _FORECAST_TRACE_RULE_VERSION,
            "attribution_rule_version": _FORECAST_TRACE_RULE_VERSION,
            "model_version": model_version,
            "rule_versions": dict(rule_versions),
            "horizon_days": horizon,
            "data_as_of": candidate.data_as_of,
            "staleness_days": staleness_days,
            "computable": computable,
            "not_computable_reason": not_computable_reason,
            "candidate_inputs": dict(candidate.formula_inputs),
            "confidence": confidence_result.formula_inputs,
            "probability": (
                probability_result.formula_inputs if probability_result is not None else None
            ),
            "probability_suppressed": confidence_result.probability_suppressed,
            "probability_source": probability_source,
            "predictor_mode": predictor_mode if predictor is not None else None,
            "challenger_probability": (
                str(challenger_probability) if challenger_probability is not None else None
            ),
            "fallback_reason": fallback_reason,
            "feature_snapshot_hash": feature_snapshot.content_hash if feature_snapshot else None,
            "feature_snapshot": (dict(feature_snapshot.values) if feature_snapshot else None),
            "ml_prediction": (
                {
                    "model_version": prediction.model_version,
                    "artifact_checksum": prediction.artifact_checksum,
                    "probability": str(prediction.probability),
                    "contributions": [
                        {"name": item.name, "value": item.value}
                        for item in prediction.contributions
                    ],
                }
                if prediction is not None
                else None
            ),
        }
        if crossing_result is not None:
            formula["crossing"] = {
                "crossing_day": crossing_result.crossing_day,
                "crossing_date": crossing_result.crossing_date,
                "crossing_value": crossing_result.crossing_value,
                "threshold_used": crossing_result.threshold_used,
                "margin": crossing_result.margin,
            }
        safe_formula = cast(dict[str, object], _json_safe(formula))
        row = Forecast(
            id=new_id(),
            run_id=run.id,
            covenant_version_id=candidate.covenant_version_id,
            horizon_days=horizon,
            probability=_quantize(shown_probability, "0.0001"),
            probability_source=probability_source,
            fallback_reason=fallback_reason,
            confidence=_quantize(confidence_result.confidence, "0.0001"),
            below_confidence_floor=confidence_result.below_confidence_floor,
            projected_cross_date=(
                crossing_result.crossing_date if crossing_result is not None else None
            ),
            direction=cast(Direction, candidate.direction).value,
            formula_inputs=safe_formula,
            data_as_of=candidate.data_as_of,
            staleness_days=staleness_days,
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        safe_formula["record_content_hash"] = _forecast_record_hash(row)
        row.formula_inputs = safe_formula
        computations.append(
            _ForecastComputation(
                row=row,
                projection=(
                    projection
                    if projection is not None and horizon == projection.horizon_days
                    else (
                        project(
                            projection.usable_observations,
                            projection.requested_pressure,
                            horizon,
                            candidate.threshold,
                            candidate.direction,
                            recent_periods=candidate.recent_periods,
                            period_days=candidate.period_days,
                        )
                        if projection is not None
                        else None
                    )
                ),
                probability_result=probability_result,
                confidence_result=confidence_result,
                crossing_result=crossing_result,
                computable=computable,
                reason=not_computable_reason,
            )
        )
    return tuple(computations)


def _attribution(
    computation: _ForecastComputation,
    *,
    thresholds: object | None,
    session: Session,
) -> tuple[tuple[DriverShare, ...], dict[str, _DriverMetadata]]:
    """Build normalized driver shares and their safe evidence-link metadata."""

    t5 = _optional_threshold_value(thresholds, "T5", "contribution_share")
    if t5 is None:
        share = DriverShare(_NEUTRAL_DRIVER_NAME, _ONE, _ATTRIBUTION_UNAVAILABLE_REASON)
        return (
            (share,),
            {
                share.name: _DriverMetadata(
                    evidence_id=None,
                    driver_type="neutral",
                    link_status="not_traceable",
                    reason=_ATTRIBUTION_UNAVAILABLE_REASON,
                )
            },
        )

    probability_result = computation.probability_result
    if probability_result is None:
        reason = computation.reason or "forecast has no computable probability"
        share = DriverShare(_NEUTRAL_DRIVER_NAME, _ONE, reason)
        return (
            (share,),
            {
                share.name: _DriverMetadata(
                    evidence_id=None,
                    driver_type="neutral",
                    link_status="not_traceable",
                    reason=reason,
                )
            },
        )

    contributions: dict[str, Decimal] = {}
    metadata: dict[str, _DriverMetadata] = {}
    probability_terms = probability_result.terms_by_name

    _add_driver_contribution(
        contributions,
        metadata,
        "distance",
        probability_terms["distance"].contribution,
        _DriverMetadata(
            evidence_id=None,
            driver_type="distance",
            link_status="not_traceable",
            reason="distance is a forecast term, not an evidence item",
        ),
    )
    _add_driver_contribution(
        contributions,
        metadata,
        "trend",
        probability_terms["velocity"].contribution,
        _DriverMetadata(
            evidence_id=None,
            driver_type="trend",
            link_status="not_traceable",
            reason="trend is computed from the financial observation series",
        ),
    )

    pressure_contribution = probability_terms["pressure"].contribution
    pressure_result = (
        computation.projection.pressure_result if computation.projection is not None else None
    )
    included_pressure_terms = tuple(
        term
        for term in (pressure_result.terms if pressure_result is not None else ())
        if term.included and term.contribution > _ZERO
    )
    pressure_total = sum((term.contribution for term in included_pressure_terms), _ZERO)
    if pressure_contribution > _ZERO and pressure_total > _ZERO:
        for index, term in enumerate(included_pressure_terms):
            evidence_id, link_status, link_reason = _evidence_link(term.evidence_id, session)
            name = _evidence_driver_name(term.evidence_id, index)
            _add_driver_contribution(
                contributions,
                metadata,
                name,
                pressure_contribution * term.contribution / pressure_total,
                _DriverMetadata(
                    evidence_id=evidence_id,
                    driver_type="evidence" if evidence_id is not None else "evidence_unresolved",
                    link_status=link_status,
                    reason=link_reason,
                ),
            )
    elif pressure_contribution > _ZERO:
        _add_driver_contribution(
            contributions,
            metadata,
            "pressure",
            pressure_contribution,
            _DriverMetadata(
                evidence_id=None,
                driver_type="pressure",
                link_status="not_traceable",
                reason="pressure was supplied without an evidence item",
            ),
        )

    for factor in computation.confidence_result.factors:
        degradation = _ONE - factor.value
        if degradation > _ZERO:
            _add_driver_contribution(
                contributions,
                metadata,
                f"data_quality:{factor.name}",
                degradation,
                _DriverMetadata(
                    evidence_id=None,
                    driver_type="data_quality",
                    link_status="not_traceable",
                    reason=factor.description,
                ),
            )

    shares = tuple(attribute(contributions, t5))
    for share in shares:
        if share.name in metadata:
            continue
        if share.name == _OTHER_DRIVER_NAME:
            metadata[share.name] = _DriverMetadata(
                evidence_id=None,
                driver_type="other",
                link_status="not_traceable",
                reason="contributions below T5 were folded into other",
            )
        elif share.name == _NEUTRAL_DRIVER_NAME:
            metadata[share.name] = _DriverMetadata(
                evidence_id=None,
                driver_type="neutral",
                link_status="not_traceable",
                reason=share.reason or "no attributable risk contribution",
            )
        else:
            raise RuntimeError(f"Attribution produced an unknown driver {share.name!r}.")
    return shares, metadata


def _quantized_driver_shares(shares: Sequence[DriverShare]) -> tuple[DriverShare, ...]:
    """Match persisted four-place shares while preserving their total.

    ``forecast_driver.share`` is fixed-point at four decimal places.  Rounding
    each normalized domain share independently could make the database rows
    sum to 0.9999 or 1.0001, so the residual is assigned to ``other`` when it
    exists, otherwise to the largest-magnitude named contribution.
    """

    if not shares:
        raise ValueError("Attribution must produce at least one driver share.")
    quantum = Decimal("0.0001")
    values = [share.share.quantize(quantum) for share in shares]
    residual = _ONE - sum(values, _ZERO)
    if residual != _ZERO:
        adjustment_index = next(
            (index for index, share in enumerate(shares) if share.name == _OTHER_DRIVER_NAME),
            max(range(len(shares)), key=lambda index: abs(values[index])),
        )
        values[adjustment_index] += residual
    return tuple(
        DriverShare(share.name, value, share.reason)
        for share, value in zip(shares, values, strict=True)
    )


def _add_driver_contribution(
    contributions: dict[str, Decimal],
    metadata: dict[str, _DriverMetadata],
    name: str,
    contribution: Decimal,
    driver_metadata: _DriverMetadata,
) -> None:
    if name in contributions:
        contributions[name] += contribution
        if metadata[name] != driver_metadata:
            raise ValueError(f"Attribution metadata differs for duplicate driver {name!r}.")
        return
    contributions[name] = contribution
    metadata[name] = driver_metadata


def _evidence_driver_name(raw_id: object, index: int) -> str:
    parsed = _parse_uuid(raw_id)
    return f"evidence:{parsed}" if parsed is not None else f"pressure_{index + 1}"


def _evidence_link(
    raw_id: object,
    session: Session,
) -> tuple[UUID | None, str, str | None]:
    parsed = _parse_uuid(raw_id)
    if parsed is None:
        return None, "not_traceable", "pressure term does not identify an evidence item"
    if session.get(EvidenceItem, parsed) is None:
        return None, "unresolved", "evidence item does not exist; link was cleared"
    return parsed, "resolved", None


def _parse_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _stage4_trace(
    run: ForecastRun,
    candidate: ForecastCandidate,
    computation: _ForecastComputation,
    forecast_row: Forecast,
    driver_details: Sequence[Mapping[str, object]],
    *,
    thresholds: object | None,
    rule_versions: Mapping[str, object],
) -> TraceRecord:
    """Assemble the complete, portable stage-4 trace value object."""

    projection = computation.projection
    probability_result = computation.probability_result
    confidence_result = computation.confidence_result
    formula = forecast_row.formula_inputs or {}
    probability_source = formula.get("probability_source", "deterministic")
    ml_prediction = formula.get("ml_prediction")
    comparisons: list[Mapping[str, object]] = []
    if probability_result is not None:
        t1 = _optional_threshold_value(thresholds, "T1", "act")
        if t1 is not None:
            observed_probability = (
                forecast_row.probability
                if forecast_row.probability is not None
                else probability_result.probability
            )
            comparisons.append(_threshold_comparison("T1", t1, observed_probability))
    t2 = _optional_threshold_value(thresholds, "T2", "confidence_floor")
    if t2 is not None:
        observed_confidence = (
            forecast_row.confidence
            if forecast_row.confidence is not None
            else confidence_result.confidence
        )
        comparisons.append(_threshold_comparison("T2", t2, observed_confidence))

    pressure_terms = _pressure_trace_terms(projection)
    confidence_factors = [
        {
            "name": factor.name,
            "value": factor.value,
            "description": factor.description,
        }
        for factor in confidence_result.factors
    ]
    mapping_weights = (
        probability_result.weights.as_mapping() if probability_result is not None else {}
    )
    reason = computation.reason
    if reason is None and confidence_result.probability_suppressed:
        reason = confidence_result.reason
    inputs: dict[str, object] = {
        "forecast_id": forecast_row.id,
        "run_id": run.id,
        "covenant_version_id": candidate.covenant_version_id,
        "threshold_snapshot_id": run.threshold_snapshot_id,
        "horizon_days": forecast_row.horizon_days,
        "threshold": candidate.threshold,
        "direction": cast(Direction, candidate.direction).value,
        "data_as_of": candidate.data_as_of,
        "staleness_days": forecast_row.staleness_days,
        "current_value": projection.current_value if projection is not None else None,
        "slope": projection.slope if projection is not None else None,
        "per_day_drift": projection.per_day_drift if projection is not None else None,
        "pressure": projection.pressure if projection is not None else None,
        "pressure_term": projection.pressure_term if projection is not None else None,
        "net_per_day_drift": projection.net_per_day_drift if projection is not None else None,
        "pressure_terms": pressure_terms,
        "mapping_weights": mapping_weights,
        "probability_terms": (
            probability_result.formula_inputs if probability_result is not None else None
        ),
        "confidence_factors": confidence_factors,
        "confidence_formula": confidence_result.formula_inputs,
        "rule_versions": dict(rule_versions),
        "attribution_rule_version": _FORECAST_TRACE_RULE_VERSION,
        "probability_source": probability_source,
        "feature_snapshot_hash": formula.get("feature_snapshot_hash"),
        "feature_snapshot": formula.get("feature_snapshot"),
        "ml_prediction": ml_prediction,
    }
    outputs: dict[str, object] = {
        "forecast_id": forecast_row.id,
        "horizon_days": forecast_row.horizon_days,
        "probability": forecast_row.probability,
        "computed_probability": (
            probability_result.probability if probability_result is not None else None
        ),
        "probability_suppressed": confidence_result.probability_suppressed,
        "confidence": confidence_result.confidence,
        "below_confidence_floor": confidence_result.below_confidence_floor,
        "projected_cross_date": forecast_row.projected_cross_date,
        "reason": reason,
        "attribution_method": "normalized_probability_contributions",
        "attribution_rule_version": _FORECAST_TRACE_RULE_VERSION,
        "drivers": list(driver_details),
        "formula_inputs": forecast_row.formula_inputs,
        "probability_source": probability_source,
        # The champion/challenger pair, side by side, so `Why?` shows what the
        # model said as well as what the decision used — and shows that the
        # decision did not come from the model unless it was promoted.
        "predictor_mode": formula.get("predictor_mode"),
        "challenger_probability": formula.get("challenger_probability"),
        "fallback_reason": formula.get("fallback_reason"),
        "ml_drivers": (
            (ml_prediction or {}).get("contributions", [])
            if isinstance(ml_prediction, Mapping)
            else []
        ),
    }
    sources: list[object] = [
        {"type": "forecast", "id": forecast_row.id},
        {"type": "forecast_run", "id": run.id},
        {"type": "covenant_version", "id": candidate.covenant_version_id},
    ]
    seen_sources = {str(candidate.covenant_version_id)}
    for driver in driver_details:
        evidence_id = driver.get("evidence_id")
        if evidence_id is None or str(evidence_id) in seen_sources:
            continue
        sources.append({"type": "evidence_item", "id": evidence_id})
        seen_sources.add(str(evidence_id))
    return stage_record(
        4,
        "statistical" if probability_source == "ml" else "code",
        inputs,
        outputs,
        (
            str((ml_prediction or {}).get("model_version"))
            if probability_source == "ml" and isinstance(ml_prediction, Mapping)
            else _FORECAST_TRACE_RULE_VERSION
        ),
        comparisons,
        confidence_result.confidence,
        sources,
    )


def _pressure_trace_terms(projection: Projection | None) -> list[dict[str, object]]:
    if projection is None:
        return []
    return [
        {
            "evidence_id": term.evidence_id,
            "materiality": term.materiality,
            "decay_factor": term.decay_factor,
            "contribution": term.contribution,
            "signed_contribution": term.signed_contribution,
            "included": term.included,
            "reason": term.reason,
        }
        for term in projection.pressure_terms
    ]


def _threshold_comparison(name: str, threshold: Decimal, observed: Decimal) -> Mapping[str, object]:
    if observed == threshold:
        side = "at"
    elif observed > threshold:
        side = "above"
    else:
        side = "below"
    return {"name": name, "value": threshold, "observed": observed, "side": side}


def _optional_threshold_value(store: object | None, name: str, field_name: str) -> Decimal | None:
    if store is None:
        return None
    section: object | None = None
    if isinstance(store, Mapping):
        section = store.get(name)
    else:
        getter = getattr(store, "get", None)
        if callable(getter):
            try:
                section = getter(name)
            except (KeyError, TypeError):
                section = None
    if section is None:
        section = getattr(store, name, None)
    if section is None:
        return None
    if isinstance(section, Mapping):
        if field_name not in section:
            return None
        value = section[field_name]
    else:
        marker = object()
        value = getattr(section, field_name, marker)
        if value is marker:
            return None
    return _decimal(value, f"{name}.{field_name}")


def _same_trace(current: object, record: TraceRecord) -> bool:
    """Compare persisted trace facts while ignoring generated row metadata."""

    return (
        getattr(current, "stage", None) == str(record.stage)
        and getattr(current, "decider", None) == record.decider
        and getattr(current, "inputs", None) == dict(record.inputs)
        and getattr(current, "outputs", None) == dict(record.outputs)
        and getattr(current, "rule_or_prompt_version", None) == record.rule_or_prompt_version
        and getattr(current, "thresholds_compared", None)
        == [dict(item) for item in record.thresholds_compared]
        and _quantize(getattr(current, "confidence", None), "0.0001")
        == _quantize(record.confidence, "0.0001")
        and getattr(current, "sources", None) == list(record.sources)
    )


def _confidence_result(
    candidate: ForecastCandidate,
    staleness_days: int | None,
    thresholds: object | None,
    *,
    computable: bool,
) -> ConfidenceResult:
    if not computable:
        return confidence(_ZERO, _ZERO, staleness_days or 0, thresholds)
    if staleness_days is None:
        return confidence(_ZERO, _ZERO, 0, thresholds)
    return confidence(
        candidate.completeness,
        candidate.evidence_support,
        staleness_days,
        thresholds,
    )


def _candidate_computability(candidate: ForecastCandidate) -> tuple[bool, str | None]:
    if not candidate.computable:
        return False, candidate.not_computable_reason or "forecast was marked not computable"
    return True, candidate.not_computable_reason


def _distance_to_boundary(value: Decimal, threshold: Decimal, direction: Direction) -> Decimal:
    if direction is Direction.MAX:
        return max(_ZERO, threshold - value)
    return max(_ZERO, value - threshold)


def _run_content_hash(
    run: ForecastRun,
    forecasts: Sequence[Forecast],
    paths: Sequence[ForecastPath],
) -> str:
    payload = {
        "run_id": run.id,
        "as_of_date": run.as_of_date,
        "threshold_snapshot_id": run.threshold_snapshot_id,
        "model_version": run.model_version,
        "covenant_count": run.covenant_count,
        "state": run.state,
        "forecasts": [
            {
                "covenant_version_id": row.covenant_version_id,
                "horizon_days": row.horizon_days,
                "probability": row.probability,
                "probability_source": row.probability_source,
                "fallback_reason": row.fallback_reason,
                "confidence": row.confidence,
                "below_confidence_floor": row.below_confidence_floor,
                "projected_cross_date": row.projected_cross_date,
                "direction": row.direction,
                "formula_inputs": row.formula_inputs,
                "data_as_of": row.data_as_of,
                "staleness_days": row.staleness_days,
            }
            for row in forecasts
        ],
        "paths": [
            {
                "covenant_version_id": row.covenant_version_id,
                "day_offset": row.day_offset,
                "projected_value": row.projected_value,
                "headroom_pct": row.headroom_pct,
            }
            for row in paths
        ],
    }
    encoded = json.dumps(
        _json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _forecast_record_hash(row: Forecast) -> str:
    payload = {
        "run_id": row.run_id,
        "covenant_version_id": row.covenant_version_id,
        "horizon_days": row.horizon_days,
        "probability": row.probability,
        "probability_source": row.probability_source,
        "fallback_reason": row.fallback_reason,
        "confidence": row.confidence,
        "below_confidence_floor": row.below_confidence_floor,
        "projected_cross_date": row.projected_cross_date,
        "direction": row.direction,
        "formula_inputs": row.formula_inputs,
        "data_as_of": row.data_as_of,
        "staleness_days": row.staleness_days,
    }
    encoded = json.dumps(
        _json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _horizons_from_configuration(
    horizons: Sequence[int] | None,
    configuration: object | None,
) -> tuple[int, ...]:
    configured = horizons
    if configured is None and configuration is not None:
        configured = cast(Sequence[int] | None, _configuration_value(configuration, "horizons"))
        if configured is None:
            configured = cast(
                Sequence[int] | None,
                _configuration_value(configuration, "forecast_horizons"),
            )
    if configured is None:
        raise ValidationError(
            "Forecast horizons must be supplied by configuration; no code default exists.",
            field="horizons",
        )
    if isinstance(configured, str) or not isinstance(configured, Sequence):
        raise TypeError("Forecast horizons must be a sequence of integers.")
    normalised: set[int] = set()
    for value in configured:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Forecast horizons must contain non-negative integers.")
        if value > _MAX_HORIZON_DAYS:
            raise ValueError(f"Forecast horizon cannot exceed {_MAX_HORIZON_DAYS} days.")
        normalised.add(value)
    if not normalised:
        raise ValueError("Forecast horizons must contain at least one value.")
    return tuple(sorted(normalised))


def _resolve_weights(
    supplied: Weights | Mapping[str, object] | None,
    configuration: object | None,
    service_weights: Weights | None,
) -> Weights | None:
    if supplied is not None:
        return _normalise_weights(supplied)
    if service_weights is not None:
        return service_weights
    if configuration is not None:
        section = _configuration_value(configuration, "probability")
        if isinstance(section, Mapping):
            return Weights.from_mapping(cast(Mapping[str, object], section))
    return None


def _candidate_weights(candidate: ForecastCandidate) -> Weights | None:
    if candidate.probability_weights is None:
        return None
    return _normalise_weights(candidate.probability_weights)


def _normalise_weights(value: Weights | Mapping[str, object]) -> Weights:
    return value if isinstance(value, Weights) else Weights.from_mapping(value)


def _snapshot_id(thresholds: object | None) -> UUID | None:
    if thresholds is None:
        return None
    getter = getattr(thresholds, "snapshot_id", None)
    if not callable(getter):
        return None
    value = getter()
    return value if isinstance(value, UUID) else None


def _normalise_rule_versions(
    value: str | Mapping[str, object] | None,
    model_version: str,
) -> Mapping[str, object]:
    if value is None:
        return {"scoring": model_version}
    if isinstance(value, str):
        _bounded_text(value, "rule_versions", _MODEL_VERSION_MAX_LENGTH)
        return {"scoring": value}
    if not isinstance(value, Mapping) or not value:
        raise TypeError("rule_versions must be non-empty text or a mapping.")
    result = {str(key): _json_safe(item) for key, item in value.items()}
    if any(not key.strip() for key in result):
        raise ValueError("rule_versions keys must not be blank.")
    return result


def _validate_resume_metadata(
    run: ForecastRun,
    *,
    as_of_date: date,
    threshold_snapshot_id: UUID,
    model_version: str,
    candidate_count: int,
) -> None:
    if run.as_of_date != as_of_date:
        raise Conflict("A forecast run cannot be resumed for a different as_of_date.")
    if run.threshold_snapshot_id != threshold_snapshot_id:
        raise Conflict("A forecast run cannot be resumed with a different threshold snapshot.")
    if run.model_version != model_version:
        raise Conflict("A forecast run cannot be resumed with a different model version.")
    if run.covenant_count is None or candidate_count > run.covenant_count:
        raise Conflict("A forecast run cannot be resumed with more covenants than it planned.")
    if run.state not in {RUNNING, INCOMPLETE, COMPLETE}:
        raise ValidationError(f"Unknown forecast run state {run.state!r}.", field="state")


def _normalise_candidates(
    values: Iterable[ForecastCandidate | Mapping[str, object] | object],
) -> tuple[ForecastCandidate, ...]:
    if isinstance(values, Mapping) or isinstance(values, ForecastCandidate):
        return (ForecastCandidate.from_value(values),)
    try:
        iterator = iter(values)
    except TypeError as error:
        raise TypeError("candidates must be an iterable of forecast candidates.") from error
    return tuple(ForecastCandidate.from_value(value) for value in iterator)


def _unique_candidate_ids(values: Sequence[ForecastCandidate]) -> None:
    ids = [candidate.covenant_version_id for candidate in values]
    if len(ids) != len(set(ids)):
        raise ValidationError("Each covenant_version_id may occur only once per scoring pass.")


def _staleness(data_as_of: date | None, scoring_date: date) -> int | None:
    if data_as_of is None:
        return None
    normalized = _calendar_date(data_as_of, "data_as_of")
    if normalized > scoring_date:
        raise ValidationError("data_as_of cannot be after as_of_date.", field="data_as_of")
    return (scoring_date - normalized).days


def _configuration_value(configuration: object, name: str) -> object | None:
    if isinstance(configuration, Mapping):
        direct = cast(object | None, configuration.get(name))
        if direct is not None:
            return direct
        for section_name in ("forecast", "scoring"):
            section = cast(object | None, configuration.get(section_name))
            if isinstance(section, Mapping) and name in section:
                return cast(object, section[name])
        return None
    value = cast(object | None, getattr(configuration, name, None))
    if value is not None:
        return value
    getter = getattr(configuration, "get", None)
    if callable(getter):
        try:
            return cast(object | None, getter(name))
        except (KeyError, TypeError):
            return None
    return None


def _read_any(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return default


def _coalesce_uuid(first: UUID | None, second: UUID | None, field_name: str) -> UUID | None:
    if first is not None and second is not None and first != second:
        raise ValueError(f"{field_name} and its alias identify different runs.")
    value = first or second
    if value is not None and not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID or None.")
    return value


def _coalesce_int(first: int | None, second: int | None, field_name: str) -> int | None:
    if first is not None and second is not None and first != second:
        raise ValueError(f"{field_name} and its alias identify different values.")
    value = first if first is not None else second
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{field_name} must be an integer or None.")
    return value


def _calendar_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field_name} must be a calendar date.")
    return value


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field_name} must be a finite Decimal.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite Decimal.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal.")
    return result


def _fraction(value: object, field_name: str) -> Decimal:
    result = _decimal(value, field_name)
    if not _ZERO <= result <= _ONE:
        raise ValueError(f"{field_name} must be between zero and one inclusive.")
    return result


def _positive_decimal_or_int(value: object, field_name: str) -> None:
    result = _decimal(value, field_name)
    if result <= _ZERO:
        raise ValueError(f"{field_name} must be positive.")


def _quantize(value: Decimal | None, quantum: str) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal(quantum))


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} must be non-empty text of at most {maximum} characters.")
    return value


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Forecast formula inputs contain unsupported value {type(value).__name__}.")


__all__ = [
    "AuditWriter",
    "ForecastCandidate",
    "ForecastScoringService",
    "ScoringResult",
]
