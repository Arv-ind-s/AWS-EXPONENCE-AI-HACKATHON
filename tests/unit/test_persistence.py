"""Unit coverage for the T3 persistence scorer."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from covenant_radar.domain.signals.persistence import (
    PersistenceArm,
    PersistenceThresholds,
    consecutive_run_length,
    decide_persistence,
    rolling_event_count,
    score_persistence,
)

pytestmark = pytest.mark.unit

_AS_OF = date(2026, 8, 31)


class _ThresholdStore:
    def __init__(self, values: dict[str, int]) -> None:
        self._values = values
        self.requested: list[str] = []

    def get(self, name: str) -> dict[str, int]:
        self.requested.append(name)
        return self._values


def _default_store() -> _ThresholdStore:
    return _ThresholdStore(
        {
            "sustained_days": 14,
            "sustained_events": 3,
            "event_window_days": 30,
        }
    )


def _days_ending_at(length: int, *, end: date = _AS_OF) -> list[date]:
    return [end - timedelta(days=offset) for offset in range(length - 1, -1, -1)]


def test_exactly_fourteen_days_sustained() -> None:
    store = _default_store()

    result = score_persistence(_days_ending_at(14), _AS_OF, store)

    assert result.persistence_days == 14
    assert result.event_count_window == 14
    assert result.state == "sustained"
    assert result.firing_arm is PersistenceArm.SUSTAINED_DAYS
    assert result.rule == "T3.sustained_days"


def test_exactly_three_events_sustained() -> None:
    store = _default_store()
    events = [_AS_OF - timedelta(days=29), _AS_OF - timedelta(days=10), _AS_OF]

    result = score_persistence(events, _AS_OF, store)

    assert result.persistence_days == 1
    assert result.event_count_window == 3
    assert result.state == "sustained"
    assert result.firing_arm is PersistenceArm.SUSTAINED_EVENTS
    assert result.firing_rule == "T3.sustained_events"


def test_thirteen_days_two_events_transient() -> None:
    decision = decide_persistence(
        persistence_days=13,
        event_count_window=2,
        thresholds=_default_store(),
    )

    assert decision.state == "transient"
    assert decision.sustained is False
    assert decision.firing_arm is None
    assert decision.rule == "T3.neither_arm"


def test_single_day_gap_restarts_run() -> None:
    events = _days_ending_at(14)
    events.remove(_AS_OF - timedelta(days=7))

    assert consecutive_run_length(events, as_of=_AS_OF, window_days=30) == 7


def test_firing_arm_recorded() -> None:
    store = _default_store()
    result = score_persistence(
        [_AS_OF - timedelta(days=29), _AS_OF - timedelta(days=15), _AS_OF],
        _AS_OF,
        store,
    )

    assert result.firing_arm is PersistenceArm.SUSTAINED_EVENTS
    assert result.arm is PersistenceArm.SUSTAINED_EVENTS
    assert result.firing_rule == "T3.sustained_events"


def test_thresholds_read_from_store_not_literal() -> None:
    store = _ThresholdStore(
        {
            "sustained_days": 2,
            "sustained_events": 4,
            "event_window_days": 6,
        }
    )

    result = score_persistence([_AS_OF - timedelta(days=1), _AS_OF], _AS_OF, store)

    assert store.requested == ["T3"]
    assert result.thresholds == PersistenceThresholds(
        sustained_days=2,
        sustained_events=4,
        event_window_days=6,
    )
    assert result.firing_arm is PersistenceArm.SUSTAINED_DAYS


def test_events_outside_window_have_zero_measurements() -> None:
    events = [_AS_OF - timedelta(days=30), _AS_OF - timedelta(days=31)]

    assert consecutive_run_length(events, as_of=_AS_OF, window_days=30) == 0
    assert rolling_event_count(events, as_of=_AS_OF, window_days=30) == 0


def test_same_day_events_count_once() -> None:
    events = [
        {"event_date": _AS_OF, "event_id": "one"},
        {"event_date": _AS_OF, "event_id": "two"},
    ]

    result = score_persistence(events, _AS_OF, _default_store())

    assert result.persistence_days == 1
    assert result.event_count_window == 1


def test_future_events_do_not_enter_historical_score() -> None:
    result = score_persistence([_AS_OF + timedelta(days=1)], _AS_OF, _default_store())

    assert result.persistence_days == 0
    assert result.event_count_window == 0
    assert result.state == "transient"


def test_invalid_or_missing_thresholds_fail_closed() -> None:
    with pytest.raises(ValueError, match="sustained_events"):
        score_persistence([_AS_OF], _AS_OF, {"T3": {"sustained_days": 2, "event_window_days": 6}})

    with pytest.raises(ValueError, match="must be a positive integer"):
        score_persistence(
            [_AS_OF],
            _AS_OF,
            {
                "T3": {
                    "sustained_days": 0,
                    "sustained_events": 4,
                    "event_window_days": 6,
                }
            },
        )
