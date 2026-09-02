"""The `UnitOfWork` port: the transaction boundary a service opens.

`covenant_radar.services` depends on this `Protocol`, never on
`covenant_radar.db.session` directly, so a use case can be exercised
against a fake unit of work with no database at all. The implementation
backed by a real SQLAlchemy session lives in `covenant_radar.db.session`.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable


@runtime_checkable
class UnitOfWork(Protocol):
    """One transaction, opened by a service and never by a repository.

    A clean exit from the enclosing `with` block commits exactly once; any
    exception raised inside it rolls the transaction back before
    propagating. Opening a `UnitOfWork` that is already open is a
    programming error, not a business outcome — it means one use case
    tried to span two transactions — and the implementation raises rather
    than silently nesting them.
    """

    def __enter__(self) -> Self:
        """Open the transaction and return self."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Commit on a clean exit, roll back on an exception, then close."""
        ...

    def commit(self) -> None:
        """Persist every change made inside this unit of work."""
        ...

    def rollback(self) -> None:
        """Discard every change made inside this unit of work."""
        ...
