"""Unit tests for the borrower and facility tables (`T-008`):
`plan.md §5.2` copied exactly, the CIN fingerprint's active-only
uniqueness, the effective-dating invariant `Facility.supersede` guards,
and idempotent daily conduct.

Every test runs against a real in-memory SQLite database — the same
technique `tests/unit/test_model_identity.py` (`T-007`) established — so
this file stays fast and network-free; the schema is proven again against
a real PostgreSQL instance once `tests/integration` exercises these models.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.errors import ValidationError
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import (
    Borrower,
    BorrowerContact,
    BorrowerGroup,
    RelatedParty,
)
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.reference import IndustryReference

_MODEL_TABLES = [
    Portfolio.__table__,
    AppUser.__table__,
    IndustryReference.__table__,
    BorrowerGroup.__table__,
    Borrower.__table__,
    RelatedParty.__table__,
    BorrowerContact.__table__,
    Facility.__table__,
    FacilityConduct.__table__,
]

# `StandardColumns` (`db/base.py`) carried by every table, plus the
# foreign-keyed overrides `identity.UserAttributedColumns` adds on top —
# every T-008 table mixes both in, so every table's column set includes
# these six.
_STANDARD_COLUMNS = {
    "id",
    "created_at",
    "updated_at",
    "created_by_id",
    "updated_by_id",
    "request_id",
}

# `plan.md §5.2`'s "Key fields" per table, copied exactly, with one
# addition: `borrower.cin_fingerprint` is named only in that table's Notes
# and Indexes text, not its "Key fields" cell, but it is a real column the
# fingerprint-uniqueness behaviour cannot exist without.
_PLAN_FIELDS: dict[str, set[str]] = {
    "borrower_group": {"name", "parent_id"},
    "borrower": {
        "reference",
        "legal_name",
        "cin_enc",
        "pan_enc",
        "cin_fingerprint",
        "industry_code",
        "group_id",
        "portfolio_id",
        "constitution",
        "incorporation_date",
        "is_active",
    },
    "related_party": {
        "borrower_id",
        "party_type",
        "name_enc",
        "identifier_enc",
        "role",
        "effective_from",
        "effective_to",
    },
    "borrower_contact": {
        "borrower_id",
        "name_enc",
        "email_enc",
        "phone_enc",
        "designation",
        "is_primary",
    },
    "facility": {
        "reference",
        "borrower_id",
        "facility_type",
        "sanctioned_limit",
        "currency",
        "drawing_power",
        "outstanding",
        "security_type",
        "pricing_bps",
        "sanction_date",
        "maturity_date",
        "effective_from",
        "effective_to",
        "superseded_by_id",
    },
    "facility_conduct": {
        "facility_id",
        "as_of_date",
        "outstanding",
        "utilisation_pct",
        "days_past_due",
        "overdue_amount",
        "excess_amount",
        "source_id",
    },
    "industry_reference": {"code", "name", "parent_code", "taxonomy_version"},
}

# Tables carrying `VersionedColumns` — the user-editable entities
# (`plan.md §5`'s convention). `facility_conduct` is ingested, not
# user-edited, and `industry_reference` is seeded, so neither carries it.
_VERSIONED_TABLES = {
    "borrower_group",
    "borrower",
    "related_party",
    "borrower_contact",
    "facility",
}

_MODELS_BY_TABLE = {table.name: table for table in _MODEL_TABLES}


def _sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_MODEL_TABLES)
    return engine


def _now() -> datetime:
    return datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)


def _request_id(suffix: str) -> str:
    return f"rq-{suffix:0>16}"


def _make_portfolio(code: str = "PF1") -> Portfolio:
    return Portfolio.create(
        code=code,
        name=f"Portfolio {code}",
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("1"),
    )


def test_all_columns_match_plan() -> None:
    for table_name, plan_fields in _PLAN_FIELDS.items():
        table = _MODELS_BY_TABLE[table_name]
        expected = set(_STANDARD_COLUMNS) | plan_fields
        if table_name in _VERSIONED_TABLES:
            expected.add("version")

        actual = {column.name for column in table.columns}
        assert actual == expected, (
            f"{table_name}: expected {sorted(expected)}, got {sorted(actual)}"
        )


def test_cin_fingerprint_unique_among_active() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        portfolio = _make_portfolio()
        session.add(portfolio)
        session.flush()

        first = Borrower(
            reference="B-000001",
            legal_name="Acme Pvt Ltd",
            portfolio_id=portfolio.id,
            cin_fingerprint="fp-shared",
            is_active=True,
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("2"),
        )
        session.add(first)
        session.commit()

        duplicate = Borrower(
            reference="B-000002",
            legal_name="Acme Duplicate Pvt Ltd",
            portfolio_id=portfolio.id,
            cin_fingerprint="fp-shared",
            is_active=True,
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("3"),
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Deactivating the first borrower frees its fingerprint: uniqueness
        # holds only among *active* borrowers, so a corrected record can be
        # created without ever purging the original's history.
        first.is_active = False
        session.add(first)
        session.commit()

        reactivated = Borrower(
            reference="B-000003",
            legal_name="Acme Corrected Pvt Ltd",
            portfolio_id=portfolio.id,
            cin_fingerprint="fp-shared",
            is_active=True,
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("4"),
        )
        session.add(reactivated)
        session.commit()


def test_facility_effective_dating_rejects_overlap() -> None:
    portfolio = _make_portfolio()
    borrower = Borrower(
        reference="B-000001",
        legal_name="Acme Pvt Ltd",
        portfolio_id=portfolio.id,
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("2"),
    )
    predecessor = Facility(
        reference="F-000001-01",
        borrower_id=borrower.id,
        facility_type="cash_credit",
        sanctioned_limit=Decimal("5000000"),
        currency="INR",
        sanction_date=date(2024, 1, 1),
        effective_from=date(2024, 1, 1),
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("3"),
    )

    with pytest.raises(ValidationError, match="F-000001-01"):
        Facility.supersede(
            predecessor,
            reference="F-000001-02",
            effective_from=date(2023, 12, 1),
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("4"),
        )
    # The refused attempt mutated nothing.
    assert predecessor.effective_to is None
    assert predecessor.superseded_by_id is None

    successor = Facility.supersede(
        predecessor,
        reference="F-000001-02",
        effective_from=date(2024, 6, 1),
        sanctioned_limit=Decimal("7500000"),
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("5"),
    )
    assert predecessor.effective_to == date(2024, 6, 1)
    assert predecessor.superseded_by_id == successor.id
    assert successor.effective_from == date(2024, 6, 1)
    assert successor.effective_to is None
    assert successor.sanctioned_limit == Decimal("7500000.0000")
    assert successor.currency == "INR"  # copied from the predecessor, unchanged
    assert successor.borrower_id == borrower.id


def test_conduct_unique_per_facility_day() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        portfolio = _make_portfolio()
        session.add(portfolio)
        session.flush()

        borrower = Borrower(
            reference="B-000001",
            legal_name="Acme Pvt Ltd",
            portfolio_id=portfolio.id,
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("2"),
        )
        session.add(borrower)
        session.flush()

        facility = Facility(
            reference="F-000001-01",
            borrower_id=borrower.id,
            facility_type="cash_credit",
            sanctioned_limit=Decimal("5000000"),
            currency="INR",
            sanction_date=date(2024, 1, 1),
            effective_from=date(2024, 1, 1),
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("3"),
        )
        session.add(facility)
        session.flush()

        as_of_date = date(2024, 6, 15)
        session.add(
            FacilityConduct(
                facility_id=facility.id,
                as_of_date=as_of_date,
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("4"),
            )
        )
        session.add(
            FacilityConduct(
                facility_id=facility.id,
                as_of_date=as_of_date,
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("5"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_references_are_stable_and_unique() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        portfolio = _make_portfolio()
        session.add(portfolio)
        session.flush()

        session.add(
            Borrower(
                reference="B-000001",
                legal_name="Acme Pvt Ltd",
                portfolio_id=portfolio.id,
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("2"),
            )
        )
        session.add(
            Borrower(
                reference="B-000001",
                legal_name="Acme Two Pvt Ltd",
                portfolio_id=portfolio.id,
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("3"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    engine = _sqlite_engine()
    with Session(engine) as session:
        portfolio = _make_portfolio()
        borrower = Borrower(
            reference="B-000001",
            legal_name="Acme Pvt Ltd",
            portfolio_id=portfolio.id,
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("2"),
        )
        session.add_all([portfolio, borrower])
        session.flush()

        session.add(
            Facility(
                reference="F-000001-01",
                borrower_id=borrower.id,
                facility_type="cash_credit",
                sanctioned_limit=Decimal("5000000"),
                currency="INR",
                sanction_date=date(2024, 1, 1),
                effective_from=date(2024, 1, 1),
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("3"),
            )
        )
        session.add(
            Facility(
                reference="F-000001-01",
                borrower_id=borrower.id,
                facility_type="term_loan",
                sanctioned_limit=Decimal("2000000"),
                currency="INR",
                sanction_date=date(2024, 1, 1),
                effective_from=date(2024, 1, 1),
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("4"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
