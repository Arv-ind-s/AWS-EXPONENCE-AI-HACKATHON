"""The external-signal boundary: one protocol every feed adapter implements.

`spec §12.1`'s [OPEN-05]: the licensed news/industry/bureau feed may not be
procured yet, so nothing downstream may depend on a live subscription
existing. `C-56` (`plan.md §6`) makes `FeedAdapter.poll(since)` the entire
contract — a synthetic generator and a live adapter are interchangeable
behind it, a feed's outage or absence is a reportable state rather than a
silent empty stream, and an item that cannot yet be tied to a borrower is
still handed onward for resolution to decide (`spec §R-30.b`), never dropped
here.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from covenant_radar.core.errors import ValidationError

_MAX_TITLE_LENGTH = 500
_MAX_BODY_LENGTH = 50_000
_MAX_SOURCE_REFERENCE_LENGTH = 500
_MAX_ENTITY_LENGTH = 300
_MAX_ENTITIES = 50

#: The closed set of feed families `R-30` names: news, industry and bureau
#: (credit-bureau) feeds. `T-129` builds one adapter per family; the
#: synthetic generator here stands in for all three until each is live.
FEED_SOURCES: tuple[str, ...] = ("news", "industry", "bureau")


class FeedItemError(ValidationError):
    """A feed item is malformed and cannot be represented as `FeedItem`."""

    code = "feed_item_error"


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One external item as read from a feed, before entity resolution.

    `entities` is exactly what the adapter extracted — names or identifiers,
    possibly empty — never a resolved borrower. `spec §R-30.b`: deciding
    whether an item is *about* a monitored borrower belongs to entity
    resolution (`T-130`), so an adapter must neither guess nor drop an item
    for lacking a confident match.
    """

    source: str
    published_at: datetime
    title: str
    body: str
    source_reference: str
    entities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or self.source not in FEED_SOURCES:
            raise FeedItemError(
                f"FeedItem.source must be one of {FEED_SOURCES}; received {self.source!r}.",
                field="feed_item.source",
            )
        if not isinstance(self.published_at, datetime):
            raise FeedItemError(
                "FeedItem.published_at must be a datetime.", field="feed_item.published_at"
            )
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise FeedItemError(
                "FeedItem.published_at must be timezone-aware.",
                field="feed_item.published_at",
            )
        title = _text(self.title, "title", _MAX_TITLE_LENGTH)
        if not isinstance(self.body, str):
            raise FeedItemError("FeedItem.body must be a string.", field="feed_item.body")
        if len(self.body) > _MAX_BODY_LENGTH:
            raise FeedItemError(
                f"FeedItem.body exceeds {_MAX_BODY_LENGTH} characters.", field="feed_item.body"
            )
        source_reference = _text(
            self.source_reference, "source_reference", _MAX_SOURCE_REFERENCE_LENGTH
        )
        if not isinstance(self.entities, tuple | list):
            raise FeedItemError(
                "FeedItem.entities must be a sequence of strings.", field="feed_item.entities"
            )
        entities = tuple(self.entities)
        if len(entities) > _MAX_ENTITIES:
            raise FeedItemError(
                f"FeedItem.entities cannot exceed {_MAX_ENTITIES} items.",
                field="feed_item.entities",
            )
        for entity in entities:
            if not isinstance(entity, str) or not entity.strip():
                raise FeedItemError(
                    "FeedItem.entities must be non-empty strings.", field="feed_item.entities"
                )
            if len(entity) > _MAX_ENTITY_LENGTH:
                raise FeedItemError(
                    f"A FeedItem entity exceeds {_MAX_ENTITY_LENGTH} characters.",
                    field="feed_item.entities",
                )
        object.__setattr__(self, "published_at", self.published_at.astimezone(UTC))
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "source_reference", source_reference)
        object.__setattr__(self, "entities", entities)


@dataclass(frozen=True, slots=True)
class FeedCapability:
    """Whether a feed is usable right now, and why.

    `spec §R-30.d`: an outage or an unconfigured feed degrades that evidence
    family alone and must be visible on the health view — never a silent
    empty stream indistinguishable from "nothing happened".
    """

    configured: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.configured, bool):
            raise TypeError("FeedCapability.configured must be a boolean.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("FeedCapability.reason must not be blank.")


@runtime_checkable
class FeedAdapter(Protocol):
    """`C-56`: one interface behind which every external signal source sits."""

    @property
    def source_reference(self) -> str:
        """Stable operator-facing reference for this feed."""
        ...

    @property
    def capability(self) -> FeedCapability:
        """Whether this feed is configured and can be polled right now."""
        ...

    def poll(self, since: datetime | None) -> Iterator[FeedItem]:
        """Yield items published after `since` (or all history when `None`).

        Called only when `capability.configured` is true. Raising is the
        adapter's signal that this poll cycle failed; the framework isolates
        the failure to this one feed.
        """
        ...


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeedItemError(
            f"FeedItem.{field} must be a non-empty string.", field=f"feed_item.{field}"
        )
    if len(value) > maximum:
        raise FeedItemError(
            f"FeedItem.{field} exceeds {maximum} characters.", field=f"feed_item.{field}"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise FeedItemError(
            f"FeedItem.{field} cannot contain control characters.", field=f"feed_item.{field}"
        )
    return value


__all__ = [
    "FEED_SOURCES",
    "FeedAdapter",
    "FeedCapability",
    "FeedItem",
    "FeedItemError",
]
