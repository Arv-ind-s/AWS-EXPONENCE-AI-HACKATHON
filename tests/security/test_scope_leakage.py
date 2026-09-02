"""Security tests proving repository reads cannot cross portfolio boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.base import RepositoryBase
from covenant_radar.db.scoping import Scope, portfolio_path_for

pytestmark = pytest.mark.security

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Portfolio.__table__,
            Borrower.__table__,
            Facility.__table__,
            FacilityConduct.__table__,
        ],
    )
    return engine


def _portfolio(code: str) -> Portfolio:
    return Portfolio.create(
        code=code,
        name=code.title(),
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-{code:0>16}",
    )


def _borrower(reference: str, portfolio: Portfolio) -> Borrower:
    return Borrower(
        id=uuid4(),
        reference=reference,
        legal_name=f"Borrower {reference}",
        portfolio_id=portfolio.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-{reference:0>16}",
    )


def _facility(reference: str, borrower: Borrower) -> Facility:
    from decimal import Decimal

    return Facility(
        id=uuid4(),
        reference=reference,
        borrower_id=borrower.id,
        facility_type="term_loan",
        sanctioned_limit=Decimal("1000.0000"),
        currency="INR",
        sanction_date=_NOW.date(),
        effective_from=_NOW.date(),
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-{reference:0>16}",
    )


def _scope(user_id, portfolio: Portfolio) -> Scope:
    return Scope.from_paths(user_id, [portfolio.path])


def test_no_repository_leaks_across_portfolios() -> None:
    engine = _engine()
    with Session(engine) as session:
        first = _portfolio("FIRST")
        second = _portfolio("SECOND")
        first_borrower = _borrower("B-FIRST", first)
        second_borrower = _borrower("B-SECOND", second)
        first_facility = _facility("F-FIRST", first_borrower)
        second_facility = _facility("F-SECOND", second_borrower)
        session.add_all(
            [first, second, first_borrower, second_borrower, first_facility, second_facility]
        )
        session.flush()

        scope = _scope(uuid4(), first)
        borrower_repository = RepositoryBase(session, Borrower)
        facility_repository = RepositoryBase(session, Facility)

        assert {row.reference for row in borrower_repository.list(scope=scope)} == {"B-FIRST"}
        assert {row.reference for row in facility_repository.list(scope=scope)} == {"F-FIRST"}
        assert borrower_repository.find(scope=scope, reference="B-SECOND") is None
        assert borrower_repository.get(second_borrower.id, scope=scope) is None
        assert facility_repository.get(second_facility.id, scope=scope) is None


def test_direct_id_returns_404_not_403() -> None:
    engine = _engine()
    with Session(engine) as session:
        first = _portfolio("FIRST")
        second = _portfolio("SECOND")
        visible = _borrower("B-FIRST", first)
        hidden = _borrower("B-SECOND", second)
        session.add_all([first, second, visible, hidden])
        session.flush()
        repository = RepositoryBase(session, Borrower)

        result = repository.get(hidden.id, scope=_scope(uuid4(), first))

        assert result is None


def test_joined_entities_follow_the_predicate() -> None:
    engine = _engine()
    with Session(engine) as session:
        first = _portfolio("FIRST")
        second = _portfolio("SECOND")
        first_borrower = _borrower("B-FIRST", first)
        second_borrower = _borrower("B-SECOND", second)
        first_facility = _facility("F-FIRST", first_borrower)
        second_facility = _facility("F-SECOND", second_borrower)
        session.add_all(
            [first, second, first_borrower, second_borrower, first_facility, second_facility]
        )
        session.flush()
        session.add_all(
            [
                FacilityConduct(
                    id=uuid4(),
                    facility_id=first_facility.id,
                    as_of_date=_NOW.date(),
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-conduct-first",
                ),
                FacilityConduct(
                    id=uuid4(),
                    facility_id=second_facility.id,
                    as_of_date=_NOW.date(),
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-conduct-second",
                ),
            ]
        )
        session.flush()

        repository = RepositoryBase(session, FacilityConduct)
        visible = repository.list(scope=_scope(uuid4(), first))

        assert len(visible) == 1
        assert visible[0].facility_id == first_facility.id
        assert portfolio_path_for(visible[0], session) == first.path
