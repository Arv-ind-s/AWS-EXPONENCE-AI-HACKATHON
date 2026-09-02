"""Transparent trend fitting and evidence-pressure terms for forecasting.

The forecast stage is deliberately deterministic.  This module fits a least-
squares trend over complete, computable observations and turns sustained
evidence into an explicit pressure term.  It contains no persistence or
framework imports, so the exact inputs and terms can be stored in a trace
before a forecast is persisted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date as CalendarDate
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_PERCENT: Final[Decimal] = Decimal("100")
_MAX_TEXT_LENGTH: Final[int] = 200
_MAX_SOURCE_LENGTH: Final[int] = 200


class Direction(StrEnum):
    """Covenant direction and the corresponding deterioration direction."""

    MIN = "min"
    MAX = "max"

    @classmethod
    def from_value(cls, value: Direction | str) -> Direction:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("direction must be 'min', 'max', or a Direction.")
        try:
            return cls(value.strip().lower())
        except ValueError as error:
            raise ValueError("direction must be either 'min' or 'max'.") from error


@dataclass(frozen=True, slots=True, init=False)
class Observation:
    """One dated value available to the trend fitter.

    ``observed_on`` is the canonical date.  The constructor also accepts
    ``date``, ``period_end`` and ``as_of_date`` because financial adapters use
    those established spellings.  Incomplete or non-computable observations
    are retained as facts and excluded by :func:`fit_trend` with a reason.
    """

    observed_on: CalendarDate
    value: Decimal | None
    is_complete: bool
    computable: bool
    period_days: int | None
    source_id: UUID | str | None
    reason: str | None

    def __init__(
        self,
        observed_on: CalendarDate | str | None = None,
        value: object = None,
        *,
        date: CalendarDate | str | None = None,
        period_end: CalendarDate | str | None = None,
        as_of_date: CalendarDate | str | None = None,
        is_complete: bool = True,
        complete: bool | None = None,
        computable: bool = True,
        is_computable: bool | None = None,
        period_days: int | None = None,
        source_id: UUID | str | None = None,
        id: UUID | str | None = None,
        reason: str | None = None,
        exclusion_reason: str | None = None,
    ) -> None:
        date_candidates = tuple(
            (name, candidate)
            for name, candidate in (
                ("date", date),
                ("period_end", period_end),
                ("as_of_date", as_of_date),
            )
            if candidate is not None
        )
        normalized_dates = tuple(
            (name, _calendar_date(candidate, name)) for name, candidate in date_candidates
        )
        if observed_on is None:
            if not normalized_dates:
                raise TypeError(
                    "Observation requires observed_on, date, period_end, or as_of_date."
                )
            normalized_observed_on = normalized_dates[0][1]
        else:
            normalized_observed_on = _calendar_date(observed_on, "observed_on")
        if any(candidate != normalized_observed_on for _, candidate in normalized_dates):
            raise ValueError("Observation date aliases must identify the same calendar date.")

        if complete is not None:
            if not isinstance(complete, bool):
                raise TypeError("complete must be a boolean or None.")
            if is_complete is not True and is_complete != complete:
                raise ValueError("is_complete and complete must agree.")
            is_complete = complete
        if not isinstance(is_complete, bool):
            raise TypeError("is_complete must be a boolean.")

        if is_computable is not None:
            if not isinstance(is_computable, bool):
                raise TypeError("is_computable must be a boolean or None.")
            if computable is not True and computable != is_computable:
                raise ValueError("computable and is_computable must agree.")
            computable = is_computable
        if not isinstance(computable, bool):
            raise TypeError("computable must be a boolean.")

        normalized_value = None if value is None else _decimal(value, "value")
        if period_days is not None:
            _positive_integer(period_days, "period_days")

        if (
            source_id is not None
            and id is not None
            and _identifier_text(source_id) != _identifier_text(id)
        ):
            raise ValueError("source_id and id must identify the same observation.")
        normalized_source_id = source_id if source_id is not None else id
        if normalized_source_id is not None:
            if isinstance(normalized_source_id, UUID):
                pass
            elif isinstance(normalized_source_id, str):
                normalized_source_id = _bounded_text(
                    normalized_source_id,
                    "source_id",
                    _MAX_SOURCE_LENGTH,
                )
            else:
                raise TypeError("source_id must be a UUID, text, or None.")

        if reason is not None and exclusion_reason is not None and reason != exclusion_reason:
            raise ValueError("reason and exclusion_reason must agree when both are supplied.")
        normalized_reason = reason if reason is not None else exclusion_reason
        if normalized_reason is not None:
            normalized_reason = _bounded_text(normalized_reason, "reason", _MAX_TEXT_LENGTH)

        object.__setattr__(self, "observed_on", normalized_observed_on)
        object.__setattr__(self, "value", normalized_value)
        object.__setattr__(self, "is_complete", is_complete)
        object.__setattr__(self, "computable", computable)
        object.__setattr__(self, "period_days", period_days)
        object.__setattr__(self, "source_id", normalized_source_id)
        object.__setattr__(self, "reason", normalized_reason)

    @classmethod
    def from_value(cls, value: Observation | Mapping[str, object] | object) -> Observation:
        """Normalize an observation value from a domain or adapter shape."""

        if isinstance(value, cls):
            return value
        observed_on = _read_any(
            value,
            "observed_on",
            "period_end",
            "as_of_date",
            "date",
            "event_date",
        )
        observation_value = _read_any(value, "value", "observed_value", default=None)
        complete = _read_any(value, "is_complete", "complete", default=True)
        computable = _read_any(value, "computable", "is_computable", default=True)
        source_id = _read_any(value, "source_id", "id", default=None)
        reason = _read_any(
            value,
            "reason",
            "exclusion_reason",
            "not_computable_reason",
            default=None,
        )
        return cls(
            observed_on=cast(CalendarDate, observed_on),
            value=observation_value,
            is_complete=cast(bool, complete),
            computable=cast(bool, computable),
            period_days=cast(int | None, _read_any(value, "period_days", default=None)),
            source_id=cast(UUID | str | None, source_id),
            reason=cast(str | None, reason),
        )

    from_observation = from_value

    @property
    def date(self) -> CalendarDate:
        """Compatibility spelling for the observation date."""

        return self.observed_on

    @property
    def period_end(self) -> CalendarDate:
        """Compatibility spelling used by financial-period adapters."""

        return self.observed_on

    @property
    def complete(self) -> bool:
        return self.is_complete

    @property
    def is_computable(self) -> bool:
        return self.computable

    @property
    def id(self) -> UUID | str | None:
        return self.source_id

    @property
    def usable(self) -> bool:
        """Whether this observation is eligible for the trend fit."""

        return self.is_complete and self.computable and self.value is not None

    @property
    def exclusion_reason(self) -> str | None:
        """Return the stable reason used when this fact is excluded."""

        if self.usable:
            return None
        if self.reason is not None:
            return self.reason
        if not self.is_complete:
            return "observation is incomplete"
        if not self.computable:
            return "observation is not computable"
        return "observation value is unavailable"


@dataclass(frozen=True, slots=True)
class TrendResult:
    """The explainable output of a least-squares trend fit.

    ``slope`` is change per selected observation period.  ``per_day_drift`` is
    the same slope divided by the configured or observed average period
    length, and is the term used by the daily projection path.
    """

    slope: Decimal
    per_day_drift: Decimal
    current_value: Decimal | None
    period_length_days: Decimal | None
    usable_observations: tuple[Observation, ...]
    excluded_observations: tuple[Observation, ...]
    reason: str | None = None
    intercept: Decimal | None = None

    @property
    def slope_per_period(self) -> Decimal:
        return self.slope

    @property
    def trend_slope(self) -> Decimal:
        return self.slope

    @property
    def observations(self) -> tuple[Observation, ...]:
        return self.usable_observations

    @property
    def excluded(self) -> tuple[Observation, ...]:
        return self.excluded_observations

    @property
    def has_sufficient_observations(self) -> bool:
        return (
            len(self.usable_observations) >= 2
            and len({item.observed_on for item in self.usable_observations}) >= 2
        )


@dataclass(frozen=True, slots=True)
class PressureTerm:
    """One sustained evidence contribution to a forecast pressure term."""

    evidence_id: UUID | str | None
    materiality: Decimal
    decay_factor: Decimal
    contribution: Decimal
    signed_contribution: Decimal
    included: bool
    reason: str

    @property
    def pressure(self) -> Decimal:
        return self.signed_contribution


@dataclass(frozen=True, slots=True)
class PressureResult:
    """The total evidence pressure and all terms that produced it."""

    direction: Direction
    magnitude: Decimal
    signed: Decimal
    terms: tuple[PressureTerm, ...] = field(default_factory=tuple)

    @property
    def pressure(self) -> Decimal:
        return self.magnitude

    @property
    def pressure_term(self) -> Decimal:
        return self.signed

    @property
    def contributions(self) -> tuple[PressureTerm, ...]:
        return self.terms


def fit_trend(
    series: Iterable[Observation | Mapping[str, object] | object],
    *,
    recent_periods: int | None = None,
    period_days: int | Decimal | None = None,
) -> TrendResult:
    """Fit a least-squares trend over recent complete usable observations.

    The regression uses selected-period indexes, making ``slope`` directly
    interpretable as change per financial period.  The per-day conversion uses
    the explicit ``period_days`` when supplied, otherwise the average calendar
    gap between selected observations.  No unusable observation is converted
    to zero; each remains in ``excluded_observations`` with a reason.
    """

    observations = _normalise_observations(series)
    selected_periods = _recent_period_count(recent_periods)
    configured_period_days = _optional_positive_decimal(period_days, "period_days")
    ordered = tuple(sorted(observations, key=_observation_sort_key))
    excluded = [item for item in ordered if not item.usable]
    usable = [item for item in ordered if item.usable]
    if selected_periods is not None and len(usable) > selected_periods:
        dropped = usable[:-selected_periods]
        excluded.extend(
            replace(item, computable=False, reason="outside configured recent period window")
            for item in dropped
        )
        usable = usable[-selected_periods:]
    selected = tuple(usable)
    excluded_tuple = tuple(sorted(excluded, key=_observation_sort_key))

    if not selected:
        return TrendResult(
            slope=_ZERO,
            per_day_drift=_ZERO,
            current_value=None,
            period_length_days=configured_period_days,
            usable_observations=(),
            excluded_observations=excluded_tuple,
            reason="no usable complete observations",
        )

    current_value = selected[-1].value
    assert current_value is not None
    distinct_dates = {item.observed_on for item in selected}
    if len(selected) < 2 or len(distinct_dates) < 2:
        return TrendResult(
            slope=_ZERO,
            per_day_drift=_ZERO,
            current_value=current_value,
            period_length_days=configured_period_days,
            usable_observations=selected,
            excluded_observations=excluded_tuple,
            reason="fewer than two usable observations",
            intercept=current_value,
        )

    values = tuple(cast(Decimal, item.value) for item in selected)
    x_values = tuple(Decimal(index) for index in range(len(values)))
    x_mean = sum(x_values, _ZERO) / Decimal(len(x_values))
    y_mean = sum(values, _ZERO) / Decimal(len(values))
    numerator = sum(
        (
            (x_value - x_mean) * (y_value - y_mean)
            for x_value, y_value in zip(x_values, values, strict=True)
        ),
        _ZERO,
    )
    denominator = sum(((x_value - x_mean) ** 2 for x_value in x_values), _ZERO)
    if denominator == _ZERO:  # pragma: no cover - guarded by distinct-date check
        return TrendResult(
            slope=_ZERO,
            per_day_drift=_ZERO,
            current_value=current_value,
            period_length_days=configured_period_days,
            usable_observations=selected,
            excluded_observations=excluded_tuple,
            reason="fewer than two distinct observation dates",
            intercept=current_value,
        )
    slope = numerator / denominator
    length = configured_period_days or _average_period_days(selected)
    per_day_drift = slope / length
    return TrendResult(
        slope=slope,
        per_day_drift=per_day_drift,
        current_value=current_value,
        period_length_days=length,
        usable_observations=selected,
        excluded_observations=excluded_tuple,
        intercept=y_mean - slope * x_mean,
    )


def evidence_pressure(
    items: Iterable[Mapping[str, object] | object],
    direction: Direction | str,
) -> PressureResult:
    """Sum sustained materiality weighted by decay in covenant direction.

    ``materiality_pct`` is the stored percentage-point representation used by
    the evidence ledger.  A ``materiality`` field is also accepted when a
    caller already supplies the fraction.  Non-sustained, non-counting or
    incomplete evidence is retained as an excluded pressure term with an
    explicit reason.
    """

    normalized_direction = Direction.from_value(direction)
    terms: list[PressureTerm] = []
    for item in _normalise_items(items):
        evidence_id = cast(UUID | str | None, _read_any(item, "id", "evidence_id", default=None))
        state = _read_any(item, "state", default=None)
        if state != "sustained":
            terms.append(_excluded_pressure_term(evidence_id, "evidence is not sustained"))
            continue
        counts = _read_any(item, "counts_toward_pressure", default=True)
        if not isinstance(counts, bool):
            raise TypeError("counts_toward_pressure must be a boolean.")
        if not counts:
            terms.append(
                _excluded_pressure_term(
                    evidence_id,
                    "evidence does not count toward pressure",
                )
            )
            continue

        materiality_value = _read_any(item, "materiality_pct", default=None)
        if materiality_value is not None:
            materiality = _decimal(materiality_value, "materiality_pct") / _PERCENT
        else:
            fraction_value = _read_any(item, "materiality", default=None)
            if fraction_value is None:
                terms.append(_excluded_pressure_term(evidence_id, "materiality is unavailable"))
                continue
            materiality = _decimal(fraction_value, "materiality")
        if materiality < _ZERO:
            raise ValueError("materiality must not be negative.")

        decay_value = _read_any(item, "decay_factor", default=None)
        if decay_value is None:
            terms.append(_excluded_pressure_term(evidence_id, "decay factor is unavailable"))
            continue
        decay = _decimal(decay_value, "decay_factor")
        if not _ZERO <= decay <= _ONE:
            raise ValueError("decay_factor must be between zero and one.")
        contribution = materiality * decay
        signed = contribution if normalized_direction is Direction.MAX else -contribution
        terms.append(
            PressureTerm(
                evidence_id=evidence_id,
                materiality=materiality,
                decay_factor=decay,
                contribution=contribution,
                signed_contribution=signed,
                included=True,
                reason="sustained evidence included in pressure",
            )
        )
    signed_total = sum((term.signed_contribution for term in terms if term.included), _ZERO)
    return PressureResult(
        direction=normalized_direction,
        magnitude=abs(signed_total),
        signed=signed_total,
        terms=tuple(terms),
    )


def _excluded_pressure_term(evidence_id: UUID | str | None, reason: str) -> PressureTerm:
    return PressureTerm(
        evidence_id=evidence_id,
        materiality=_ZERO,
        decay_factor=_ZERO,
        contribution=_ZERO,
        signed_contribution=_ZERO,
        included=False,
        reason=reason,
    )


def _normalise_observations(
    values: Iterable[Observation | Mapping[str, object] | object],
) -> tuple[Observation, ...]:
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("series must be an iterable of observations, not text.")
    try:
        iterator = iter(values)
    except TypeError as error:
        raise TypeError("series must be an iterable of observations.") from error
    return tuple(Observation.from_value(value) for value in iterator)


def _normalise_items(values: Iterable[Mapping[str, object] | object]) -> tuple[object, ...]:
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("items must be an iterable of evidence values, not text.")
    try:
        iterator = iter(values)
    except TypeError as error:
        raise TypeError("items must be an iterable of evidence values.") from error
    return tuple(iterator)


def _observation_sort_key(value: Observation) -> tuple[CalendarDate, str]:
    return (
        value.observed_on,
        _identifier_text(value.source_id) if value.source_id is not None else "",
    )


def _average_period_days(values: Sequence[Observation]) -> Decimal:
    explicit = tuple(Decimal(item.period_days) for item in values if item.period_days is not None)
    if len(explicit) == len(values):
        return sum(explicit, _ZERO) / Decimal(len(explicit))
    total_days = (values[-1].observed_on - values[0].observed_on).days
    if total_days <= 0:
        raise ValueError("Observation dates must span at least one calendar day.")
    return Decimal(total_days) / Decimal(len(values) - 1)


def _recent_period_count(value: object) -> int | None:
    if value is None:
        return None
    _positive_integer(value, "recent_periods")
    return cast(int, value)


def _optional_positive_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    result = _decimal(value, field_name)
    if result <= _ZERO:
        raise ValueError(f"{field_name} must be positive.")
    return result


def _read_any(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    marker = object()
    for name in names:
        candidate = getattr(value, name, marker)
        if candidate is not marker:
            return candidate
    return default


def _identifier_text(value: object) -> str:
    return str(value)


def _bounded_text(value: object, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains a control character.")
    return normalized


def _calendar_date(value: object, field_name: str) -> CalendarDate:
    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a calendar date, not a datetime.")
    if isinstance(value, CalendarDate):
        return value
    if isinstance(value, str):
        try:
            return CalendarDate.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO calendar date.") from error
    raise TypeError(f"{field_name} must be a calendar date.")


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field_name} must be a finite Decimal.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite Decimal.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal.")
    return result


def _positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


# Descriptive aliases keep the stage discoverable from either vocabulary.
trend = fit_trend
fit = fit_trend
compute_pressure = evidence_pressure
pressure_from_evidence = evidence_pressure
sum_sustained_pressure = evidence_pressure


__all__ = [
    "Direction",
    "Observation",
    "PressureResult",
    "PressureTerm",
    "TrendResult",
    "compute_pressure",
    "evidence_pressure",
    "fit",
    "fit_trend",
    "pressure_from_evidence",
    "sum_sustained_pressure",
    "trend",
]
