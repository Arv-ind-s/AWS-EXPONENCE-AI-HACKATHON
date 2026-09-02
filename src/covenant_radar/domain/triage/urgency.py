"""Deterministic portfolio triage and urgency ranking (contract ``C-39``).

Stage 6 consumes persisted stage-4 forecast facts.  It never calls a model,
recomputes a forecast, or silently drops a borrower.  A borrower with no
usable forecast remains in the returned queue with an explicit state and
reason, after rankable entries.  A forecast below T2 is retained as a
suppressed watch entry; its probability is not used to manufacture urgency.

The total ordering is, in order:

1. rankable urgency descending;
2. exposure descending;
3. borrower reference ascending.

Non-rankable entries have no urgency and are placed after rankable entries,
while the same exposure/reference tie-break keeps their order deterministic.
The rule and the rule actually needed for each row are retained in ``why``
for the later why-panel.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Final, cast
from uuid import UUID

from covenant_radar.domain.triage.banding import (
    ACT_BAND,
    AMBER_BAND,
    WATCH_BAND,
    TriageThresholds,
    band,
)

AVAILABLE_STATE: Final[str] = "available"
SUPPRESSED_STATE: Final[str] = "suppressed"
NO_FORECAST_STATE: Final[str] = "no_forecast"
UNRANKABLE_STATE: Final[str] = "unrankable"

TIE_BREAK_RULE: Final[str] = (
    "urgency descending, then exposure descending, then borrower reference ascending"
)
WORST_HORIZON_RULE: Final[str] = (
    "highest probability at a covenant horizon; ties use higher confidence, "
    "then shorter horizon, then covenant version id"
)
URGENCY_FORMULA: Final[str] = "probability × exposure × confidence"

_MAX_REFERENCE_LENGTH: Final[int] = 100
_MAX_REASON_LENGTH: Final[int] = 500
_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_MISSING: Final[object] = object()


@dataclass(frozen=True, slots=True, init=False)
class ForecastFact:
    """The forecast fields required to rank one covenant horizon.

    The constructor accepts the canonical names and common persistence
    aliases.  ``probability=None`` is an explicit absence, not a zero risk.
    ``below_confidence_floor`` and ``suppressed`` both indicate that a
    probability must not be displayed or used for urgency.
    """

    covenant_version_id: UUID
    horizon_days: int
    probability: Decimal | None
    confidence: Decimal | None
    below_confidence_floor: bool
    suppressed: bool
    reason: str | None

    def __init__(
        self,
        covenant_version_id: UUID | None = None,
        horizon_days: int | None = None,
        probability: object = None,
        confidence: object = None,
        *,
        below_confidence_floor: bool = False,
        suppressed: bool | None = None,
        reason: str | None = None,
        version_id: UUID | None = None,
        id: UUID | None = None,
        forecast_id: UUID | None = None,
        horizon: int | None = None,
        suppression_reason: str | None = None,
        probability_suppressed: bool | None = None,
    ) -> None:
        normalized_id = _coalesce_uuid(
            covenant_version_id,
            version_id,
            id,
            forecast_id,
            field_name="covenant_version_id",
        )
        if normalized_id is None:
            raise TypeError("ForecastFact requires covenant_version_id.")
        normalized_horizon = _coalesce_int(horizon_days, horizon, "horizon_days")
        if normalized_horizon is None:
            raise TypeError("ForecastFact requires horizon_days.")
        _non_negative_integer(normalized_horizon, "horizon_days")
        if not isinstance(below_confidence_floor, bool):
            raise TypeError("below_confidence_floor must be a boolean.")
        if suppressed is not None and not isinstance(suppressed, bool):
            raise TypeError("suppressed must be a boolean or None.")
        if probability_suppressed is not None and not isinstance(probability_suppressed, bool):
            raise TypeError("probability_suppressed must be a boolean or None.")
        if (
            suppressed is not None
            and probability_suppressed is not None
            and suppressed != probability_suppressed
        ):
            raise ValueError("suppressed and probability_suppressed must agree.")
        if reason is not None and suppression_reason is not None and reason != suppression_reason:
            raise ValueError("reason and suppression_reason must agree.")
        normalized_reason = reason if reason is not None else suppression_reason
        if normalized_reason is not None:
            normalized_reason = _bounded_text(normalized_reason, "reason", _MAX_REASON_LENGTH)

        normalized_probability = _optional_fraction(probability, "probability")
        normalized_confidence = _optional_fraction(confidence, "confidence")
        explicit_suppression = (
            suppressed
            if suppressed is not None
            else bool(probability_suppressed)
            if probability_suppressed is not None
            else False
        )
        object.__setattr__(self, "covenant_version_id", normalized_id)
        object.__setattr__(self, "horizon_days", normalized_horizon)
        object.__setattr__(self, "probability", normalized_probability)
        object.__setattr__(self, "confidence", normalized_confidence)
        object.__setattr__(self, "below_confidence_floor", below_confidence_floor)
        object.__setattr__(self, "suppressed", explicit_suppression)
        object.__setattr__(self, "reason", normalized_reason)

    @classmethod
    def from_value(cls, value: ForecastFact | Mapping[str, object] | object) -> ForecastFact:
        """Normalize a domain value, mapping, or persistence row."""

        if isinstance(value, cls):
            return value
        return cls(
            covenant_version_id=cast(
                UUID | None,
                _read_any(value, "covenant_version_id", "version_id", "id", default=None),
            ),
            horizon_days=cast(
                int | None,
                _read_any(value, "horizon_days", "horizon", default=None),
            ),
            probability=_read_any(value, "probability", default=None),
            confidence=_read_any(value, "confidence", default=None),
            below_confidence_floor=cast(
                bool,
                _read_any(value, "below_confidence_floor", "below_floor", default=False),
            ),
            suppressed=cast(
                bool | None,
                _read_any(value, "suppressed", "probability_suppressed", default=None),
            ),
            reason=cast(
                str | None,
                _read_any(
                    value,
                    "reason",
                    "not_computable_reason",
                    "suppression_reason",
                    default=None,
                ),
            ),
        )

    @property
    def forecast_id(self) -> UUID:
        """Compatibility view for code that calls the fact's identity id."""

        return self.covenant_version_id

    @property
    def probability_suppressed(self) -> bool:
        return self.suppressed


