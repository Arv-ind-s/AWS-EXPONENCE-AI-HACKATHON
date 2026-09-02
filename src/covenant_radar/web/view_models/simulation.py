"""Read model for the intervention simulator screen (T-079).

The simulator screen is deliberately a presentation adapter around the
deterministic simulation service.  It never derives a risk number itself.  A
forecast is selected through a scoped query, its stored path and formula
inputs are reconstructed into the domain ``Projection`` shape, and the
service remains the only component that calculates a counterfactual.

Older forecast rows do not all contain a serialized observation series.  For
those rows the persisted daily path is used to recover the two scalar terms
that the domain projection needs (trend and evidence pressure).  The result
is still sent through :func:`project` and is rejected when the stored facts
are insufficient; a screen must never make up a counterfactual from a
displayed crossing date alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Literal, cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.core.errors import NotFound, ValidationError
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastPath,
    ForecastRun,
)
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope, ownership_path_for
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.forecast import Observation, Projection, project
from covenant_radar.domain.interventions.catalogue import CatalogueEntry
from covenant_radar.domain.interventions.simulate import SimulationComparison, SimulationResult
from covenant_radar.i18n.formatting import format_ist_date

_ZERO: Final[Decimal] = Decimal("0")
_ONE_HUNDRED: Final[Decimal] = Decimal("100")
_PERCENT_QUANTUM: Final[Decimal] = Decimal("0.01")
_MAX_PARAMETERS_JSON_LENGTH: Final[int] = 16 * 1024

SimulationScreenState = Literal["ready", "empty", "error", "degraded"]


class ComparisonStatus(StrEnum):
    """Presentation status for a valid baseline or simulated option."""

    BASELINE = "baseline"
    APPLIED = "applied"
    NO_EFFECT = "no_effect"


@dataclass(frozen=True, slots=True)
class SimulationForecastView:
    """The scoped forecast and covenant facts shown above the comparison."""

    id: UUID
    borrower_reference: str
    borrower_name: str
    facility_reference: str
    covenant_reference: str
    covenant_name: str
    covenant_class: str
    horizon_days: int
    as_of_date: date | None
    threshold: Decimal
    threshold_display: str
    direction: str
    probability_display: str
    confidence_display: str
    crossing_display: str


@dataclass(frozen=True, slots=True)
class EffectParameterView:
    """One immutable catalogue parameter rendered in an intervention card."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class InterventionOptionView:
    """One applicable catalogue option offered by the simulator."""

    id: UUID | None
    code: str
    text: str
    role_tag: str
    effect_model: str
    effect_parameters: tuple[EffectParameterView, ...]
    assumptions: tuple[str, ...]
    requires_approval: bool
    selected: bool
    retired: bool = False


@dataclass(frozen=True, slots=True)
class ComparisonHorizonView:
    """One option's result at one persisted forecast horizon."""

    horizon_days: int
    crossing_display: str
    probability_display: str
    delta_days_display: str
    delta_probability_display: str
    crossing_date: date | None
    probability: Decimal | None
    delta_days: int | None
    delta_probability: Decimal | None


@dataclass(frozen=True, slots=True)
class ComparisonColumnView:
    """A baseline or option column in the comparison table."""

    code: str
    label: str
    description: str
    status: ComparisonStatus
    assumptions: tuple[str, ...]
    horizons: tuple[ComparisonHorizonView, ...]
    simulation_ids: tuple[UUID, ...] = ()
    no_effect_reason: str | None = None

    @property
    def is_baseline(self) -> bool:
        """Whether this column is the mandatory do-nothing reference."""

        return self.status is ComparisonStatus.BASELINE

    @property
    def no_effect(self) -> bool:
        """Whether the valid option produced no observable change."""

        return self.status is ComparisonStatus.NO_EFFECT


@dataclass(frozen=True, slots=True)
class ComparisonView:
    """Comparison shape with a structural baseline-first invariant."""

    baseline: ComparisonColumnView
    options: tuple[ComparisonColumnView, ...] = ()

    def __post_init__(self) -> None:
        if not self.baseline.is_baseline:
            raise ValueError("A simulation comparison must start with the baseline column.")
        options = tuple(self.options)
        if any(option.is_baseline for option in options):
            raise ValueError("The baseline may not be repeated as an option.")
        codes = tuple(option.code for option in options)
        if len(codes) != len(set(codes)):
            raise ValueError("An intervention may occur only once in a comparison.")
        object.__setattr__(self, "options", options)

    @property
    def columns(self) -> tuple[ComparisonColumnView, ...]:
        """Return the exact render order: baseline first, options second."""

        return (self.baseline, *self.options)

    @property
    def results(self) -> tuple[ComparisonColumnView, ...]:
        """Compatibility alias used by callers that call them results."""

        return self.columns


