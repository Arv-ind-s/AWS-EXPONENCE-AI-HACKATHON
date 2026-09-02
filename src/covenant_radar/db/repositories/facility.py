"""Scoped repository adapter for effective-dated facilities."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for

#: How many rows one facility list screen may ask the database for at once.
_MAX_LIST_LIMIT = 200
#: An upper bound on the effective-dated chain walk, so a cyclic
#: ``superseded_by_id`` (which the write path forbids, but which a repaired
#: database could still carry) cannot spin the request thread.
_MAX_CHAIN_LENGTH = 50
#: The `LIKE` escape character used for every user-supplied search term.
_LIKE_ESCAPE = "\\"
#: Effective-dating states a list screen can ask for. `current` is every
#: screen's default, so a reader never sees a superseded limit beside the
#: live one without having asked for the history.
CURRENT_STATUS = "current"
SUPERSEDED_STATUS = "superseded"
ALL_STATUSES = "all"
FACILITY_STATUSES: tuple[str, ...] = (CURRENT_STATUS, SUPERSEDED_STATUS, ALL_STATUSES)


@dataclass(frozen=True, slots=True)
class FacilityBookRow:
    """One facility reduced to the columns a book-level summary reads.

    Money stays `Decimal` the whole way: the summary is computed in Python
    rather than by SQL `sum()` because `MoneyAmount` is fixed-point *text*
    on SQLite (see `db/types.py`), where a SQL aggregate would silently
    round the book through an IEEE-754 float.
    """

    borrower_id: UUID
    facility_type: str
    currency: str
    sanctioned_limit: Decimal
    outstanding: Decimal | None
    drawing_power: Decimal | None
    pricing_bps: int | None
    sanction_date: date
    maturity_date: date | None


@dataclass(frozen=True, slots=True)
class FacilityListing:
    """One facility together with the borrower a reader needs to identify it.

    The list screen previously rendered `borrower_id` — a raw UUID no user
    can act on — because the row carried nothing else. The borrower is
    already joined by the ownership path, so naming it costs no extra query.
    """

    facility: Facility
    borrower_reference: str
    borrower_legal_name: str


class FacilityRepository(RepositoryBase[Facility]):
    """Repository that resolves facility ownership through its borrower."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(session, Facility, ownership=ownership_path_for(Facility), audit=audit)

    def by_reference(self, reference: str, *, scope: Scope) -> Facility | None:
        """Return one in-scope effective-dated row by stable reference."""
        return self.find(scope=scope, reference=reference)

    def ordered(
        self,
        *,
        scope: Scope,
        current_only: bool = True,
        search: str | None = None,
        facility_type: str | None = None,
        currency: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[Facility]:
        """Return facilities in deterministic order, normally current rows only."""
        statement = self._listing_select(
            self._scoped_select(scope),
            status=CURRENT_STATUS if current_only else ALL_STATUSES,
            search=search,
            facility_type=facility_type,
            currency=currency,
            limit=limit,
            offset=offset,
        )
        return tuple(self.session.execute(cast(Select[tuple[Facility]], statement)).scalars().all())

    def ordered_with_borrower(
        self,
        *,
        scope: Scope,
        status: str = CURRENT_STATUS,
        search: str | None = None,
        facility_type: str | None = None,
        currency: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[FacilityListing]:
        """Return the same page as :meth:`ordered`, each row named by borrower."""
        statement = self._listing_select(
            self._scoped_select(scope).add_columns(Borrower.reference, Borrower.legal_name),
            status=status,
            search=search,
            facility_type=facility_type,
            currency=currency,
            limit=limit,
            offset=offset,
        )
        return tuple(
            FacilityListing(
                facility=row[0],
                borrower_reference=row[1],
                borrower_legal_name=row[2],
            )
            for row in self.session.execute(statement).all()
        )

    def count(
        self,
        *,
        scope: Scope,
        status: str = CURRENT_STATUS,
        search: str | None = None,
        facility_type: str | None = None,
        currency: str | None = None,
    ) -> int:
        """Count in-scope facilities matching the same filters as a page."""
        statement = self._aggregate_select(select(func.count()).select_from(Facility), scope)
        statement = self._filters(
            statement,
            status=status,
            search=search,
            facility_type=facility_type,
            currency=currency,
        )
        return int(self.session.execute(statement).scalar_one())

    def book_rows(
        self,
        *,
        scope: Scope,
        current_only: bool = True,
        limit: int | None = None,
    ) -> Sequence[FacilityBookRow]:
        """Return the whole in-scope book as summary inputs, money intact."""
        if limit is not None and limit < 1:
            raise ValueError("Facility book limit must be positive.")
        statement = self._aggregate_select(
            select(
                Facility.borrower_id,
                Facility.facility_type,
                Facility.currency,
                Facility.sanctioned_limit,
                Facility.outstanding,
                Facility.drawing_power,
                Facility.pricing_bps,
                Facility.sanction_date,
                Facility.maturity_date,
            ),
            scope,
        )
        if current_only:
            statement = statement.where(Facility.effective_to.is_(None))
        statement = statement.order_by(Facility.reference, Facility.id)
        if limit is not None:
            statement = statement.limit(limit)
        return tuple(
            FacilityBookRow(
                borrower_id=row[0],
                facility_type=row[1],
                currency=row[2],
                sanctioned_limit=row[3],
                outstanding=row[4],
                drawing_power=row[5],
                pricing_bps=row[6],
                sanction_date=row[7],
                maturity_date=row[8],
            )
            for row in self.session.execute(statement).all()
        )

    def distinct_values(self, column_name: str, *, scope: Scope) -> Sequence[str]:
        """Return the in-scope values of one small categorical column.

        Only the two columns the facility filters offer are accepted, so a
        query parameter can never name an arbitrary column here.
        """
        if column_name not in {"facility_type", "currency"}:
            raise ValueError(f"{column_name!r} is not a filterable facility column.")
        column = cast(Any, getattr(Facility, column_name))
        statement = self._aggregate_select(select(column).distinct(), scope).order_by(column)
        return tuple(value for value in self.session.execute(statement).scalars().all() if value)

    def revision_chain(self, facility: Facility, *, scope: Scope) -> Sequence[Facility]:
        """Return the effective-dated chain ``facility`` belongs to, oldest first.

        A limit change never overwrites a row: it closes the current one and
        inserts a successor (`Facility.supersede`).  Walking
        ``superseded_by_id`` in both directions therefore reconstructs the
        limit history a credit officer needs, and every hop goes back through
        the scoped reads so a chain can never leak an out-of-scope row.
        """
        chain: list[Facility] = [facility]
        seen: set[UUID] = {facility.id}

        current = facility
        while len(chain) < _MAX_CHAIN_LENGTH:
            predecessor = self.find(scope=scope, superseded_by_id=current.id)
            if predecessor is None or predecessor.id in seen:
                break
            chain.insert(0, predecessor)
            seen.add(predecessor.id)
            current = predecessor

        current = facility
        while len(chain) < _MAX_CHAIN_LENGTH and current.superseded_by_id is not None:
            successor = self.get(current.superseded_by_id, scope=scope)
            if successor is None or successor.id in seen:
                break
            chain.append(successor)
            seen.add(successor.id)
            current = successor
        return tuple(chain)

    def live_for_borrower(self, borrower_id: UUID, *, scope: Scope) -> Sequence[Facility]:
        """Return current rows for one in-scope borrower."""
        statement: Select[tuple[Facility]] = cast(
            Select[tuple[Facility]], self._scoped_select(scope)
        )
        statement = statement.where(
            Facility.borrower_id == borrower_id,
            Facility.effective_to.is_(None),
        ).order_by(Facility.reference, Facility.id)
        return tuple(self.session.execute(statement).scalars().all())

    def for_borrower(
        self, borrower_id: UUID, *, scope: Scope, current_only: bool = True
    ) -> Sequence[Facility]:
        """Return all or current versions for one in-scope borrower."""
        statement: Select[tuple[Facility]] = cast(
            Select[tuple[Facility]], self._scoped_select(scope)
        )
        statement = statement.where(Facility.borrower_id == borrower_id)
        if current_only:
            statement = statement.where(Facility.effective_to.is_(None))
        statement = statement.order_by(Facility.effective_from, Facility.id)
        return tuple(self.session.execute(statement).scalars().all())

    def as_of(self, borrower_id: UUID, as_of: date, *, scope: Scope) -> Facility | None:
        """Return the facility version effective for ``as_of``.

        The half-open interval ``[effective_from, effective_to)`` makes a
        successor effective on its start date while preserving the prior row
        for dates strictly before that boundary.
        """
        statement: Select[tuple[Facility]] = cast(
            Select[tuple[Facility]], self._scoped_select(scope)
        )
        statement = (
            statement.where(
                Facility.borrower_id == borrower_id,
                Facility.effective_from <= as_of,
                or_(Facility.effective_to.is_(None), Facility.effective_to > as_of),
            )
            .order_by(Facility.effective_from.desc(), Facility.id.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().one_or_none()

    def by_id_for_update(self, facility_id: UUID, *, scope: Scope) -> Facility | None:
        """Lock one in-scope row for an optimistic, effective-dated write."""
        statement: Select[tuple[Facility]] = cast(
            Select[tuple[Facility]], self._scoped_select(scope)
        )
        statement = statement.where(Facility.id == facility_id).with_for_update()
        return self.session.execute(statement).scalars().one_or_none()

    # ---- shared query composition ---------------------------------------

    def _aggregate_select(self, statement: Select[Any], scope: Scope) -> Select[Any]:
        """Compose the ownership joins and scope predicate onto a bare select.

        `_scoped_select` only builds `select(Facility)`; a count or a
        column-projection needs the same predicate over a different select
        shape, and building it here keeps the "no read without the scope
        predicate" rule in one place for this repository too.
        """
        if not isinstance(scope, Scope):
            raise TypeError("Every repository read requires a covenant_radar.db.scoping.Scope.")
        return self.ownership.apply(statement).where(scope.predicate(self.ownership.path_column))

    def _listing_select(
        self,
        statement: Select[Any],
        *,
        status: str,
        search: str | None,
        facility_type: str | None,
        currency: str | None,
        limit: int | None,
        offset: int,
    ) -> Select[Any]:
        if offset < 0:
            raise ValueError("Facility list offset cannot be negative.")
        if limit is not None and not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ValueError(f"Facility list limit must be between 1 and {_MAX_LIST_LIMIT}.")
        statement = self._filters(
            statement,
            status=status,
            search=search,
            facility_type=facility_type,
            currency=currency,
        )
        statement = statement.order_by(Facility.reference, Facility.id).offset(offset)
        return statement.limit(limit) if limit is not None else statement

    def _filters(
        self,
        statement: Select[Any],
        *,
        status: str,
        search: str | None,
        facility_type: str | None,
        currency: str | None,
    ) -> Select[Any]:
        if status not in FACILITY_STATUSES:
            raise ValueError(f"{status!r} is not a facility effective-dating status.")
        if status == CURRENT_STATUS:
            statement = statement.where(Facility.effective_to.is_(None))
        elif status == SUPERSEDED_STATUS:
            statement = statement.where(Facility.effective_to.is_not(None))
        if facility_type:
            statement = statement.where(Facility.facility_type == facility_type)
        if currency:
            statement = statement.where(Facility.currency == currency.upper())
        term = _like_term(search)
        if term is not None:
            clauses: tuple[ColumnElement[bool], ...] = (
                func.lower(Facility.reference).like(term, escape=_LIKE_ESCAPE),
                func.lower(Borrower.reference).like(term, escape=_LIKE_ESCAPE),
                func.lower(Borrower.legal_name).like(term, escape=_LIKE_ESCAPE),
            )
            statement = statement.where(or_(*clauses))
        return statement


def _like_term(search: str | None) -> str | None:
    """Return a lowercased, wildcard-escaped ``LIKE`` term, or ``None``.

    A user's own ``%`` or ``_`` must match literally rather than widen the
    predicate, so both are escaped before the surrounding wildcards are added.
    """
    if search is None:
        return None
    clean = search.strip().lower()
    if not clean:
        return None
    for character in (_LIKE_ESCAPE, "%", "_"):
        clean = clean.replace(character, f"{_LIKE_ESCAPE}{character}")
    return f"%{clean}%"


__all__ = [
    "ALL_STATUSES",
    "CURRENT_STATUS",
    "FACILITY_STATUSES",
    "SUPERSEDED_STATUS",
    "FacilityBookRow",
    "FacilityListing",
    "FacilityRepository",
]
