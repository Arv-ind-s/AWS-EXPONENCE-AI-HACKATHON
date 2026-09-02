"""Integration checks for the T-128 synthetic feed and poll framework."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from covenant_radar.core.clock import FixedClock
from covenant_radar.ingestion.feeds.framework import DEFAULT_RETENTION_DAYS, FeedPollFramework
from covenant_radar.ingestion.feeds.synthetic import SyntheticFeedAdapter, SyntheticFeedConfig

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _adapter(seed: int = 7, *, days: int = 10, items_per_day: int = 3) -> SyntheticFeedAdapter:
    return SyntheticFeedAdapter(
        SyntheticFeedConfig(
            seed=seed,
            start_date=(_NOW - timedelta(days=days)).date(),
            days=days,
            items_per_day=items_per_day,
        )
    )


def test_deterministic_with_seed() -> None:
    first = list(_adapter(seed=42).poll(since=None))
    second = list(_adapter(seed=42).poll(since=None))

    assert first == second
    assert len(first) > 0

    different_seed = list(_adapter(seed=43).poll(since=None))
    assert different_seed != first


def test_stream_covers_industry_and_news() -> None:
    items = list(_adapter(seed=7, days=60, items_per_day=4).poll(since=None))

    sources = {item.source for item in items}
    assert "industry" in sources
    assert "news" in sources
    assert sources <= {"industry", "news"}
    assert all(item.entities for item in items)


def test_stale_items_ignored_with_count() -> None:
    stale_days = DEFAULT_RETENTION_DAYS + 30
    adapter = _adapter(seed=11, days=stale_days, items_per_day=1)
    clock = FixedClock(_NOW)
    framework = FeedPollFramework(clock=clock)

    outcome = framework.poll(adapter)

    assert outcome.error is None
    assert outcome.stale_count > 0
    horizon = _NOW - timedelta(days=DEFAULT_RETENTION_DAYS)
    assert all(item.published_at >= horizon for item in outcome.items)
    assert len(outcome.items) + outcome.stale_count == stale_days
