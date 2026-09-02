"""Scoped repository for `Memo` rows — `T-101`'s persistence half of
`spec §R-17.b`.

A row reaches this repository only after `services.memo.MemoGenerationService`
has a passed stage-7 draft in hand; a refused draft is never constructed as a
`Memo` in the first place, so there is no delete or update path to guard here
the way `TraceRepository` guards `trace_row` — the invariant is enforced by
what the caller never builds, not by what this adapter refuses to do.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.orm import Session

from covenant_radar.db.models.workflow import Memo
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for


class MemoRepository(RepositoryBase[Memo]):
    """Repository for one borrower's generated memos, scoped through it."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(session, Memo, ownership=ownership_path_for(Memo), audit=audit)

    def for_borrower(self, borrower_id: UUID, *, scope: Scope) -> tuple[Memo, ...]:
        """Return every in-scope memo for one borrower, newest first."""
        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        statement: Select[tuple[Memo]] = cast(Select[tuple[Memo]], self._scoped_select(scope))
        statement = statement.where(Memo.borrower_id == borrower_id).order_by(
            Memo.created_at.desc(), Memo.id.desc()
        )
        return tuple(self.session.execute(statement).scalars().all())


__all__ = ["MemoRepository"]
