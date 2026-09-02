"""The synthetic feed: the documented default external signal source.

`spec §12.1`'s [OPEN-05]: the licensed news/industry/bureau subscription may
not be procured yet, so the product must still be demonstrable, testable and
evaluable without it. This generator is that default — the same role
`evaluation/reference_portfolio/signals.py` plays for internal behavioural
signals — and every reference-portfolio and evaluation run uses it so feed
polling, entity resolution and the review queue are all exercised before a
live subscription exists (`spec §R-30.e`).

Every value is derived from ``seed`` and an item's ordinal position through
SHA-256, never from process randomness or the wall clock, so the same seed
reproduces byte-identical output on every run.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from covenant_radar.ports.feed import FeedCapability, FeedItem

#: `R-30.e` calls for coverage of the feed families the synthetic generator
#: stands in for; bureau data arrives as structured records rather than
#: prose and is intentionally out of this generator's scope.
_CATEGORIES: Final[tuple[str, ...]] = ("industry", "news")

_ENTITY_POOL: Final[tuple[str, ...]] = (
    "Meridian Auto Components",
    "Harbourline Logistics",
    "Cascade Steel Works",
    "Sundale Retail Group",
    "Palisade Energy Partners",
    "Northgate Textiles",
    "Ironbridge Manufacturing",
    "Silverbrook Foods",
    "Wrenfield Chemicals",
    "Kestrel Freight Holdings",
)

_INDUSTRY_TEMPLATES: Final[tuple[str, ...]] = (
    "{entity} sector outlook downgraded amid demand softness",
    "{entity} peer group reports tightening credit conditions",
    "{entity} industry index signals rising input costs",
    "{entity} segment forecast revised on regulatory change",
)

_NEWS_TEMPLATES: Final[tuple[str, ...]] = (
    "{entity} announces restructuring of regional operations",
    "{entity} disputes supplier payment terms in a public filing",
    "{entity} reports delayed quarterly results",
    "{entity} faces regulatory inquiry over reporting practices",
)

_DEFAULT_START_DATE: Final[date] = date(2025, 1, 1)
_DEFAULT_DAYS = 365
_DEFAULT_ITEMS_PER_DAY = 2
_MINUTES_BETWEEN_SLOTS = 17


def _stable_unit_interval(seed: int, ordinal: int, salt: str) -> float:
    """A deterministic pseudo-random value in [0, 1) derived from seed and ordinal."""

    digest = hashlib.sha256(f"{seed}:{salt}:{ordinal}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _require_aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("SyntheticFeedAdapter.poll since must be a timezone-aware datetime.")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SyntheticFeedConfig:
    """Deterministic generation parameters for one synthetic feed instance."""

    seed: int
    source_reference: str = "synthetic-feed"
    start_date: date = _DEFAULT_START_DATE
    days: int = _DEFAULT_DAYS
    items_per_day: int = _DEFAULT_ITEMS_PER_DAY

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("SyntheticFeedConfig.seed must be an integer.")
        if not isinstance(self.source_reference, str) or not self.source_reference.strip():
            raise ValueError("SyntheticFeedConfig.source_reference must not be blank.")
        if isinstance(self.start_date, datetime) or not isinstance(self.start_date, date):
            raise TypeError("SyntheticFeedConfig.start_date must be a calendar date.")
        if isinstance(self.days, bool) or not isinstance(self.days, int) or self.days < 1:
            raise ValueError("SyntheticFeedConfig.days must be a positive integer.")
        if (
            isinstance(self.items_per_day, bool)
            or not isinstance(self.items_per_day, int)
            or self.items_per_day < 1
        ):
            raise ValueError("SyntheticFeedConfig.items_per_day must be a positive integer.")


class SyntheticFeedAdapter:
    """A deterministic, always-configured stand-in for a licensed feed.

    Implements `C-56`'s `FeedAdapter` protocol. Regenerating with the same
    `SyntheticFeedConfig.seed` reproduces the identical stream, so evaluation
    runs and tests are reproducible without a live subscription
    (`spec §R-30.e`).
    """

    def __init__(self, config: SyntheticFeedConfig) -> None:
        if not isinstance(config, SyntheticFeedConfig):
            raise TypeError("SyntheticFeedAdapter requires a SyntheticFeedConfig.")
        self._config = config

    @property
    def source_reference(self) -> str:
        return self._config.source_reference

    @property
    def capability(self) -> FeedCapability:
        return FeedCapability(configured=True, reason="synthetic generator always available")

    def poll(self, since: datetime | None) -> Iterator[FeedItem]:
        """Yield the deterministic stream, filtered to items after `since`."""

        if since is not None:
            since = _require_aware_utc(since)

        config = self._config
        ordinal = 0
        for day_number in range(config.days):
            event_date = config.start_date + timedelta(days=day_number)
            for slot in range(config.items_per_day):
                ordinal += 1
                published_at = datetime(
                    event_date.year, event_date.month, event_date.day, tzinfo=UTC
                ) + timedelta(minutes=slot * _MINUTES_BETWEEN_SLOTS)
                if since is not None and published_at <= since:
                    continue
                yield self._item(ordinal, published_at)

    def _item(self, ordinal: int, published_at: datetime) -> FeedItem:
        config = self._config
        category = _CATEGORIES[
            int(_stable_unit_interval(config.seed, ordinal, "category") * len(_CATEGORIES))
        ]
        entity = _ENTITY_POOL[
            int(_stable_unit_interval(config.seed, ordinal, "entity") * len(_ENTITY_POOL))
        ]
        templates = _INDUSTRY_TEMPLATES if category == "industry" else _NEWS_TEMPLATES
        template = templates[
            int(_stable_unit_interval(config.seed, ordinal, "template") * len(templates))
        ]
        title = template.format(entity=entity)
        return FeedItem(
            source=category,
            published_at=published_at,
            title=title,
            body=(
                f"{title}. Synthetic body text generated deterministically for "
                f"evaluation fixture #{ordinal} (seed {config.seed})."
            ),
            source_reference=f"{config.source_reference}:{ordinal}",
            entities=(entity,),
        )


__all__ = ["SyntheticFeedAdapter", "SyntheticFeedConfig"]