@dataclass(frozen=True, slots=True)
class SimulationScreenView:
    """Complete server-rendered simulator page."""

    state: SimulationScreenState
    forecast: SimulationForecastView | None
    interventions: tuple[InterventionOptionView, ...]
    comparison: ComparisonView
    selected_codes: tuple[str, ...] = ()
    parameters_json: str = "{}"
    memo_href: str | None = None
    # Posted as repeated ``simulation_ids`` fields alongside ``borrower_ref``
    # so the memo cites exactly the options compared on this screen (`C-08`).
    memo_simulation_ids: tuple[str, ...] = ()
    memo_borrower_ref: str = ""
    error_message: str | None = None
    empty_title: str = "Select an intervention"
    empty_message: str = "Select an intervention to compare against doing nothing."
    # An empty state that names no next step is indistinguishable from a
    # screen that failed to load, so it carries one.  Both are blank when
    # the recovery is already on the screen (the intervention checkboxes).
    empty_action_label: str = ""
    empty_action_href: str = ""

    def __post_init__(self) -> None:
        if self.state not in {"ready", "empty", "error", "degraded"}:
            raise ValueError(f"Unsupported simulator state: {self.state!r}.")
        object.__setattr__(self, "interventions", tuple(self.interventions))
        object.__setattr__(self, "selected_codes", tuple(self.selected_codes))
        object.__setattr__(self, "memo_simulation_ids", tuple(self.memo_simulation_ids))
        if len(self.parameters_json) > _MAX_PARAMETERS_JSON_LENGTH:
            raise ValueError("Simulator parameter JSON is too large.")

    @property
    def baseline(self) -> ComparisonColumnView:
        """Expose the mandatory baseline without requiring template knowledge."""

        return self.comparison.baseline

    @property
    def options(self) -> tuple[ComparisonColumnView, ...]:
        """Expose simulated options without exposing comparison internals."""

        return self.comparison.options


@dataclass(frozen=True, slots=True)
class SimulationContext:
    """Scoped persistence facts needed to run one or more horizons."""

    forecast: Forecast
    forecasts: tuple[Forecast, ...]
    paths: tuple[ForecastPath, ...]
    run: ForecastRun
    covenant_version: CovenantVersion
    covenant: Covenant
    facility: Facility
    borrower: Borrower
    entries: tuple[CatalogueEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.forecast, Forecast):
            raise TypeError("SimulationContext forecast must be a Forecast row.")
        if not self.forecasts:
            raise ValueError("SimulationContext requires at least one forecast row.")
        if self.forecast.id not in {row.id for row in self.forecasts}:
            raise ValueError("The selected forecast must be part of the forecast set.")
        if self.run.state != "complete":
            raise ValueError("SimulationContext requires a completed forecast run.")
        object.__setattr__(self, "forecasts", tuple(self.forecasts))
        object.__setattr__(self, "paths", tuple(self.paths))
        object.__setattr__(self, "entries", tuple(self.entries))


