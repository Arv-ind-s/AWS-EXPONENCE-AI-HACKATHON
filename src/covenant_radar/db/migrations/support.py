"""The one thing every migration script and `radarctl migrate` share:
how a revision declares itself irreversible.

`plan.md §5`'s migration rule is "every schema change is an Alembic
revision with an upgrade and a downgrade, **or an explicit irreversibility
note with its reason**." A revision that cannot be undone calls
`irreversible` from its own `downgrade()` instead of leaving the function
empty or silently doing nothing — `radarctl migrate downgrade`
(`cli.py`) catches `IrreversibleMigrationError` and reports the reason
plainly rather than letting a raw traceback stand in for it.
"""

from __future__ import annotations


class IrreversibleMigrationError(RuntimeError):
    """Raised by a migration's `downgrade()` that refuses to run."""


def irreversible(revision: str, reason: str) -> None:
    """Refuse to downgrade `revision`, naming why.

    Called from inside a migration's `downgrade()` in place of the
    operations that would undo `upgrade()`, for a change that has no safe
    or meaningful reverse (a destructive data migration, a dropped
    column whose values cannot be reconstructed, and the like).
    """
    raise IrreversibleMigrationError(
        f"Revision {revision} is irreversible and cannot be downgraded: {reason}"
    )
