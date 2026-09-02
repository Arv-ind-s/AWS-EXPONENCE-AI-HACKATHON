"""Unit tests for the T-128 `C-56` feed protocol and per-feed isolation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from covenant_radar.ingestion.feeds.framework import FeedPollFramework
from covenant_radar.ports.feed import FeedAdapter, FeedCapability, FeedItem, FeedItemError

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class _StubAdapter:
    """A minimal `FeedAdapter` for isolation tests."""

    def __init__(
        self,
        reference: str,
        *,
        configured: bool = True,
        reason: str = "configured",
        items: tuple[FeedItem, ...] = (),
        raises: Exception | None = None,
    ) -> None:
        self._reference = reference
        self._configured = configured
        self._reason = reason
        self._items = items
        self._raises = raises

    @property
    def source_reference(self) -> str:
        return self._reference

    @property
    def capability(self) -> FeedCapability:
        return FeedCapability(configured=self._configured, reason=self._reason)

    def poll(self, since: datetime | None) -> Iterator[FeedItem]:
        if self._raises is not None:
            raise self._raises
        yield from self._items


def _item(entities: tuple[str, ...] = ("Meridian Auto",)) -> FeedItem:
    return FeedItem(
        source="news",
        published_at=_NOW,
        title="Meridian Auto disputes supplier payment terms",
        body="Body text.",
        source_reference="fixture:1",
        entities=entities,
    )


def test_item_shape() -> None:
    item = _item()
    assert item.source == "news"
    assert item.published_at == _NOW
    assert item.title
    assert item.body
    assert item.source_reference == "fixture:1"
    assert item.entities == ("Meridian Auto",)

    # An item with no resolvable entity is still a valid item: resolution
    # decides, the adapter never drops it (`spec §R-30.b`).
    unresolved = _item(entities=())
    assert unresolved.entities == ()

    with pytest.raises(FeedItemError, match="source"):
        FeedItem(
            source="unknown-family",
            published_at=_NOW,
            title="x",
            body="x",
            source_reference="x",
        )
    with pytest.raises(FeedItemError, match="published_at"):
        FeedItem(
            source="news",
            published_at=datetime(2026, 8, 30, 12, 0),  # noqa: DTZ001 -- the refusal itself is under test
            title="x",
            body="x",
            source_reference="x",
        )
    with pytest.raises(FeedItemError, match="title"):
        FeedItem(
            source="news",
            published_at=_NOW,
            title="   ",
            body="x",
            source_reference="x",
        )


def test_unconfigured_feed_reports_absence_with_reason() -> None:
    adapter = _StubAdapter(
        "bureau-feed",
        configured=False,
        reason="No licensed bureau subscription is configured.",
    )
    framework = FeedPollFramework()

    outcome = framework.poll(adapter)

    assert outcome.configured is False
    assert outcome.absent is True
    assert outcome.reason == "No licensed bureau subscription is configured."
    # Absence is a distinct, visible state — never a silently empty stream.
    assert outcome.items == ()
    assert outcome.error is None
    assert outcome.degraded is False


def test_adapter_failure_isolated() -> None:
    healthy = _StubAdapter("healthy-feed", items=(_item(),))
    failing = _StubAdapter("failing-feed", raises=RuntimeError("upstream timed out"))

    framework = FeedPollFramework()
    report = framework.poll_all([failing, healthy])

    failing_outcome, healthy_outcome = report.outcomes
    assert failing_outcome.source_reference == "failing-feed"
    assert failing_outcome.degraded is True
    assert failing_outcome.error is not None
    assert "upstream timed out" in failing_outcome.error
    assert failing_outcome.items == ()

    # The other feed is completely unaffected.
    assert healthy_outcome.source_reference == "healthy-feed"
    assert healthy_outcome.degraded is False
    assert len(healthy_outcome.items) == 1
    assert report.degraded_sources == ("failing-feed",)
    assert report.items == healthy_outcome.items
    assert isinstance(healthy, FeedAdapter)