def load_simulation_context(
    session: Session,
    forecast_id: UUID,
    *,
    scope: Scope,
    entries: Sequence[CatalogueEntry] = (),
) -> SimulationContext:
    """Load a forecast and all same-covenant horizons through one scope."""

    if not is_database_session(session):
        raise TypeError("load_simulation_context requires a SQLAlchemy Session.")
    if not isinstance(forecast_id, UUID):
        raise TypeError("forecast_id must be a UUID.")
    if not isinstance(scope, Scope):
        raise TypeError("load_simulation_context requires a portfolio Scope.")

    statement: Select[
        tuple[Forecast, CovenantVersion, Covenant, Facility, Borrower, Portfolio, ForecastRun]
    ] = (
        select(Forecast, CovenantVersion, Covenant, Facility, Borrower, Portfolio, ForecastRun)
        .select_from(Forecast)
        .join(CovenantVersion, CovenantVersion.id == Forecast.covenant_version_id)
        .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
        .join(Facility, Facility.id == Covenant.facility_id)
        .join(Borrower, Borrower.id == Facility.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .join(ForecastRun, ForecastRun.id == Forecast.run_id)
        .where(Forecast.id == forecast_id, scope.predicate(Portfolio.path))
    )
    selected = session.execute(statement).one_or_none()
    if selected is None:
        raise NotFound("The forecast was not found within the current scope.")
    forecast, version, covenant, facility, borrower, _portfolio, run = selected
    if run.state != "complete":
        raise NotFound("The selected forecast is not from a completed forecast run.")

    forecast_statement = _scoped_forecast_select(scope).where(
        Forecast.run_id == run.id,
        Forecast.covenant_version_id == version.id,
    )
    forecasts = tuple(
        session.execute(forecast_statement.order_by(Forecast.horizon_days, Forecast.id))
        .scalars()
        .all()
    )
    path_statement = _scoped_path_select(session, scope).where(
        ForecastPath.run_id == run.id,
        ForecastPath.covenant_version_id == version.id,
    )
    paths = tuple(session.execute(path_statement.order_by(ForecastPath.day_offset)).scalars().all())
    return SimulationContext(
        forecast=forecast,
        forecasts=forecasts,
        paths=paths,
        run=run,
        covenant_version=version,
        covenant=covenant,
        facility=facility,
        borrower=borrower,
        entries=tuple(entries),
    )


def build_simulation_projection(
    context: SimulationContext,
    forecast: Forecast,
) -> Projection:
    """Reconstruct the domain projection from persisted forecast facts.

    The scorer stores candidate inputs when they are available.  If an older
    run only has a path, the path is authoritative for the observed net drift;
    pressure is recovered from the persisted probability formula and the
    remaining drift is the trend term.  No forecast date or probability is
    reverse-engineered into an input.
    """

    if not isinstance(context, SimulationContext):
        raise TypeError("context must be a SimulationContext.")
    if not isinstance(forecast, Forecast):
        raise TypeError("forecast must be a Forecast row.")
    if forecast.covenant_version_id != context.covenant_version.id:
        raise ValueError("forecast does not belong to the simulation context covenant.")

    formula = _mapping(forecast.formula_inputs)
    candidate = _mapping(formula.get("candidate_inputs"))
    threshold = context.covenant_version.threshold
    direction = forecast.direction or context.covenant_version.direction
    horizon = forecast.horizon_days
    pressure = _first_decimal(
        candidate,
        ("pressure", "requested_pressure", "evidence_pressure"),
    )
    if pressure is None:
        probability_inputs = _mapping(formula.get("probability"))
        pressure = _first_decimal(probability_inputs, ("pressure",))
    if pressure is None:
        pressure = _first_decimal(formula, ("requested_pressure", "pressure"))
    if pressure is None:
        # The original scorer permits a zero pressure candidate.  When an
        # older row did not retain either candidate or probability formula
        # inputs, zero is the only defensible value; the stored path remains
        # the source for the net drift below.
        pressure = _ZERO
    if pressure < _ZERO:
        raise ValueError("Persisted forecast pressure cannot be negative.")

    candidate_series = _candidate_series(candidate)
    if candidate_series:
        return project(
            candidate_series,
            pressure,
            horizon,
            threshold,
            direction,
            recent_periods=_optional_int(candidate, ("recent_periods",)),
            period_days=_optional_positive_decimal(
                candidate, ("period_days", "period_length_days")
            ),
        )

    rows = tuple(
        row
        for row in context.paths
        if row.run_id == forecast.run_id
        and row.covenant_version_id == forecast.covenant_version_id
        and row.day_offset <= horizon
        and row.projected_value is not None
    )
    if not rows:
        raise ValidationError(
            "The selected forecast has no stored projection path to simulate.",
            field="forecast_path",
        )
    if len(rows) == 1:
        as_of = forecast.data_as_of or context.run.as_of_date
        return project(
            (Observation(observed_on=as_of, value=rows[0].projected_value),),
            pressure,
            horizon,
            threshold,
            direction,
        )

    first = rows[0]
    last = rows[-1]
    day_gap = last.day_offset - first.day_offset
    if day_gap <= 0:
        raise ValidationError(
            "The selected forecast path does not contain distinct days.",
            field="forecast_path",
        )
    assert first.projected_value is not None
    assert last.projected_value is not None
    net_drift = (last.projected_value - first.projected_value) / Decimal(day_gap)
    signed_pressure = pressure if direction == "max" else -pressure
    trend_drift = net_drift - signed_pressure
    as_of = forecast.data_as_of or context.run.as_of_date
    previous_date = as_of - timedelta(days=day_gap)
    previous_value = first.projected_value - trend_drift * Decimal(day_gap)
    return project(
        (
            Observation(observed_on=previous_date, value=previous_value),
            Observation(observed_on=as_of, value=first.projected_value),
        ),
        pressure,
        horizon,
        threshold,
        direction,
        period_days=day_gap,
    )


def build_simulation_view(
    context: SimulationContext | None,
    *,
    selected_codes: Sequence[str] = (),
    comparisons: Mapping[UUID, SimulationComparison] | None = None,
    parameters: Mapping[str, object] | None = None,
    simulation_ids: Mapping[tuple[UUID, str], UUID] | None = None,
    error_message: str | None = None,
) -> SimulationScreenView:
    """Shape catalogue entries and domain results into the screen contract."""

    selected = _normalise_codes(selected_codes)
    safe_parameters = dict(parameters or {})
    parameters_json = _parameters_json(safe_parameters)
    if context is None:
        # No forecast was resolved, so there is nothing on this screen to
        # select. The default copy ("Select an intervention") described a
        # control that is not rendered in this branch; say what is actually
        # missing and where the reader gets it.
        return SimulationScreenView(
            state="error" if error_message else "empty",
            forecast=None,
            interventions=(),
            comparison=ComparisonView(_baseline_column(())),
            selected_codes=selected,
            parameters_json=parameters_json,
            error_message=error_message,
            empty_title="No forecast selected",
            empty_message=(
                "The simulator compares interventions against one covenant forecast. "
                "Open a borrower from the queue and start a simulation from the "
                "covenant you want to test."
            ),
            empty_action_label="Open the queue",
            empty_action_href="/",
        )

    comparison_map = comparisons or {}
    result_ids = simulation_ids or {}
    forecast_view = _forecast_view(context)
    option_views = tuple(
        _intervention_view(entry, entry.code in selected) for entry in context.entries
    )
    baseline_horizons = _baseline_horizons(context, comparison_map)
    baseline = _baseline_column(baseline_horizons)
    options: list[ComparisonColumnView] = []
    for code in selected:
        entry = next((candidate for candidate in context.entries if candidate.code == code), None)
        if entry is None:
            continue
        result_by_forecast: dict[UUID, SimulationResult] = {}
        for forecast_id, comparison in comparison_map.items():
            values = comparison.options
            result = next((item for item in values if item.intervention_code == code), None)
            if result is not None:
                result_by_forecast[forecast_id] = result
        horizons = tuple(
            _simulation_horizon_view(result_by_forecast[forecast_row.id])
            for forecast_row in context.forecasts
            if forecast_row.id in result_by_forecast
        )
        if not horizons:
            continue
        first_result = next(iter(result_by_forecast.values()))
        is_no_effect = all(result.no_effect for result in result_by_forecast.values())
        options.append(
            ComparisonColumnView(
                code=entry.code,
                label=entry.code,
                description=entry.text,
                status=(ComparisonStatus.NO_EFFECT if is_no_effect else ComparisonStatus.APPLIED),
                assumptions=first_result.assumptions,
                horizons=horizons,
                simulation_ids=tuple(
                    result_ids[(forecast_row.id, entry.code)]
                    for forecast_row in context.forecasts
                    if (forecast_row.id, entry.code) in result_ids
                ),
                no_effect_reason=(
                    "Zero observable effect for this forecast; the option is valid and applicable."
                    if is_no_effect
                    else None
                ),
            )
        )
    memo_ids = tuple(simulation_id for option in options for simulation_id in option.simulation_ids)
    # `/memos` drafts a memo rather than serving one, so this is a form target
    # rather than a link; the ids travel as fields beside the borrower.
    memo_href = "/memos" if memo_ids else None
    return SimulationScreenView(
        state="error" if error_message else "ready",
        forecast=forecast_view,
        interventions=option_views,
        comparison=ComparisonView(baseline, tuple(options)),
        selected_codes=selected,
        parameters_json=parameters_json,
        memo_href=memo_href,
        memo_simulation_ids=tuple(str(simulation_id) for simulation_id in memo_ids),
        memo_borrower_ref=context.borrower.reference,
        error_message=error_message,
    )


# The shorter name is useful to route callers and mirrors the task title.
build_simulator_view = build_simulation_view


def _scoped_forecast_select(scope: Scope) -> Select[tuple[Forecast]]:
    ownership = ownership_path_for(Forecast)
    statement: Select[tuple[Forecast]] = cast(
        Select[tuple[Forecast]], ownership.apply(select(Forecast))
    )
    return statement.where(scope.predicate(ownership.path_column))


def _scoped_path_select(session: Session, scope: Scope) -> Select[tuple[ForecastPath]]:
    ownership = ownership_path_for(ForecastPath)
    statement: Select[tuple[ForecastPath]] = cast(
        Select[tuple[ForecastPath]], ownership.apply(select(ForecastPath))
    )
    return statement.where(scope.predicate(ownership.path_column))


def _forecast_view(context: SimulationContext) -> SimulationForecastView:
    row = context.forecast
    probability = _probability_display(row.probability)
    confidence = _probability_display(row.confidence)
    crossing = (
        format_ist_date(row.projected_cross_date)
        if row.projected_cross_date is not None
        else f"No projected crossing in {row.horizon_days} days."
    )
    return SimulationForecastView(
        id=row.id,
        borrower_reference=context.borrower.reference,
        borrower_name=context.borrower.legal_name,
        facility_reference=context.facility.reference,
        covenant_reference=context.covenant.reference,
        covenant_name=context.covenant.name,
        covenant_class=context.covenant.covenant_class,
        horizon_days=row.horizon_days,
        as_of_date=row.data_as_of or context.run.as_of_date,
        threshold=context.covenant_version.threshold,
        threshold_display=_number_with_unit(
            context.covenant_version.threshold, context.covenant_version.unit
        ),
        direction=row.direction or context.covenant_version.direction,
        probability_display=probability,
        confidence_display=confidence,
        crossing_display=crossing,
    )


def _number_with_unit(value: Decimal, unit: str) -> str:
    """Strip a stored ratio's excess decimal scale for display (e.g. 3.00000000x -> 3x)."""

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{rendered}{unit}"


def _intervention_view(entry: CatalogueEntry, selected: bool) -> InterventionOptionView:
    return InterventionOptionView(
        id=entry.database_id,
        code=entry.code,
        text=entry.text,
        role_tag=entry.role_tag.value,
        effect_model=entry.effect_model.value,
        effect_parameters=tuple(
            EffectParameterView(name=str(name), value=_display_value(value))
            for name, value in sorted(
                entry.effect_parameters.items(), key=lambda item: str(item[0])
            )
        ),
        assumptions=entry.assumptions,
        requires_approval=entry.requires_approval,
        selected=selected,
        retired=entry.is_retired,
    )


def _baseline_horizons(
    context: SimulationContext,
    comparisons: Mapping[UUID, SimulationComparison],
) -> tuple[ComparisonHorizonView, ...]:
    views: list[ComparisonHorizonView] = []
    for row in context.forecasts:
        comparison = comparisons.get(row.id)
        baseline = comparison.baseline if comparison is not None else None
        views.append(
            ComparisonHorizonView(
                horizon_days=row.horizon_days,
                crossing_display=_crossing_display(
                    baseline.crossing_date if baseline is not None else row.projected_cross_date,
                    row.horizon_days,
                ),
                probability_display=_probability_display(
                    baseline.probability if baseline is not None else row.probability
                ),
                delta_days_display="Not applicable",
                delta_probability_display="Not applicable",
                crossing_date=(
                    baseline.crossing_date if baseline is not None else row.projected_cross_date
                ),
                probability=baseline.probability if baseline is not None else row.probability,
                delta_days=None,
                delta_probability=None,
            )
        )
    return tuple(views)


def _baseline_column(horizons: Sequence[ComparisonHorizonView]) -> ComparisonColumnView:
    return ComparisonColumnView(
        code="baseline",
        label="Do nothing (baseline)",
        description="The stored forecast with no intervention applied.",
        status=ComparisonStatus.BASELINE,
        assumptions=("No intervention is applied; this is the comparison baseline.",),
        horizons=tuple(horizons),
    )


def _simulation_horizon_view(result: SimulationResult) -> ComparisonHorizonView:
    raw_horizon = result.parameters.get("horizon_days")
    horizon_days = (
        raw_horizon
        if isinstance(raw_horizon, int) and not isinstance(raw_horizon, bool)
        else result.projection.horizon_days
    )
    return ComparisonHorizonView(
        horizon_days=horizon_days,
        crossing_display=_crossing_display(result.crossing_date, result.projection.horizon_days),
        probability_display=_probability_display(result.probability),
        delta_days_display=_delta_days_display(result.delta_days, result.delta_days_qualifier),
        delta_probability_display=_delta_probability_display(result.delta_probability),
        crossing_date=result.crossing_date,
        probability=result.probability,
        delta_days=result.delta_days,
        delta_probability=result.delta_probability,
    )


def _crossing_display(crossing_date: date | None, horizon_days: int) -> str:
    return (
        format_ist_date(crossing_date)
        if crossing_date is not None
        else f"No projected crossing within {horizon_days} days."
    )


def _delta_days_display(value: int | None, qualifier: str) -> str:
    if value is None:
        return "Unavailable"
    if value == 0:
        return "0 days"
    magnitude = abs(value)
    sign = "+" if value > 0 else "-"
    if qualifier == "at_least":
        return f"At least {sign}{magnitude} days"
    if qualifier == "at_most":
        return f"At most {sign}{magnitude} days"
    return f"{sign}{magnitude} days"


def _delta_probability_display(value: Decimal | None) -> str:
    if value is None:
        return "Unavailable"
    percentage = (value * _ONE_HUNDRED).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    sign = "+" if percentage > _ZERO else ""
    return f"{sign}{percentage}%"


def _probability_display(value: Decimal | None) -> str:
    if value is None:
        return "Unavailable"
    percentage = (value * _ONE_HUNDRED).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{percentage}%"


def _display_value(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))
    return str(value)


