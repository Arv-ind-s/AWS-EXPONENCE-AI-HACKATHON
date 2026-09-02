"""Poll framework for `C-56` feed adapters: watermarks, retention and isolation.

`spec §R-30.d`: a feed that fails or is not configured degrades that
evidence family alone; every other configured feed continues to poll. This
mirrors the per-source isolation `SignalSourceRegistry` already gives
internal signal sources (`ingestion/signals/sources.py`), with one
difference: a feed's failure discards that whole cycle's items rather than
keeping whatever arrived before the failure — a story half-read from a wire
is not a story a downstream match should ever see, so the watermark is left
exactly where it was and the next successful poll re-reads from that point.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.ports.feed import FeedAdapter, FeedItem

#: Items published before this many days ago are out of scope for ingestion
#: regardless of watermark position — a bounded window, not an unbounded
#: backfill, the same posture `spec §12.1` takes for every external source.
DEFAULT_RETENTION_DAYS = 90

_MAX_SOURCE_REFERENCE_LENGTH = 500
_MAX_ERROR_LENGTH = 2000


@runtime_checkable
class FeedWatermarkStore(Protocol):
    """Read and advance the last successfully processed instant per feed."""

    def get(self, source_reference: str) -> datetime | None:
        """Return the current watermark for `source_reference`."""

    def advance(self, source_reference: str, watermark: datetime) -> datetime:
        """Advance a feed's watermark without allowing regression."""


class InMemoryFeedWatermarkStore:
    """Thread-safe monotonic per-feed watermark store for local runs and tests.

    ``advance`` is compare-and-set-like: the largest instant observed wins,
    so a replayed poll cannot make an already-processed item look new.
    """

    def __init__(self) -> None:
        self._watermarks: dict[str, datetime] = {}
        self._lock = RLock()

    def get(self, source_reference: str) -> datetime | None:
        _require_reference(source_reference)
        with self._lock:
            return self._watermarks.get(source_reference)

    def advance(self, source_reference: str, watermark: datetime) -> datetime:
        _require_reference(source_reference)
        _require_aware(watermark, "watermark")
        with self._lock:
            current = self._watermarks.get(source_reference)
            if current is None or watermark > current:
                self._watermarks[source_reference] = watermark
                return watermark
            return current

    @property
    def watermarks(self) -> Mapping[str, datetime]:
        """Return an immutable snapshot for diagnostics and tests."""

        with self._lock:
            return MappingProxyType(dict(self._watermarks))


@dataclass(frozen=True, slots=True)
class FeedSourceOutcome:
    """The result of polling one feed exactly once."""

    source_reference: str
    configured: bool
    reason: str | None
    items: tuple[FeedItem, ...]
    stale_count: int
    error: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.source_reference, str) or not self.source_reference.strip():
            raise ValueError("FeedSourceOutcome.source_reference must not be blank.")
        if not isinstance(self.configured, bool):
            raise TypeError("FeedSourceOutcome.configured must be a boolean.")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("FeedSourceOutcome.reason must be a string or None.")
        if not self.configured:
            if self.reason is None or not self.reason.strip():
                raise ValueError("An unconfigured feed outcome must carry a non-blank reason.")
            if self.items:
                raise ValueError("An unconfigured feed cannot report items.")
            if self.error is not None:
                raise ValueError("An unconfigured feed has no poll error; it was never polled.")
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, FeedItem) for item in self.items
        ):
            raise TypeError("FeedSourceOutcome.items must be a tuple of FeedItem.")
        if (
            isinstance(self.stale_count, bool)
            or not isinstance(self.stale_count, int)
            or self.stale_count < 0
        ):
            raise ValueError("FeedSourceOutcome.stale_count must be a non-negative integer.")
        if self.error is not None:
            if not isinstance(self.error, str) or not self.error.strip():
                raise ValueError("FeedSourceOutcome.error must be a non-blank string or None.")
            if self.items:
                raise ValueError(
                    "A degraded poll cannot report items; a partial read is discarded."
                )

    @property
    def degraded(self) -> bool:
        """Whether this feed's poll cycle failed (as opposed to being unconfigured)."""

        return self.error is not None

    @property
    def absent(self) -> bool:
        """Whether this feed is not configured at all."""

        return not self.configured

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready summary for the admin health view."""

        return {
            "source_reference": self.source_reference,
            "configured": self.configured,
            "reason": self.reason,
            "item_count": len(self.items),
            "stale_count": self.stale_count,
            "error": self.error,
            "degraded": self.degraded,
            "absent": self.absent,
        }


@dataclass(frozen=True, slots=True)
class FeedPollReport:
    """The combined outcome of polling every registered feed once."""

    outcomes: tuple[FeedSourceOutcome, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.outcomes, tuple) or not all(
            isinstance(outcome, FeedSourceOutcome) for outcome in self.outcomes
        ):
            raise TypeError("FeedPollReport.outcomes must be a tuple of FeedSourceOutcome.")
        references = [outcome.source_reference for outcome in self.outcomes]
        if len(references) != len(set(references)):
            raise ValueError("FeedPollReport cannot contain duplicate source references.")

    @property
    def items(self) -> tuple[FeedItem, ...]:
        """Every item accepted across every healthy, configured feed."""

        return tuple(item for outcome in self.outcomes for item in outcome.items)

    @property
    def stale_count(self) -> int:
        return sum(outcome.stale_count for outcome in self.outcomes)

    @property
    def degraded_sources(self) -> tuple[str, ...]:
        """References of feeds whose poll cycle failed this round."""

        return tuple(outcome.source_reference for outcome in self.outcomes if outcome.degraded)

    @property
    def absent_sources(self) -> tuple[FeedSourceOutcome, ...]:
        """Outcomes for feeds that are not configured at all."""

        return tuple(outcome for outcome in self.outcomes if outcome.absent)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready summary for the admin health view."""

        return {
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "item_count": len(self.items),
            "stale_count": self.stale_count,
            "degraded_sources": list(self.degraded_sources),
            "absent_sources": [outcome.source_reference for outcome in self.absent_sources],
        }


