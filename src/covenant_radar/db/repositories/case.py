"""Scoped persistence for cases and their append-only history."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.core.errors import Conflict
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Case, CaseEvent
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for


class CaseRepository(RepositoryBase[Case]):
    """A scope-enforcing repository with no case or history delete/update API."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(session, Case, ownership=ownership_path_for(Case), audit=audit)

    def add_event(self, event: CaseEvent) -> CaseEvent:
        """Append one history row; there is deliberately no edit counterpart."""

        if not isinstance(event, CaseEvent):
            raise TypeError("CaseRepository.add_event requires a CaseEvent.")
        self.session.add(event)
        self.session.flush()
        return event

    def by_reference(self, reference: str, *, scope: Scope) -> Case | None:
        """Return one scoped case by its stable human reference."""

        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("Case reference must be non-empty text.")
        return self.find(scope=scope, reference=reference.strip())

    get_by_reference = by_reference

    def get_for_update(self, case_id: UUID, *, scope: Scope) -> Case | None:
        """Load one scoped case under a row lock for a lifecycle mutation."""

        if not isinstance(case_id, UUID):
            raise TypeError("case_id must be a UUID.")
        statement: Select[tuple[Case]] = cast(
            Select[tuple[Case]], self._scoped_select(scope)
        ).where(Case.id == case_id)
        statement = statement.with_for_update()
        return self.session.execute(statement).scalars().one_or_none()

    def open_cases_for_borrower(
        self,
        borrower_id: UUID,
        *,
        scope: Scope,
        for_update: bool = False,
    ) -> tuple[Case, ...]:
        """Return every non-closed scoped case for a borrower in stable order."""

        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        statement: Select[tuple[Case]] = cast(
            Select[tuple[Case]], self._scoped_select(scope)
        ).where(Case.borrower_id == borrower_id, Case.state != "closed")
        statement = statement.order_by(Case.created_at, Case.id)
        if for_update:
            statement = statement.with_for_update()
        return tuple(self.session.execute(statement).scalars().all())

    def open_for_borrower(
        self,
        borrower_id: UUID,
        *,
        scope: Scope,
        for_update: bool = False,
    ) -> Case | None:
        """Return the sole open case, refusing a pre-existing invariant break."""

        rows = self.open_cases_for_borrower(borrower_id, scope=scope, for_update=for_update)
        if len(rows) > 1:
            raise Conflict(
                f"Borrower {borrower_id} has {len(rows)} open cases; exactly one is permitted."
            )
        return rows[0] if rows else None

    def list(
        self,
        *,
        scope: Scope,
        state: str | None = None,
        borrower_id: UUID | None = None,
    ) -> Sequence[Case]:
        """List scoped cases using database-side filters and stable ordering."""

        statement: Select[tuple[Case]] = cast(Select[tuple[Case]], self._scoped_select(scope))
        if state is not None:
            statement = statement.where(Case.state == state)
        if borrower_id is not None:
            if not isinstance(borrower_id, UUID):
                raise TypeError("borrower_id must be a UUID.")
            statement = statement.where(Case.borrower_id == borrower_id)
        statement = statement.order_by(Case.created_at.desc(), Case.id.desc())
        return tuple(self.session.execute(statement).scalars().all())

    def overdue(
        self,
        now: datetime,
        *,
        scope: Scope,
    ) -> tuple[Case, ...]:
        """Return all non-closed cases whose SLA is due at or before ``now``."""

        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime.")
        statement: Select[tuple[Case]] = cast(
            Select[tuple[Case]], self._scoped_select(scope)
        ).where(
            Case.state != "closed",
            Case.due_at.is_not(None),
            Case.due_at <= now,
        )
        statement = statement.order_by(Case.due_at, Case.created_at, Case.id)
        return tuple(self.session.execute(statement).scalars().all())

    list_overdue = overdue

    def events_for(self, case_id: UUID, *, scope: Scope) -> tuple[CaseEvent, ...]:
        """Return a scoped case's complete history in chronological order."""

        if not isinstance(case_id, UUID):
            raise TypeError("case_id must be a UUID.")
        statement: Select[tuple[CaseEvent]] = (
            select(CaseEvent)
            .join(Case, Case.id == CaseEvent.case_id)
            .join(Borrower, Borrower.id == Case.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(
                CaseEvent.case_id == case_id,
                scope.predicate(Portfolio.path),
            )
            .order_by(CaseEvent.occurred_at, CaseEvent.id)
        )
        return tuple(self.session.execute(statement).scalars().all())

    history_for = events_for

    def lock_borrower(self, borrower_id: UUID, *, scope: Scope) -> Borrower | None:
        """Lock the owning borrower to serialize open-or-update decisions."""

        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        statement = (
            select(Borrower)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(Borrower.id == borrower_id, scope.predicate(Portfolio.path))
            .with_for_update()
        )
        return self.session.execute(statement).scalars().one_or_none()

    def references_for_borrower(self, borrower_id: UUID) -> tuple[str, ...]:
        """Return all references for one borrower for deterministic re-escalation IDs."""

        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        statement = (
            select(Case.reference).where(Case.borrower_id == borrower_id).order_by(Case.reference)
        )
        return tuple(self.session.execute(statement).scalars().all())


__all__ = ["CaseRepository"]