ForecastInput = ForecastFact
ForecastFacts = ForecastFact


@dataclass(frozen=True, slots=True, init=False)
class TriageInput:
    """All borrower-level facts needed by :func:`rank`."""

    borrower_id: UUID
    reference: str
    exposure: Decimal | None
    forecasts: tuple[ForecastFact, ...]
    sma_band: str | None

    def __init__(
        self,
        borrower_id: UUID,
        reference: str | None = None,
        exposure: object = None,
        forecasts: Iterable[ForecastFact | Mapping[str, object] | object] = (),
        *,
        borrower_reference: str | None = None,
        borrower_ref: str | None = None,
        forecast: ForecastFact | Mapping[str, object] | object | None = None,
        sma_band: str | None = None,
    ) -> None:
        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        normalized_reference = _coalesce_text(
            reference,
            borrower_reference,
            borrower_ref,
            "reference",
        )
        normalized_exposure = _optional_money(exposure, "exposure")
        if isinstance(forecasts, Mapping) or isinstance(forecasts, ForecastFact):
            normalized_forecasts: tuple[ForecastFact, ...] = (ForecastFact.from_value(forecasts),)
        else:
            if isinstance(forecasts, str | bytes | bytearray):
                raise TypeError("forecasts must be an iterable of forecast facts, not text.")
            try:
                normalized_forecasts = tuple(ForecastFact.from_value(item) for item in forecasts)
            except TypeError as error:
                raise TypeError("forecasts must be an iterable of forecast facts.") from error
        if forecast is not None:
            extra_forecast = ForecastFact.from_value(forecast)
            if normalized_forecasts:
                raise ValueError("Provide forecasts or forecast, not both.")
            normalized_forecasts = (extra_forecast,)
        normalized_sma_band = _optional_text(sma_band, "sma_band", _MAX_REFERENCE_LENGTH)
        object.__setattr__(self, "borrower_id", borrower_id)
        object.__setattr__(self, "reference", normalized_reference)
        object.__setattr__(self, "exposure", normalized_exposure)
        object.__setattr__(self, "forecasts", normalized_forecasts)
        object.__setattr__(self, "sma_band", normalized_sma_band)

    @classmethod
    def from_value(cls, value: TriageInput | Mapping[str, object] | object) -> TriageInput:
        """Normalize an adapter-neutral borrower triage record."""

        if isinstance(value, cls):
            return value
        return cls(
            borrower_id=cast(UUID, _read_any(value, "borrower_id", default=None)),
            reference=cast(
                str | None,
                _read_any(value, "reference", "borrower_reference", "borrower_ref", default=None),
            ),
            exposure=_read_any(value, "exposure", default=None),
            forecasts=cast(
                Iterable[ForecastFact | Mapping[str, object] | object],
                _read_any(value, "forecasts", default=()),
            ),
            sma_band=cast(str | None, _read_any(value, "sma_band", default=None)),
        )

    @property
    def borrower_reference(self) -> str:
        """Compatibility spelling used by borrower-facing adapters."""

        return self.reference


