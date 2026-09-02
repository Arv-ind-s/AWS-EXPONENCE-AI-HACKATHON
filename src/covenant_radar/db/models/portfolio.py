"""`portfolio`: the branch/desk tree, addressed by a materialised path so
`T-016`'s scope predicate is one indexed prefix match (``LIKE 'prefix%'``)
rather than a recursive query (`plan.md §5.1`).

**Path format.** Each node contributes its own id, as 32 lowercase hex
characters, followed by a trailing ``/``. A root's path is just its own
segment; a child's path is its parent's path with its own segment
appended. The trailing separator on every segment is what makes prefix
matching exact — without it, portfolio id ``"1"`` would wrongly match as a
prefix of id ``"10"``; hex ids collide the same way decimal ones would.

**Maintenance on insert and move.** Nothing computes the path implicitly
through an ORM event: `Portfolio.create` builds it from the parent
supplied at construction, and `Portfolio.move_to` rebuilds it — and every
already-loaded descendant's — when a subtree is re-parented. Both are
pure Python operations with no database round trip of their own, because
every portfolio id is minted client-side (`core.ids.new_id`) and is
therefore already known before the row is ever inserted.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.core.errors import ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID

_CODE_MAX_LENGTH = 64
_NAME_MAX_LENGTH = 200
_BRANCH_CODE_MAX_LENGTH = 32

#: Path segments are a 32-character hex id plus its trailing separator.
_PATH_SEGMENT_LENGTH: Final[int] = 33

#: No config plumbing carries this today (`config/settings.py` has no
#: field for it); it is a structural limit on the tree's shape, not a
#: business threshold, so it does not belong in `T-012`'s threshold
#: store. Promoting it to a configured setting later needs no change to
#: the path format itself.
MAX_PORTFOLIO_DEPTH: Final[int] = 20

_PATH_MAX_LENGTH = _PATH_SEGMENT_LENGTH * MAX_PORTFOLIO_DEPTH


def _build_path(parent_path: str | None, portfolio_id: UUID) -> str:
    """Return the path for `portfolio_id` given its parent's path (or
    `None` for a root), refusing one that would exceed the configured
    maximum depth rather than silently truncating it."""
    prefix = parent_path or ""
    candidate = f"{prefix}{portfolio_id.hex}/"
    depth = candidate.count("/")
    if depth > MAX_PORTFOLIO_DEPTH:
        raise ValidationError(
            f"Portfolio path would be {depth} levels deep; "
            f"the maximum is {MAX_PORTFOLIO_DEPTH}.",
            field="portfolio.path",
        )
    return candidate


class Portfolio(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A node in the branch/desk tree: the organisation's own structure,
    not a customer entity."""

    __tablename__ = "portfolio"

    code: Mapped[str] = mapped_column(String(_CODE_MAX_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(_NAME_MAX_LENGTH), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("portfolio.id", ondelete="RESTRICT"), nullable=True
    )
    branch_code: Mapped[str | None] = mapped_column(
        String(_BRANCH_CODE_MAX_LENGTH), nullable=True
    )
    path: Mapped[str] = mapped_column(String(_PATH_MAX_LENGTH), nullable=False, index=True)

    @classmethod
    def create(
        cls,
        *,
        code: str,
        name: str,
        created_at: datetime,
        updated_at: datetime,
        request_id: str,
        parent: Portfolio | None = None,
        branch_code: str | None = None,
        created_by_id: UUID | None = None,
        updated_by_id: UUID | None = None,
    ) -> Portfolio:
        """Construct a new portfolio with its materialised path already
        computed from `parent`, so the row is insert-ready and the path
        is never a second step a caller can forget."""
        portfolio_id = new_id()
        path = _build_path(parent.path if parent is not None else None, portfolio_id)
        return cls(
            id=portfolio_id,
            code=code,
            name=name,
            parent_id=parent.id if parent is not None else None,
            branch_code=branch_code,
            path=path,
            created_at=created_at,
            updated_at=updated_at,
            request_id=request_id,
            created_by_id=created_by_id,
            updated_by_id=updated_by_id,
        )

    def move_to(
        self, new_parent: Portfolio | None, *, descendants: Sequence[Portfolio] = ()
    ) -> None:
        """Re-parent this portfolio under `new_parent`, updating its own
        path and every already-loaded descendant's path to match.

        `descendants` must be every currently-loaded descendant of this
        portfolio (the caller — a repository or service with access to
        the session — is responsible for fetching them by path prefix);
        a descendant not among them would be left with a stale path that
        no longer resolves under its ancestor's new location.
        """
        old_prefix = self.path
        new_prefix = _build_path(
            new_parent.path if new_parent is not None else None, self.id
        )

        rewritten: list[tuple[Portfolio, str]] = []
        for descendant in descendants:
            if not descendant.path.startswith(old_prefix):
                raise ValueError(
                    f"Portfolio {descendant.id} is not a descendant of {self.id}; "
                    "its path does not start with the portfolio being moved."
                )
            suffix = descendant.path[len(old_prefix) :]
            candidate = f"{new_prefix}{suffix}"
            depth = candidate.count("/")
            if depth > MAX_PORTFOLIO_DEPTH:
                raise ValidationError(
                    f"Moving {self.id} would put descendant {descendant.id} at "
                    f"{depth} levels deep; the maximum is {MAX_PORTFOLIO_DEPTH}.",
                    field="portfolio.path",
                )
            rewritten.append((descendant, candidate))

        self.parent_id = new_parent.id if new_parent is not None else None
        self.path = new_prefix
        for descendant, candidate in rewritten:
            descendant.path = candidate
