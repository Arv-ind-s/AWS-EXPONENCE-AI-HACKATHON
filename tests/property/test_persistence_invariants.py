"""Property coverage for the monotone T3 persistence decision."""

from __future__ import annotations

from datetime import date, timedelta

from covenant_radar.domain.signals.persistence import PersistenceThresholds, score_persistence

_AS_OF = date(2026, 8, 31)
_THRESHOLDS = PersistenceThresholds(
    sustained_days=14,
    sustained_events=3,
    event_window_days=30,
)


def test_longer_run_never_less_sustained() -> None:
    previous = score_persistence([], _AS_OF, _THRESHOLDS)
    for length in range(1, 91):
        current = score_persistence(
            [_AS_OF - timedelta(days=offset) for offset in range(length)],
            _AS_OF,
            _THRESHOLDS,
        )

        assert not previous.sustained or current.sustained
        assert current.persistence_days >= previous.persistence_days
        previous = current


def test_more_events_never_less_sustained() -> None:
    previous = score_persistence([], _AS_OF, _THRESHOLDS)
    for count in range(1, 91):
        current = score_persistence(
            [_AS_OF - timedelta(days=offset * 2) for offset in range(count)],
            _AS_OF,
            _THRESHOLDS,
        )

        assert not previous.sustained or current.sustained
        assert current.event_count_window >= previous.event_count_window
        previous = current
