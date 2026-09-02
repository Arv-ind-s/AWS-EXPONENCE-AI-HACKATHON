"""Geometric decay for evidence-ledger items.

Decay is a weighting operation, not a retention operation.  An evidence item
remains in the ledger after its observation becomes old; only the contribution
it makes to forecast pressure is reduced.  This distinction is kept explicit
in :class:`DecayScore.visible` and :attr:`DecayScore.pressure_contribution`.

The decay rate is the daily retention factor.  For example, a rate of ``0.9``
produces factors of ``1``, ``0.9`` and ``0.81`` after zero, one and two days.
The rate must come from the caller's approved configuration.  This module has
no policy default, which prevents an offline replay from silently using a
different scoring rule from the run that produced the stored evidence.

The module is deliberately independent of storage and web frameworks.  It
accepts the same fact and mapping shapes as the preceding evidence stages and
returns an :class:`EvidenceScore`, extended with the decay-specific values
needed by the forecast and trace stages.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from typing import Final, cast
from uuid import UUID

from covenant_radar.domain.signals.evidence import (
    EvidenceFacts,
    EvidenceScore,
    EvidenceTransitionFacts,
    SignalEventFacts,
    to_evidence_facts,
)

_T3_NAME: Final[str] = "T3"
_DECAY_SECTION_NAMES: Final[tuple[str, ...]] = (_T3_NAME, "decay", "evidence")
_RATE_FIELDS: Final[tuple[str, ...]] = (
    "decay_rate",
    "rate",
    "daily_retention_factor",
)
_DISPLAY_FLOOR_FIELDS: Final[tuple[str, ...]] = (
    "display_floor",
    "visibility_floor",
    "decay_display_floor",
)
_DECAY_RESET_RULE: Final[str] = "decay.reset.new_observation.v1"
_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_PERCENT: Final[Decimal] = Decimal("100")
_MAX_SOURCE_ID_LENGTH: Final[int] = 128


@dataclass(frozen=True, slots=True)
class DecayThresholds:
    """Validated configuration for the evidence-decay stage.

    ``decay_rate`` is a daily retention factor in the closed interval
    ``[0, 1]``.  ``display_floor`` is optional metadata for presentation
    clients; it is deliberately not used to filter items.  A threshold
    snapshot id may be carried into a reset transition when the supplied
    configuration store exposes one.
    """

    decay_rate: Decimal
    display_floor: Decimal | None = None
    threshold_snapshot_id: UUID | None = None

    def __post_init__(self) -> None:
        rate = _decimal(self.decay_rate, "decay_rate")
        if not _ZERO <= rate <= _ONE:
            raise ValueError("decay_rate must be between zero and one inclusive.")
        floor = self.display_floor
        if floor is not None:
            floor = _decimal(floor, "display_floor")
            if not _ZERO <= floor <= _ONE:
                raise ValueError("display_floor must be between zero and one inclusive.")
        if self.threshold_snapshot_id is not None and not isinstance(
            self.threshold_snapshot_id, UUID
        ):
            raise TypeError("threshold_snapshot_id must be a UUID or None.")
        object.__setattr__(self, "decay_rate", rate)
        object.__setattr__(self, "display_floor", floor)

    @property
    def rate(self) -> Decimal:
        """Compatibility spelling for the configured daily retention factor."""

        return self.decay_rate

    @classmethod
    def from_store(cls, store: object) -> DecayThresholds:
        """Read decay settings from an adapter-neutral configuration store.

        The preferred shape is ``store.get("T3")`` with a ``decay_rate``
        field, matching the existing persistence threshold access pattern.
        A dedicated ``decay`` or ``evidence`` section and direct mappings or
        attributes are also accepted so configuration can be separated later
        without changing the domain API.  Missing settings are rejected.
        """

        if isinstance(store, cls):
            return store

        sections = _store_sections(store)
        for section in sections:
            rate = _read_first(section, _RATE_FIELDS)
            if rate is None:
                continue
            display_floor = _read_first(section, _DISPLAY_FLOOR_FIELDS)
            return cls(
                decay_rate=cast(Decimal, rate),
                display_floor=cast(Decimal | None, display_floor),
                threshold_snapshot_id=_snapshot_id(store),
            )
        raise ValueError("Decay configuration is missing a daily retention factor ('decay_rate').")


@dataclass(frozen=True, slots=True)
class DecayScore(EvidenceScore):
    """An evidence score with decay and pressure facts for one item.

    The inherited evidence fields are intentionally repeated directly on the
    result, so a caller cannot accidentally discard visibility or state by
    unpacking a nested object.  ``visible`` is always true for a valid score;
    it is a field rather than an implicit assumption to make the retention
    invariant inspectable by API and UI layers.
    """

    pressure_contribution: Decimal = field(default=_ZERO)
    visible: bool = True
    days_since_last_seen: int = 0
    reset: bool = False
    thresholds: DecayThresholds | None = None

    def __post_init__(self) -> None:
        EvidenceScore.__post_init__(self)
        _validate_decimal(self.pressure_contribution, "pressure_contribution")
        if self.pressure_contribution < _ZERO:
            raise ValueError("pressure_contribution must not be negative.")
        if not isinstance(self.visible, bool):
            raise TypeError("visible must be a boolean.")
        if self.visible is not True:
            raise ValueError("Evidence items must remain visible after decay.")
        factor = self.decay_factor
        if factor is None:
            raise ValueError("decay_factor is required for a DecayScore.")
        _validate_decimal(factor, "decay_factor")
        if not _ZERO <= factor <= _ONE:
            raise ValueError("decay_factor must be between zero and one.")
        _non_negative_integer(self.days_since_last_seen, "days_since_last_seen")
        if not isinstance(self.reset, bool):
            raise TypeError("reset must be a boolean.")
        if self.thresholds is not None and not isinstance(self.thresholds, DecayThresholds):
            raise TypeError("thresholds must be DecayThresholds or None.")
        expected = pressure_contribution(
            self.materiality_pct,
            self.counts_toward_pressure,
            factor,
        )
        if self.pressure_contribution != expected:
            raise ValueError("pressure_contribution does not match the evidence and decay facts.")

    @property
    def evidence(self) -> DecayScore:
        """Return the flat score as the evidence value for generic callers."""

        return self

    @property
    def item(self) -> DecayScore:
        """Compatibility spelling for callers that model a score as an item."""

        return self

    @property
    def included(self) -> bool:
        """Return the retention decision: decay never excludes an item."""

        return True

    @property
    def below_display_floor(self) -> bool:
        """Whether the factor is below the configured presentation floor."""

        thresholds = self.thresholds
        if thresholds is None or thresholds.display_floor is None:
            return False
        factor = self.decay_factor
        assert factor is not None
        return factor < thresholds.display_floor

    @property
    def decay_state(self) -> str:
        """Return the display state without changing the domain evidence state."""

        if self.decay_factor == _ONE:
            return "fresh"
        if self.decay_factor == _ZERO:
            return "decayed"
        return "decaying"

    @property
    def pressure(self) -> Decimal:
        """Compatibility spelling for the weighted pressure contribution."""

        return self.pressure_contribution


@dataclass(frozen=True, slots=True)
class _Observation:
    event_date: date
    identity: tuple[UUID, UUID | None, str, str] | None = None
    source_id: str | None = None


def decay_factor(days_since_last_seen: int, rate: Decimal) -> Decimal:
    """Return the geometric decay factor for an evidence observation.

    ``rate`` is the fraction retained per day, not a percentage to subtract.
    Zero days always returns one, including when the configured rate is zero.
    Very small finite results are allowed to underflow to the specified floor
    of zero; no negative value can be returned.
    """

    _non_negative_integer(days_since_last_seen, "days_since_last_seen")
    configured_rate = _decimal(rate, "rate")
    if not _ZERO <= configured_rate <= _ONE:
        raise ValueError("rate must be between zero and one inclusive.")
    if days_since_last_seen == 0 or configured_rate == _ONE:
        return _ONE
    if configured_rate == _ZERO:
        return _ZERO

    try:
        # A higher precision than the process default avoids avoidable drift
        # when the factor is later multiplied into a Decimal contribution.
        with localcontext() as context:
            context.prec = max(context.prec, len(configured_rate.as_tuple().digits) + 12)
            result = configured_rate**days_since_last_seen
    except (DecimalException, InvalidOperation, OverflowError):
        # For a retention factor in [0, 1), an unrepresentably small result is
        # on the documented floor.  Other invalid inputs were rejected above.
        return _ZERO
    if not result.is_finite() or result < _ZERO:
        return _ZERO
    return min(_ONE, result)


def score_decay(
    item: EvidenceFacts | Mapping[str, object] | object,
    as_of: date,
    thresholds: object | None = None,
    observations: Iterable[object] | None = None,
    *,
    events: Iterable[object] | None = None,
    event_dates: Iterable[object] | None = None,
    rate: Decimal | None = None,
) -> DecayScore:
    """Apply decay to one evidence item and preserve its visibility.

    ``observations``/``events`` may contain signal-event facts, mappings,
    event-like objects, or bare calendar dates.  Dates are filtered at
    ``as_of`` and only observations belonging to this evidence identity are
    considered when identity information is present.  A new observation
    resets the factor at its observation date and creates an append-only
    same-state transition; the evidence state itself is owned by persistence,
    materiality and supersession stages.
    """

    if events is not None:
        if observations is not None:
            raise TypeError("Pass either observations or events, not both.")
        observations = events
    if event_dates is not None:
        if observations is not None:
            raise TypeError("Pass only one of observations, events, or event_dates.")
        observations = event_dates

    scoring_date = _calendar_date(as_of, "as_of")
    evidence = to_evidence_facts(item)
    configured = _resolve_thresholds(thresholds, rate)
    matching = _matching_observations(
        _normalise_observations(observations or ()), evidence, scoring_date
    )
    return _score_item(evidence, scoring_date, configured, matching)


def apply_decay(
    items: Iterable[EvidenceFacts | Mapping[str, object] | object] | EvidenceFacts,
    as_of: date,
    thresholds: object | None = None,
    observations: Iterable[object] | None = None,
    *,
    events: Iterable[object] | None = None,
    event_dates: Iterable[object] | None = None,
    rate: Decimal | None = None,
) -> list[DecayScore]:
    """Score every evidence item, retaining all items including fully decayed ones."""

    normalised_items = _normalise_items(items)
    if not normalised_items:
        return []
    if events is not None:
        if observations is not None:
            raise TypeError("Pass either observations or events, not both.")
        observations = events
    if event_dates is not None:
        if observations is not None:
            raise TypeError("Pass only one of observations, events, or event_dates.")
        observations = event_dates

    scoring_date = _calendar_date(as_of, "as_of")
    configured = _resolve_thresholds(thresholds, rate)
    all_observations = _normalise_observations(observations or ())
    if len(normalised_items) > 1 and any(
        observation.identity is None for observation in all_observations
    ):
        raise ValueError("Identity is required when applying observations to multiple items.")
    results: list[DecayScore] = []
    for item in normalised_items:
        matching = _matching_observations(all_observations, item, scoring_date)
        results.append(_score_item(item, scoring_date, configured, matching))
    return results


def pressure_contribution(
    materiality_pct: Decimal | None,
    counts_toward_pressure: bool,
    factor: Decimal,
) -> Decimal:
    """Return materiality weighted by decay, or zero for excluded evidence."""

    configured_factor = _decimal(factor, "factor")
    if not _ZERO <= configured_factor <= _ONE:
        raise ValueError("factor must be between zero and one inclusive.")
    if not isinstance(counts_toward_pressure, bool):
        raise TypeError("counts_toward_pressure must be a boolean.")
    if not counts_toward_pressure or materiality_pct is None:
        return _ZERO
    _validate_decimal(materiality_pct, "materiality_pct")
    if materiality_pct < _ZERO:
        raise ValueError("materiality_pct must not be negative.")
    return (materiality_pct / _PERCENT) * configured_factor


def _score_item(
    item: EvidenceFacts,
    as_of: date,
    thresholds: DecayThresholds,
    observations: Sequence[_Observation],
) -> DecayScore:
    if item.last_seen > as_of:
        raise ValueError("last_seen must not be after as_of.")

    is_terminal = item.state in {"superseded", "disputed"}
    usable_observations = () if is_terminal else observations
    observation_date = max(
        (observation.event_date for observation in usable_observations),
        default=item.last_seen,
    )
    if observation_date < item.last_seen:
        observation_date = item.last_seen
    new_observation = any(
        observation.event_date > item.last_seen
        or (
            observation.event_date == item.last_seen
            and observation.source_id is not None
            and observation.source_id not in item.source_event_ids
        )
        for observation in usable_observations
    )
    days = (as_of - observation_date).days
    factor = decay_factor(days, thresholds.decay_rate)
    transition = None
    if new_observation:
        transition = EvidenceTransitionFacts(
            evidence_id=item.id,
            from_state=item.state,
            to_state=item.state,
            occurred_on=observation_date,
            rule=_DECAY_RESET_RULE,
            threshold_snapshot_id=thresholds.threshold_snapshot_id,
        )

    source_event_ids = _merged_source_ids(
        item.source_event_ids,
        tuple(
            observation.source_id
            for observation in usable_observations
            if observation.source_id is not None
        ),
    )
    first_seen = min(item.first_seen, observation_date)
    last_seen = max(item.last_seen, observation_date)
    contribution = pressure_contribution(item.materiality_pct, item.counts_toward_pressure, factor)
    return DecayScore(
        id=item.id,
        borrower_id=item.borrower_id,
        facility_id=item.facility_id,
        family=item.family,
        evidence_type=item.evidence_type,
        first_seen=first_seen,
        last_seen=last_seen,
        persistence_days=item.persistence_days,
        event_count_window=item.event_count_window,
        materiality_pct=item.materiality_pct,
        decay_factor=factor,
        state=item.state,
        counts_toward_pressure=item.counts_toward_pressure,
        superseded_by_id=item.superseded_by_id,
        supersedes_id=item.supersedes_id,
        source_event_ids=source_event_ids,
        transition=transition,
        pressure_contribution=contribution,
        visible=True,
        days_since_last_seen=days,
        reset=new_observation and not is_terminal,
        thresholds=thresholds,
    )


def _resolve_thresholds(thresholds: object | None, rate: Decimal | None) -> DecayThresholds:
    if thresholds is not None and rate is not None:
        raise TypeError("Pass either thresholds or rate, not both.")
    if thresholds is None:
        if rate is None:
            raise TypeError("A configured decay rate is required.")
        return DecayThresholds(rate)
    if isinstance(thresholds, Decimal | int | str) and not isinstance(thresholds, bool):
        return DecayThresholds(cast(Decimal, thresholds))
    return (
        thresholds
        if isinstance(thresholds, DecayThresholds)
        else DecayThresholds.from_store(thresholds)
    )


def _normalise_items(
    values: Iterable[EvidenceFacts | Mapping[str, object] | object] | EvidenceFacts,
) -> tuple[EvidenceFacts, ...]:
    if isinstance(values, EvidenceFacts):
        return (values,)
    if isinstance(values, Mapping) and _looks_like_item(values):
        return (to_evidence_facts(values),)
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("items must be evidence facts or an iterable of evidence facts.")
    try:
        return tuple(to_evidence_facts(value) for value in values)
    except TypeError as error:
        raise TypeError("items must be evidence facts or an iterable of evidence facts.") from error


def _matching_observations(
    observations: Iterable[object],
    item: EvidenceFacts,
    as_of: date,
) -> tuple[_Observation, ...]:
    normalised = tuple(
        value if isinstance(value, _Observation) else _normalise_observation(value)
        for value in observations
    )
    known = tuple(value for value in normalised if value.event_date <= as_of)
    matched = tuple(value for value in known if value.identity in {None, item.identity})
    return matched


def _normalise_observations(values: Iterable[object]) -> tuple[_Observation, ...]:
    if isinstance(values, Mapping) or isinstance(values, date):
        return (_normalise_observation(values),)
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("observations must be dates or an iterable of observations.")
    try:
        return tuple(_normalise_observation(value) for value in values)
    except TypeError as error:
        raise TypeError("observations must be dates or an iterable of observations.") from error


def _normalise_observation(value: object) -> _Observation:
    if isinstance(value, _Observation):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return _Observation(event_date=value)
    try:
        event = SignalEventFacts.from_event(value)
    except (TypeError, ValueError, AttributeError, KeyError):
        if _has_identity_fields(value):
            raise
        event_date = _read(value, "event_date")
        return _Observation(
            event_date=_calendar_date(event_date, "event_date"),
            identity=_optional_identity(value),
            source_id=_source_id(value),
        )
    return _Observation(
        event_date=event.event_date,
        identity=event.identity,
        source_id=_source_id(event),
    )


def _optional_identity(value: object) -> tuple[UUID, UUID | None, str, str] | None:
    try:
        borrower_id = _read(value, "borrower_id")
        family = _read(value, "family")
        evidence_type = _read(value, "evidence_type", _read(value, "event_type"))
    except (TypeError, ValueError, KeyError):
        return None
    facility_id = _read(value, "facility_id", None)
    try:
        return SignalEventFacts(
            borrower_id=cast(UUID, borrower_id),
            facility_id=cast(UUID | None, facility_id),
            event_date=date.min,
            family=cast(str, family),
            evidence_type=cast(str, evidence_type),
        ).identity
    except (TypeError, ValueError):
        return None


def _has_identity_fields(value: object) -> bool:
    names = ("borrower_id", "facility_id", "family", "event_type", "evidence_type")
    return any(_read(value, name, None) is not None for name in names)


def _source_id(value: object) -> str | None:
    candidate = _read(value, "event_id", _read(value, "id", _read(value, "content_hash", None)))
    if candidate is None:
        return None
    if not isinstance(candidate, UUID | str):
        raise TypeError("event identifier must be a UUID or text.")
    return _bounded_source_id(str(candidate))


def _store_sections(store: object) -> tuple[object, ...]:
    sections: list[object] = []
    if isinstance(store, Mapping):
        if any(field_name in store for field_name in _RATE_FIELDS):
            sections.append(store)
        for name in _DECAY_SECTION_NAMES:
            value = store.get(name)
            if value is not None and value not in sections:
                sections.append(value)
        return tuple(sections)

    if any(hasattr(store, field_name) for field_name in _RATE_FIELDS):
        sections.append(store)
    for name in ("T3", "t3", "decay", "evidence"):
        value = getattr(store, name, None)
        if value is not None and value not in sections:
            sections.append(value)

    getter = getattr(store, "get", None)
    if callable(getter):
        for name in _DECAY_SECTION_NAMES:
            try:
                value = getter(name)
            except (KeyError, TypeError, AttributeError):
                continue
            if value is not None and value not in sections:
                sections.append(value)
                if _read_first(value, _RATE_FIELDS) is not None:
                    break
    return tuple(sections)


def _read_first(value: object, names: Sequence[str]) -> object | None:
    for name in names:
        candidate = _read(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _snapshot_id(store: object) -> UUID | None:
    candidate = _read(store, "threshold_snapshot_id", None)
    if candidate is None:
        candidate = _read(store, "snapshot_id", None)
        if callable(candidate):
            try:
                candidate = candidate()
            except (TypeError, ValueError):
                return None
    return candidate if isinstance(candidate, UUID) else None


def _read(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    marker = object()
    candidate = getattr(value, name, marker)
    return default if candidate is marker else candidate


def _looks_like_item(value: Mapping[str, object]) -> bool:
    return "borrower_id" in value and "evidence_type" in value and "last_seen" in value


def _merged_source_ids(*groups: Sequence[str]) -> tuple[str, ...]:
    values: set[str] = set()
    for group in groups:
        values.update(_bounded_source_id(value) for value in group)
    return tuple(sorted(values))


def _bounded_source_id(value: str) -> str:
    if not value or len(value) > _MAX_SOURCE_ID_LENGTH:
        raise ValueError("source_event_id must be non-blank text of at most 128 characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("source_event_id contains a control character.")
    return value


def _calendar_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a calendar date, not a datetime.")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO calendar date.") from error
    raise TypeError(f"{field_name} must be a calendar date.")


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool | float):
        raise TypeError(f"{field_name} must be an exact Decimal value.")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | str):
        try:
            result = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"{field_name} must be a valid Decimal value.") from error
    else:
        raise TypeError(f"{field_name} must be a Decimal value.")
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    return result


def _validate_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal.")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite.")


def _non_negative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


# Verb-first and noun-first names keep this stage discoverable while retaining
# one implementation and one transition rule.
calculate_decay_factor = decay_factor
decay_evidence = apply_decay
apply_evidence_decay = apply_decay
score_evidence_decay = apply_decay
weighted_pressure = pressure_contribution


__all__ = [
    "DecayScore",
    "DecayThresholds",
    "apply_decay",
    "apply_evidence_decay",
    "calculate_decay_factor",
    "decay_evidence",
    "decay_factor",
    "pressure_contribution",
    "score_decay",
    "score_evidence_decay",
    "weighted_pressure",
]
