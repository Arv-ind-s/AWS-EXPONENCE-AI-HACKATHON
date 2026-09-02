"""Scoped repository adapter for borrower master data."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import Select, or_
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for


class BorrowerRepository(RepositoryBase[Borrower]):
    """Repository whose every borrower read carries a portfolio predicate."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(session, Borrower, ownership=ownership_path_for(Borrower), audit=audit)

    def by_reference(self, reference: str, *, scope: Scope) -> Borrower | None:
        """Return one in-scope borrower by its stable human reference."""
        return self.find(scope=scope, reference=reference)

    def ordered(
        self,
        *,
        scope: Scope,
        active_only: bool | None = None,
        portfolio_id: UUID | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[Borrower]:
        """Return filtered borrowers in deterministic reference order."""
        if offset < 0:
            raise ValueError("Borrower list offset cannot be negative.")
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("Borrower list limit must be between 1 and 200.")
        if search is not None and len(search) > 100:
            raise ValueError("Borrower search must be at most 100 characters.")
        statement: Select[tuple[Borrower]] = cast(
            Select[tuple[Borrower]], self._scoped_select(scope)
        )
        if active_only is not None:
            statement = statement.where(Borrower.is_active.is_(active_only))
        if portfolio_id is not None:
            statement = statement.where(Borrower.portfolio_id == portfolio_id)
        if search and search.strip():
            term = _like_term(search)
            statement = statement.where(
                or_(
                    Borrower.reference.ilike(term, escape="\\"),
                    Borrower.legal_name.ilike(term, escape="\\"),
                )
            )
        statement = statement.order_by(Borrower.reference, Borrower.id).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return tuple(self.session.execute(statement).scalars().all())


def _like_term(value: str) -> str:
    escaped = value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
