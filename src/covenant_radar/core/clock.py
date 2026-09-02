"""The injectable clock: the one sanctioned source of the current instant.

Nothing in the tree calls `datetime.now()` directly — a lint rule enforces
it. Services and domain code instead take a `Clock` so tests can supply a
`FixedClock` and get deterministic, reproducible instants instead of racing
the wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A source of the current instant."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock, backed by the operating system."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A deterministic clock for tests: fixed unless explicitly advanced."""

    __slots__ = ("_instant",)

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware instant.")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def advance(self, delta: timedelta) -> None:
        """Move the fixed instant forward (or, with a negative delta, back)."""
        self._instant = self._instant + delta

    def set(self, instant: datetime) -> None:
        """Replace the fixed instant outright."""
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware instant.")
        self._instant = instant.astimezone(UTC)
