"""Application service composing feed adapters into one poll and health view.

`spec §R-30.d`: an outage or an unconfigured feed must be visible on the
health view rather than merged silently into "no items today". This service
is the seam a scheduler job or an admin health screen calls; it owns adapter
registration and delegates the actual polling to `FeedPollFramework`, which
is where the per-feed isolation guarantee lives (`ingestion/feeds/framework.py`).
"""

from __future__ import annotations

from collections.abc import Iterable

from covenant_radar.ingestion.feeds.framework import FeedPollFramework, FeedPollReport
from covenant_radar.ports.feed import FeedAdapter

_MAX_SOURCE_REFERENCE_LENGTH = 500


class DuplicateFeedError(ValueError):
    """Two adapters were registered under the same source reference."""


class FeedIngestionService:
    """Register feed adapters and poll all of them through one framework."""

    def __init__(
        self,
        adapters: Iterable[FeedAdapter] = (),
        *,
        framework: FeedPollFramework | None = None,
    ) -> None:
        if framework is not None and not callable(getattr(framework, "poll_all", None)):
            raise TypeError("FeedIngestionService framework must expose poll_all().")
        self._framework = framework or FeedPollFramework()
        self._adapters: dict[str, FeedAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: FeedAdapter, *, replace: bool = False) -> None:
        """Register one feed adapter without disturbing already-registered feeds."""

        if not isinstance(adapter, FeedAdapter):
            raise TypeError("FeedIngestionService.register requires a FeedAdapter.")
        reference = adapter.source_reference
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("A feed adapter must expose a non-empty source_reference.")
        if len(reference) > _MAX_SOURCE_REFERENCE_LENGTH:
            raise ValueError(
                f"A feed source_reference exceeds {_MAX_SOURCE_REFERENCE_LENGTH} characters."
            )
        if reference in self._adapters and not replace:
            raise DuplicateFeedError(f"Feed {reference!r} is already registered.")
        self._adapters[reference] = adapter

    def source_references(self) -> tuple[str, ...]:
        """Return registered feed references in registration order."""

        return tuple(self._adapters)

    def poll_all(self) -> FeedPollReport:
        """Poll every registered feed once; one feed's failure isolates to it."""

        return self._framework.poll_all(self._adapters.values())

    def capability_report(self) -> dict[str, object]:
        """A JSON-ready summary for the admin health view.

        `spec §R-30.d`: absence and outage must both be visible, so this
        polls every feed rather than inspecting `adapter.capability` alone,
        which would miss a feed that is configured but currently failing.
        """

        return self.poll_all().as_dict()


__all__ = ["DuplicateFeedError", "FeedIngestionService"]
