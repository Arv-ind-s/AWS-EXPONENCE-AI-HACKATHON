"""Persistence scoring for the evidence ledger.

This module implements the T3 decision independently of storage and signal
ingestion.  A scoring run receives the event dates for one evidence identity,
the date at which the run is being evaluated, and the approved threshold
store.  It returns the two measured persistence arms and the arm that caused
the decision.

The rolling window is a calendar window containing ``event_window_days``
dates, including both its first date and ``as_of``.  Multiple observations on
one date count once for the event-count arm.  A missing date terminates a
consecutive run; the next observed date starts a new run.  These conventions
are explicit here because silently choosing a different convention changes
which evidence reaches forecast pressure.

The module is deliberately free of framework and persistence imports.  The
threshold argument is a small protocol-by-shape rather than a concrete
configuration class so the domain remains independent of its adapters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final, cast

_T3_NAME: Final[str] = "T3"
_SUSTAINED_DAYS_FIELD: Final[str] = "sustained_days"
_SUSTAINED_EVENTS_FIELD: Final[str] = "sustained_events"
_EVENT_WINDOW_FIELD: Final[str] = "event_window_days"
_SUSTAINED_DAYS_RULE: Final[str] = "T3.sustained_days"
_SUSTAINED_EVENTS_RULE: Final[str] = "T3.sustained_events"
_TRANSIENT_RULE: Final[str] = "T3.neither_arm"


class PersistenceArm(StrEnum):
    """The two independently sufficient T3 persistence arms."""

    SUSTAINED_DAYS = _SUSTAINED_DAYS_FIELD
    SUSTAINED_EVENTS = _SUSTAINED_EVENTS_FIELD


@dataclass(frozen=True, slots=True)
class PersistenceThresholds:
    """Validated T3 values read from the active threshold store."""

    sustained_days: int
    sustained_events: int
    event_window_days: int

    def __post_init__(self) -> None:
        _positive_integer(self.sustained_days, _SUSTAINED_DAYS_FIELD)
        _positive_integer(self.sustained_events, _SUSTAINED_EVENTS_FIELD)
        _positive_integer(self.event_window_days, _EVENT_WINDOW_FIELD)
        if self.sustained_days > self.event_window_days:
            raise ValueError(
                f"{_T3_NAME} invariant: {_SUSTAINED_DAYS_FIELD} must not exceed "
                f"{_EVENT_WINDOW_FIELD}."
            )

    @classmethod
    def from_store(cls, store: object) -> PersistenceThresholds:
        """Read and validate T3 from a threshold store or threshold mapping.

        A configured store is required.  Missing values are rejected instead
        of being replaced with policy defaults, which keeps an offline replay
        faithful to the snapshot that made the decision.
        """

        section = _threshold_section(store)
        return cls(
            sustained_days=_threshold_integer(section, _SUSTAINED_DAYS_FIELD),
            sustained_events=_threshold_integer(section, _SUSTAINED_EVENTS_FIELD),
            event_window_days=_threshold_integer(section, _EVENT_WINDOW_FIELD),
        )


@dataclass(frozen=True, slots=True)
class PersistenceDecision:
    """A T3 decision from already measured persistence values."""

    persistence_days: int
    event_count_window: int
    sustained: bool
    firing_arm: PersistenceArm | None
    thresholds: PersistenceThresholds
    rule: str

    def __post_init__(self) -> None:
        _non_negative_integer(self.persistence_days, "persistence_days")
        _non_negative_integer(self.event_count_window, "event_count_window")
        if not isinstance(self.sustained, bool):
            raise TypeError("sustained must be a boolean.")
        if not isinstance(self.thresholds, PersistenceThresholds):
            raise TypeError("thresholds must be PersistenceThresholds.")
        if self.firing_arm is not None and not isinstance(self.firing_arm, PersistenceArm):
            try:
                object.__setattr__(self, "firing_arm", PersistenceArm(self.firing_arm))
            except ValueError as error:
                raise ValueError(f"Unknown persistence arm {self.firing_arm!r}.") from error
        expected_sustained = self.firing_arm is not None
        if self.sustained is not expected_sustained:
            raise ValueError("sustained must agree with whether a persistence arm fired.")
        expected_rule = _rule_for_arm(self.firing_arm)
        if self.rule != expected_rule:
            raise ValueError(f"rule must be {expected_rule!r} for the selected persistence arm.")

    @property
    def state(self) -> str:
        """Return the evidence state represented by this decision."""

        return "sustained" if self.sustained else "transient"

    @property
    def arm(self) -> PersistenceArm | None:
        """Compatibility spelling for callers that refer to the firing arm."""

        return self.firing_arm

    @property
    def firing_rule(self) -> str:
        """Return the named T3 rule used in an explainability record."""

        return self.rule


@dataclass(frozen=True, slots=True)
class PersistenceScore:
    """The explainable result of applying T3 to one evidence identity."""

    as_of: date
    persistence_days: int
    event_count_window: int
    sustained: bool
    firing_arm: PersistenceArm | None
    thresholds: PersistenceThresholds
    rule: str

    def __post_init__(self) -> None:
        normalized_as_of = _calendar_date(self.as_of, "as_of")
        _non_negative_integer(self.persistence_days, "persistence_days")
        _non_negative_integer(self.event_count_window, "event_count_window")
        if not isinstance(self.sustained, bool):
            raise TypeError("sustained must be a boolean.")
        if not isinstance(self.thresholds, PersistenceThresholds):
            raise TypeError("thresholds must be PersistenceThresholds.")
        if self.firing_arm is not None and not isinstance(self.firing_arm, PersistenceArm):
            try:
                object.__setattr__(self, "firing_arm", PersistenceArm(self.firing_arm))
            except ValueError as error:
                raise ValueError(f"Unknown persistence arm {self.firing_arm!r}.") from error
        expected_sustained = self.firing_arm is not None
        if self.sustained is not expected_sustained:
            raise ValueError("sustained must agree with whether a persistence arm fired.")
        expected_rule = _rule_for_arm(self.firing_arm)
        if self.rule != expected_rule:
            raise ValueError(f"rule must be {expected_rule!r} for the selected persistence arm.")
        object.__setattr__(self, "as_of", normalized_as_of)

    @property
    def state(self) -> str:
        """Return the evidence state represented by this score."""

        return "sustained" if self.sustained else "transient"

    @property
    def arm(self) -> PersistenceArm | None:
        """Compatibility spelling for callers that refer to the firing arm."""

        return self.firing_arm

    @property
    def firing_rule(self) -> str:
        """Return the named T3 rule used in an explainability record."""

        return self.rule


def consecutive_run_length(
    event_dates: Iterable[object],
    as_of: date,
    window_days: int | None = None,
) -> int:
    """Return the longest consecutive observed-date run as of ``as_of``.

    Future dates are excluded from a historical run.  If ``window_days`` is
    supplied, only dates in that inclusive calendar window participate; this
    prevents an old run from satisfying T3 after it has left the scoring
    horizon.
    """

    scoring_date = _calendar_date(as_of, "as_of")
    normalized_dates = _normalise_dates(event_dates)
    eligible_dates = _dates_in_window(
        normalized_dates,
        as_of=scoring_date,
        window_days=window_days,
    )
    return _longest_run(eligible_dates)


def rolling_event_count(
    event_dates: Iterable[object],
    as_of: date,
    window_days: int,
) -> int:
    """Return distinct observed event dates in the inclusive rolling window."""

    scoring_date = _calendar_date(as_of, "as_of")
    normalized_dates = _normalise_dates(event_dates)
    return len(
        _dates_in_window(
            normalized_dates,
            as_of=scoring_date,
            window_days=window_days,
        )
    )


def score_persistence(
    events: Iterable[object],
    as_of: date,
    thresholds: object,
) -> PersistenceScore:
    """Apply the configured T3 arms to one evidence identity.

    ``events`` may contain calendar dates, mappings with an ``event_date``
    field, or objects exposing that field.  Dates on the same day are
    de-duplicated for both the run and rolling-count measurements.  The
    consecutive-days arm is evaluated first when both arms are true, giving
    the result a deterministic single explanation while the raw measurements
    preserve enough information for a reader to see the other arm as well.
    """

    scoring_date = _calendar_date(as_of, "as_of")
    configured = PersistenceThresholds.from_store(thresholds)
    normalized_dates = _normalise_dates(events)
    eligible_dates = _dates_in_window(
        normalized_dates,
        as_of=scoring_date,
        window_days=configured.event_window_days,
    )
    run_length = _longest_run(eligible_dates)
    event_count = len(eligible_dates)
    decision = decide_persistence(run_length, event_count, configured)

    return PersistenceScore(
        as_of=scoring_date,
        persistence_days=run_length,
        event_count_window=event_count,
        sustained=decision.sustained,
        firing_arm=decision.firing_arm,
        thresholds=configured,
        rule=decision.rule,
    )


def decide_persistence(
    persistence_days: int,
    event_count_window: int,
    thresholds: object,
) -> PersistenceDecision:
    """Apply T3 to measured run and rolling-count values.

    Keeping this decision separate from event-date measurement makes the
    boundary rule directly testable and lets a caller that already maintains
    evidence measurements reuse exactly the same decision logic.
    """

    configured = (
        thresholds
        if isinstance(thresholds, PersistenceThresholds)
        else PersistenceThresholds.from_store(thresholds)
    )
    _non_negative_integer(persistence_days, "persistence_days")
    _non_negative_integer(event_count_window, "event_count_window")
    if persistence_days >= configured.sustained_days:
        firing_arm: PersistenceArm | None = PersistenceArm.SUSTAINED_DAYS
    elif event_count_window >= configured.sustained_events:
        firing_arm = PersistenceArm.SUSTAINED_EVENTS
    else:
        firing_arm = None
    return PersistenceDecision(
        persistence_days=persistence_days,
        event_count_window=event_count_window,
        sustained=firing_arm is not None,
        firing_arm=firing_arm,
        thresholds=configured,
        rule=_rule_for_arm(firing_arm),
    )


# These descriptive aliases keep the domain operation discoverable to callers
# that use noun-first or verb-first terminology without creating another rule.
persistence_score = score_persistence
classify_persistence = score_persistence
compute_run_length = consecutive_run_length
count_events_in_window = rolling_event_count


def _threshold_section(store: object) -> object:
    if isinstance(store, PersistenceThresholds):
        return {
            _SUSTAINED_DAYS_FIELD: store.sustained_days,
            _SUSTAINED_EVENTS_FIELD: store.sustained_events,
            _EVENT_WINDOW_FIELD: store.event_window_days,
        }

    if isinstance(store, Mapping):
        if _T3_NAME in store:
            return store[_T3_NAME]
        return store

    getter = getattr(store, "get", None)
    if callable(getter):
        section = getter(_T3_NAME)
        if section is None:
            raise ValueError("The threshold store has no active T3 section.")
        return section

    for name in (_T3_NAME, "t3", "persistence"):
        section = getattr(store, name, None)
        if section is not None:
            return section
    raise TypeError("thresholds must be a T3 mapping or expose get('T3').")


def _threshold_integer(section: object, field_name: str) -> int:
    if isinstance(section, Mapping):
        if field_name not in section:
            raise ValueError(f"T3 threshold is missing {field_name!r}.")
        value = section[field_name]
    else:
        marker = object()
        value = getattr(section, field_name, marker)
        if value is marker:
            raise ValueError(f"T3 threshold is missing {field_name!r}.")
    _positive_integer(value, f"T3.{field_name}")
    return cast(int, value)


def _dates_in_window(
    dates: set[date],
    *,
    as_of: date,
    window_days: int | None,
) -> set[date]:
    if window_days is None:
        return {event_date for event_date in dates if event_date <= as_of}
    _positive_integer(window_days, "window_days")
    try:
        first_date = as_of - timedelta(days=window_days - 1)
    except OverflowError as error:
        raise ValueError("window_days is too large for calendar arithmetic.") from error
    return {event_date for event_date in dates if first_date <= event_date <= as_of}


def _longest_run(dates: set[date]) -> int:
    if not dates:
        return 0
    ordinals = sorted(event_date.toordinal() for event_date in dates)
    longest = 1
    current = 1
    for previous, candidate in zip(ordinals, ordinals[1:], strict=False):
        if candidate == previous + 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
    return longest


def _normalise_dates(values: Iterable[object]) -> set[date]:
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("event dates must be an iterable of dates, not text.")
    try:
        return {_event_date(value) for value in values}
    except TypeError as error:
        raise TypeError("events must be an iterable of event dates or event records.") from error


def _event_date(value: object) -> date:
    if isinstance(value, Mapping):
        marker = object()
        candidate = value.get("event_date", marker)
        if candidate is marker:
            raise ValueError("An event mapping is missing 'event_date'.")
    elif isinstance(value, date):
        candidate = value
    else:
        marker = object()
        candidate = getattr(value, "event_date", marker)
        if candidate is marker:
            raise TypeError("Each event must be a calendar date or expose event_date.")
    return _calendar_date(candidate, "event_date")


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


def _rule_for_arm(arm: PersistenceArm | None) -> str:
    if arm is PersistenceArm.SUSTAINED_DAYS:
        return _SUSTAINED_DAYS_RULE
    if arm is PersistenceArm.SUSTAINED_EVENTS:
        return _SUSTAINED_EVENTS_RULE
    return _TRANSIENT_RULE


def _positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _non_negative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


__all__ = [
    "PersistenceArm",
    "PersistenceDecision",
    "PersistenceScore",
    "PersistenceThresholds",
    "classify_persistence",
    "compute_run_length",
    "consecutive_run_length",
    "count_events_in_window",
    "decide_persistence",
    "persistence_score",
    "rolling_event_count",
    "score_persistence",
]
