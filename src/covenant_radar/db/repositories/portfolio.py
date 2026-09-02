"""Scoped repository adapter for the portfolio hierarchy."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.orm import Session

from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for


class PortfolioRepository(RepositoryBase[Portfolio]):
    """Repository for materialised-path portfolio rows."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(session, Portfolio, ownership=ownership_path_for(Portfolio), audit=audit)

    def by_id(self, portfolio_id: UUID, *, scope: Scope) -> Portfolio | None:
        """Return one in-scope portfolio by UUID."""
        return self.get(portfolio_id, scope=scope)

    def by_code(self, code: str, *, scope: Scope) -> Portfolio | None:
        """Return one in-scope portfolio by its code."""
        return self.find(scope=scope, code=code)

    def ordered(
        self, *, scope: Scope, limit: int | None = None, offset: int = 0
    ) -> Sequence[Portfolio]:
        """Return portfolios in stable code/path order."""
        if offset < 0:
            raise ValueError("Portfolio list offset cannot be negative.")
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("Portfolio list limit must be between 1 and 200.")
        statement: Select[tuple[Portfolio]] = cast(
            Select[tuple[Portfolio]], self._scoped_select(scope)
        )
        statement = statement.order_by(Portfolio.code, Portfolio.path, Portfolio.id).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return tuple(self.session.execute(statement).scalars().all())

    def descendants(self, portfolio: Portfolio, *, scope: Scope) -> Sequence[Portfolio]:
        """Return every visible strict descendant of ``portfolio``."""
        statement: Select[tuple[Portfolio]] = cast(
            Select[tuple[Portfolio]], self._scoped_select(scope)
        )
        statement = statement.where(
            Portfolio.path.like(f"{portfolio.path}%", escape="\\"),
            Portfolio.id != portfolio.id,
        ).order_by(Portfolio.path, Portfolio.id)
        return tuple(self.session.execute(statement).scalars().all())

    def by_id_for_update(self, portfolio_id: UUID, *, scope: Scope) -> Portfolio | None:
        """Lock one in-scope portfolio for an optimistic write."""
        statement: Select[tuple[Portfolio]] = cast(
            Select[tuple[Portfolio]], self._scoped_select(scope)
        )
        statement = statement.where(Portfolio.id == portfolio_id).with_for_update()
        return self.session.execute(statement).scalars().one_or_none()