class FeedPollFramework:
    """Poll every configured `FeedAdapter` once, isolating failures per feed."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        watermark_store: FeedWatermarkStore | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("FeedPollFramework clock must expose now().")
        if watermark_store is not None and not isinstance(watermark_store, FeedWatermarkStore):
            raise TypeError("FeedPollFramework watermark_store must expose get() and advance().")
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 1
        ):
            raise ValueError("FeedPollFramework retention_days must be a positive integer.")
        self._clock = clock or SystemClock()
        self._watermark_store = watermark_store or InMemoryFeedWatermarkStore()
        self._retention = timedelta(days=retention_days)

    @property
    def watermark_store(self) -> FeedWatermarkStore:
        return self._watermark_store

    def poll(self, adapter: FeedAdapter) -> FeedSourceOutcome:
        """Poll one feed exactly once; that feed's own failure never raises."""

        if not isinstance(adapter, FeedAdapter):
            raise TypeError("FeedPollFramework.poll requires a FeedAdapter.")
        reference = _require_reference(adapter.source_reference)
        capability = adapter.capability
        if not capability.configured:
            return FeedSourceOutcome(
                source_reference=reference,
                configured=False,
                reason=capability.reason,
                items=(),
                stale_count=0,
                error=None,
            )

        since = self._watermark_store.get(reference)
        horizon = self._clock.now() - self._retention
        items: list[FeedItem] = []
        stale_count = 0
        error: str | None = None
        try:
            for item in adapter.poll(since):
                if not isinstance(item, FeedItem):
                    raise TypeError(
                        f"Feed {reference!r} yielded {type(item).__name__}, not FeedItem."
                    )
                if item.published_at < horizon:
                    stale_count += 1
                    continue
                items.append(item)
        except Exception as raised:
            error = str(raised)[:_MAX_ERROR_LENGTH] or type(raised).__name__

        if error is not None:
            return FeedSourceOutcome(
                source_reference=reference,
                configured=True,
                reason=None,
                items=(),
                stale_count=0,
                error=error,
            )

        if items:
            newest = max(item.published_at for item in items)
            self._watermark_store.advance(reference, newest)

        return FeedSourceOutcome(
            source_reference=reference,
            configured=True,
            reason=None,
            items=tuple(items),
            stale_count=stale_count,
            error=None,
        )

    def poll_all(self, adapters: Iterable[FeedAdapter]) -> FeedPollReport:
        """Poll every adapter once; one feed's failure never blocks another."""

        return FeedPollReport(outcomes=tuple(self.poll(adapter) for adapter in adapters))


def _require_reference(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A feed source_reference must be a non-empty string.")
    if len(value) > _MAX_SOURCE_REFERENCE_LENGTH:
        raise ValueError(
            f"A feed source_reference exceeds {_MAX_SOURCE_REFERENCE_LENGTH} characters."
        )
    return value


def _require_aware(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime.")


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "FeedPollFramework",
    "FeedPollReport",
    "FeedSourceOutcome",
    "FeedWatermarkStore",
    "InMemoryFeedWatermarkStore",
]