@dataclass(frozen=True, slots=True)
class TriageEntry:
    """One immutable, explainable row in the ordered morning queue."""

    borrower_id: UUID
    reference: str
    exposure: Decimal | None
    worst_covenant_version_id: UUID | None
    worst_horizon: int | None
    probability: Decimal | None
    confidence: Decimal | None
    urgency: Decimal | None
    band: str
    sma_band: str | None
    state: str
    reason: str
    rank: int
    tie_break_rule: str
    why: Mapping[str, object] = field(default_factory=dict)
    what_changed: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        _bounded_text(self.reference, "reference", _MAX_REFERENCE_LENGTH)
        if self.exposure is not None:
            _optional_money(self.exposure, "exposure")
        if self.worst_covenant_version_id is not None and not isinstance(
            self.worst_covenant_version_id, UUID
        ):
            raise TypeError("worst_covenant_version_id must be a UUID or None.")
        if self.worst_horizon is not None:
            _non_negative_integer(self.worst_horizon, "worst_horizon")
        if self.probability is not None:
            _fraction(self.probability, "probability")
        if self.confidence is not None:
            _fraction(self.confidence, "confidence")
        if self.urgency is not None:
            urgency = _decimal(self.urgency, "urgency")
            if urgency < _ZERO:
                raise ValueError("urgency must not be negative.")
            object.__setattr__(self, "urgency", urgency)
        if self.band not in (ACT_BAND, AMBER_BAND, WATCH_BAND):
            raise ValueError(f"Unknown triage band {self.band!r}.")
        if self.state not in (
            AVAILABLE_STATE,
            SUPPRESSED_STATE,
            NO_FORECAST_STATE,
            UNRANKABLE_STATE,
        ):
            raise ValueError(f"Unknown triage state {self.state!r}.")
        _bounded_text(self.reason, "reason", _MAX_REASON_LENGTH)
        _positive_integer(self.rank, "rank")
        _bounded_text(self.tie_break_rule, "tie_break_rule", 500)
        if self.what_changed is not None:
            _bounded_text(self.what_changed, "what_changed", 2000)
        if not isinstance(self.why, Mapping):
            raise TypeError("why must be a mapping.")
        object.__setattr__(self, "reference", self.reference.strip())
        if self.exposure is not None:
            object.__setattr__(self, "exposure", _optional_money(self.exposure, "exposure"))
        if self.probability is not None:
            object.__setattr__(self, "probability", _fraction(self.probability, "probability"))
        if self.confidence is not None:
            object.__setattr__(self, "confidence", _fraction(self.confidence, "confidence"))
        object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(self, "tie_break_rule", self.tie_break_rule.strip())
        if self.what_changed is not None:
            object.__setattr__(self, "what_changed", self.what_changed.strip())
        object.__setattr__(self, "why", _freeze(self.why))

    @property
    def borrower_reference(self) -> str:
        return self.reference

    @property
    def borrower_ref(self) -> str:
        return self.reference

    @property
    def applied_tie_break(self) -> str:
        return cast(str, self.why.get("applied_tie_break", self.tie_break_rule))

    @property
    def applied_rule(self) -> str:
        """Compatibility spelling for why-panel consumers."""

        return self.applied_tie_break

    @property
    def tie_break(self) -> str:
        return self.applied_tie_break

    @property
    def why_panel(self) -> Mapping[str, object]:
        return self.why

    @property
    def explanation(self) -> Mapping[str, object]:
        return self.why

    @property
    def suppressed(self) -> bool:
        return self.state == SUPPRESSED_STATE

    @property
    def forecast_state(self) -> str:
        return self.state

    @property
    def probability_suppressed(self) -> bool:
        return self.suppressed

    @property
    def has_forecast(self) -> bool:
        return self.state != NO_FORECAST_STATE

    @property
    def status(self) -> str:
        return self.state