def _candidate_series(candidate: Mapping[str, object]) -> tuple[Observation, ...]:
    raw = next(
        (
            candidate.get(name)
            for name in ("series", "observations", "history")
            if candidate.get(name) is not None
        ),
        None,
    )
    if raw is None:
        return ()
    if isinstance(raw, str | bytes | bytearray) or not isinstance(raw, Sequence):
        raise ValidationError(
            "Persisted forecast observations are malformed.", field="forecast_inputs"
        )
    try:
        return tuple(Observation.from_value(item) for item in raw)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            f"Persisted forecast observations are invalid: {error}.", field="forecast_inputs"
        ) from error


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _first_decimal(mapping: Mapping[str, object], names: Sequence[str]) -> Decimal | None:
    for name in names:
        if name not in mapping or mapping[name] is None:
            continue
        try:
            value = mapping[name]
            if isinstance(value, bool):
                return None
            decimal = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValidationError(
                f"Persisted forecast input {name!r} is not a valid decimal.",
                field="forecast_inputs",
            ) from None
        if not decimal.is_finite():
            raise ValidationError(
                f"Persisted forecast input {name!r} is not finite.", field="forecast_inputs"
            )
        return decimal
    return None


def _optional_int(mapping: Mapping[str, object], names: Sequence[str]) -> int | None:
    for name in names:
        value = mapping.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(
                f"Persisted forecast input {name!r} is invalid.", field="forecast_inputs"
            )
        return value
    return None