def urgency(
    probability: Decimal | int | str | None,
    exposure: Decimal | int | str | None,
    confidence: Decimal | int | str | None,
) -> Decimal | None:
    """Compute the exact C-39 urgency product, or ``None`` if not rankable."""

    if probability is None or exposure is None or confidence is None:
        return None
    exposure_value = _optional_money(exposure, "exposure")
    assert exposure_value is not None
    return (
        _fraction(probability, "probability") * exposure_value * _fraction(confidence, "confidence")
    )


def compute_urgency(
    probability: Decimal | int | str | None,
    exposure: Decimal | int | str | None,
    confidence: Decimal | int | str | None,
) -> Decimal | None:
    """Compatibility spelling for :func:`urgency`."""

    return urgency(probability, exposure, confidence)


def rank(
    entries: Sequence[TriageInput | Mapping[str, object] | object],
    thresholds: TriageThresholds | Mapping[str, object] | object,
) -> list[TriageEntry]:
    """Rank borrower inputs into a total, stable and explainable queue."""

    configured = TriageThresholds.from_store(thresholds)
    if isinstance(entries, str | bytes | bytearray):
        raise TypeError("entries must be a sequence of triage inputs, not text.")
    try:
        values = tuple(TriageInput.from_value(entry) for entry in entries)
    except TypeError as error:
        raise TypeError("entries must be a sequence of triage inputs.") from error
    _unique_borrowers(values)
    ranked = sorted(
        (_build_ranked_entry(value, configured) for value in values),
        key=_sort_key,
    )
    urgency_counts = _urgency_counts(ranked)
    result: list[TriageEntry] = []
    for position, item in enumerate(ranked, start=1):
        applied = _applied_tie_break(item, urgency_counts)
        why = dict(item.why)
        why["applied_tie_break"] = applied
        why["tie_break"] = applied
        why["rank"] = position
        result.append(
            TriageEntry(
                borrower_id=item.borrower_id,
                reference=item.reference,
                exposure=item.exposure,
                worst_covenant_version_id=item.worst_covenant_version_id,
                worst_horizon=item.worst_horizon,
                probability=item.probability,
                confidence=item.confidence,
                urgency=item.urgency,
                band=item.band,
                sma_band=item.sma_band,
                state=item.state,
                reason=item.reason,
                rank=position,
                tie_break_rule=TIE_BREAK_RULE,
                why=why,
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class _RankedFacts:
    borrower_id: UUID
    reference: str
    exposure: Decimal | None
    worst_covenant_version_id: UUID | None
    worst_horizon: int | None
    probability: Decimal | None
    confidence: Decimal | None
    urgency: Decimal | None
    band: str
    sma_band: str | None
    state: str
    reason: str
    why: Mapping[str, object]


def _build_ranked_entry(value: TriageInput, thresholds: TriageThresholds) -> _RankedFacts:
    if not value.forecasts:
        return _RankedFacts(
            borrower_id=value.borrower_id,
            reference=value.reference,
            exposure=value.exposure,
            worst_covenant_version_id=None,
            worst_horizon=None,
            probability=None,
            confidence=None,
            urgency=None,
            band=WATCH_BAND,
            sma_band=value.sma_band,
            state=NO_FORECAST_STATE,
            reason=(
                "no forecast is available for this borrower; included at the bottom of the queue"
            ),
            why=_why(
                thresholds,
                state=NO_FORECAST_STATE,
                reason="no forecast is available",
                worst_horizon=None,
                probability=None,
                confidence=None,
                urgency=None,
            ),
        )

    usable = tuple(
        forecast
        for forecast in value.forecasts
        if _is_usable(forecast, thresholds.confidence_floor)
    )
    if usable:
        selected = max(usable, key=_worst_key)
        selected_probability = _required_probability(selected)
        selected_confidence = selected.confidence
        selected_urgency = urgency(
            selected_probability,
            value.exposure,
            selected_confidence,
        )
        if selected_urgency is None:
            state = UNRANKABLE_STATE
            selected_band = WATCH_BAND
            reason = (
                "forecast is available but exposure or confidence is unavailable; "
                "urgency is not rankable"
            )
        else:
            state = AVAILABLE_STATE
            selected_band = band(selected_probability, thresholds)
            reason = (
                f"worst covenant-horizon is {selected.horizon_days} days; "
                f"urgency = {URGENCY_FORMULA}"
            )
        return _RankedFacts(
            borrower_id=value.borrower_id,
            reference=value.reference,
            exposure=value.exposure,
            worst_covenant_version_id=selected.covenant_version_id,
            worst_horizon=selected.horizon_days,
            probability=selected_probability,
            confidence=selected_confidence,
            urgency=selected_urgency,
            band=selected_band,
            sma_band=value.sma_band,
            state=state,
            reason=reason,
            why=_why(
                thresholds,
                state=state,
                reason=reason,
                worst_horizon=selected.horizon_days,
                probability=selected_probability,
                confidence=selected_confidence,
                urgency=selected_urgency,
            ),
        )

    selected = max(value.forecasts, key=_suppressed_key)
    suppression_reason = _suppression_reason(selected, thresholds.confidence_floor)
    return _RankedFacts(
        borrower_id=value.borrower_id,
        reference=value.reference,
        exposure=value.exposure,
        worst_covenant_version_id=selected.covenant_version_id,
        worst_horizon=selected.horizon_days,
        probability=None,
        confidence=selected.confidence,
        urgency=None,
        band=WATCH_BAND,
        sma_band=value.sma_band,
        state=SUPPRESSED_STATE,
        reason=suppression_reason,
        why=_why(
            thresholds,
            state=SUPPRESSED_STATE,
            reason=suppression_reason,
            worst_horizon=selected.horizon_days,
            probability=None,
            confidence=selected.confidence,
            urgency=None,
        ),
    )


def _is_usable(forecast: ForecastFact, confidence_floor: Decimal) -> bool:
    return (
        forecast.probability is not None
        and forecast.confidence is not None
        and not forecast.below_confidence_floor
        and not forecast.suppressed
        and forecast.confidence >= confidence_floor
    )


def _worst_key(forecast: ForecastFact) -> tuple[Decimal, Decimal, int, int]:
    probability = forecast.probability
    confidence = forecast.confidence
    assert probability is not None
    assert confidence is not None
    # max() makes the first two values the risk-first decision.  The negative
    # horizon chooses the nearer horizon for equal probabilities.  The final
    # value makes equal facts deterministic without depending on input order.
    return probability, confidence, -forecast.horizon_days, -forecast.covenant_version_id.int


def _suppressed_key(forecast: ForecastFact) -> tuple[int, Decimal, Decimal, int, int]:
    probability = forecast.probability or _ZERO
    confidence = forecast.confidence or _ZERO
    return (
        0 if forecast.probability is not None else -1,
        probability,
        confidence,
        -forecast.horizon_days,
        -forecast.covenant_version_id.int,
    )


def _required_probability(forecast: ForecastFact) -> Decimal:
    if forecast.probability is None:  # pragma: no cover - guarded by _is_usable
        raise RuntimeError("A usable forecast must have a probability.")
    return forecast.probability


def _suppression_reason(forecast: ForecastFact, floor: Decimal) -> str:
    if forecast.reason is not None:
        return f"forecast suppressed: {forecast.reason}"
    if forecast.below_confidence_floor or (
        forecast.confidence is not None and forecast.confidence < floor
    ):
        confidence_text = "unknown" if forecast.confidence is None else str(forecast.confidence)
        return (
            f"forecast suppressed: confidence {confidence_text} is below the inclusive "
            f"T2 floor {floor}; probability is absent and the borrower remains watch"
        )
    return "forecast suppressed: probability is unavailable; the borrower remains watch"


def _sort_key(item: _RankedFacts) -> tuple[int, Decimal, int, Decimal, str]:
    # An absent urgency is explicitly after every computable urgency.  Within
    # each group the documented exposure/reference tie-break is total.
    return (
        0 if item.urgency is not None else 1,
        -(item.urgency if item.urgency is not None else _ZERO),
        1 if item.exposure is None else 0,
        -(item.exposure if item.exposure is not None else _ZERO),
        item.reference,
    )


def _urgency_counts(items: Sequence[_RankedFacts]) -> Mapping[Decimal, int]:
    counts: dict[Decimal, int] = {}
    for item in items:
        if item.urgency is not None:
            counts[item.urgency] = counts.get(item.urgency, 0) + 1
    return counts


def _applied_tie_break(item: _RankedFacts, counts: Mapping[Decimal, int]) -> str:
    if item.urgency is None:
        return "exposure descending, then borrower reference ascending (urgency unavailable)"
    if counts.get(item.urgency, 0) > 1:
        return "exposure descending, then borrower reference ascending (urgency tied)"
    return "urgency descending"


def _why(
    thresholds: TriageThresholds,
    *,
    state: str,
    reason: str,
    worst_horizon: int | None,
    probability: Decimal | None,
    confidence: Decimal | None,
    urgency: Decimal | None,
) -> Mapping[str, object]:
    return {
        "state": state,
        "reason": reason,
        "urgency_formula": URGENCY_FORMULA,
        "worst_horizon_rule": WORST_HORIZON_RULE,
        "worst_horizon": worst_horizon,
        "probability": probability,
        "confidence": confidence,
        "urgency": urgency,
        "t1": {"act": thresholds.act, "amber": thresholds.amber},
        "t2": {"confidence_floor": thresholds.confidence_floor},
        "banding_rule": (
            "act when probability >= T1.act; amber when probability >= T1.amber; "
            "otherwise watch; exact boundaries belong to the higher band"
        ),
        "tie_break_rule": TIE_BREAK_RULE,
        "tie_break": TIE_BREAK_RULE,
    }


def _unique_borrowers(values: Sequence[TriageInput]) -> None:
    seen: set[UUID] = set()
    references: set[str] = set()
    for value in values:
        if value.borrower_id in seen:
            raise ValueError(f"Borrower {value.borrower_id} occurs more than once in triage input.")
        if value.reference in references:
            raise ValueError(
                f"Borrower reference {value.reference!r} occurs more than once in triage input."
            )
        seen.add(value.borrower_id)
        references.add(value.reference)


def _read_any(value: object, *names: str, default: object = _MISSING) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        candidate = getattr(value, name, _MISSING)
        if candidate is not _MISSING:
            return candidate
    return default


def _coalesce_uuid(*values: UUID | None, field_name: str) -> UUID | None:
    supplied = tuple(value for value in values if value is not None)
    if not supplied:
        return None
    if any(not isinstance(value, UUID) for value in supplied):
        raise TypeError(f"{field_name} must be a UUID.")
    if len(set(supplied)) != 1:
        raise ValueError(f"{field_name} aliases must identify the same UUID.")
    return supplied[0]


def _coalesce_int(first: int | None, second: int | None, field_name: str) -> int | None:
    if first is not None and second is not None and first != second:
        raise ValueError(f"{field_name} aliases must identify the same value.")
    value = first if first is not None else second
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{field_name} must be an integer.")
    return value


def _coalesce_text(
    first: str | None,
    second: str | None,
    third: str | None,
    field_name: str,
) -> str:
    supplied = tuple(value for value in (first, second, third) if value is not None)
    if not supplied:
        raise TypeError(f"{field_name} is required.")
    if any(not isinstance(value, str) for value in supplied):
        raise TypeError(f"{field_name} must be text.")
    normalized = tuple(value.strip() for value in supplied)
    if len(set(normalized)) != 1:
        raise ValueError(f"{field_name} aliases must identify the same value.")
    return _bounded_text(normalized[0], field_name, _MAX_REFERENCE_LENGTH)


def _optional_text(value: object, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name, maximum)


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} must be non-empty text of at most {maximum} characters.")
    return value.strip()


def _optional_fraction(value: object, field_name: str) -> Decimal | None:
    return None if value is None else _fraction(value, field_name)


def _optional_money(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    result = _decimal(value, field_name)
    if result < _ZERO:
        raise ValueError(f"{field_name} must not be negative.")
    return result


def _fraction(value: object, field_name: str) -> Decimal:
    result = _decimal(value, field_name)
    if not _ZERO <= result <= _ONE:
        raise ValueError(f"{field_name} must be between zero and one inclusive.")
    return result


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


def _non_negative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = [
    "AVAILABLE_STATE",
    "ForecastFact",
    "ForecastFacts",
    "ForecastInput",
    "NO_FORECAST_STATE",
    "SUPPRESSED_STATE",
    "TIE_BREAK_RULE",
    "TriageEntry",
    "TriageInput",
    "TriageThresholds",
    "Thresholds",
    "UNRANKABLE_STATE",
    "URGENCY_FORMULA",
    "WATCH_BAND",
    "WORST_HORIZON_RULE",
    "compute_urgency",
    "rank",
    "urgency",
]


Thresholds = TriageThresholds