def _optional_positive_decimal(
    mapping: Mapping[str, object], names: Sequence[str]
) -> Decimal | None:
    value = _first_decimal(mapping, names)
    if value is not None and value <= _ZERO:
        raise ValidationError(
            "Persisted forecast period length must be positive.", field="forecast_inputs"
        )
    return value


def _normalise_codes(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValidationError("intervention_code must be text.", field="intervention_code")
        code = value.strip()
        if not code:
            raise ValidationError("intervention_code must not be blank.", field="intervention_code")
        if code not in result:
            result.append(code)
    return tuple(result)


def _parameters_json(parameters: Mapping[str, object]) -> str:
    try:
        value = json.dumps(
            _json_safe(parameters),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ValidationError(
            f"Simulation parameters cannot be serialized: {error}.", field="parameters"
        ) from error
    if len(value) > _MAX_PARAMETERS_JSON_LENGTH:
        raise ValidationError("Simulation parameters are too large.", field="parameters")
    return value


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Unsupported JSON value {type(value).__name__}.")


__all__ = [
    "ComparisonColumnView",
    "ComparisonHorizonView",
    "ComparisonStatus",
    "ComparisonView",
    "EffectParameterView",
    "InterventionOptionView",
    "SimulationContext",
    "SimulationForecastView",
    "SimulationScreenView",
    "build_simulation_projection",
    "build_simulation_view",
    "build_simulator_view",
    "load_simulation_context",
]
