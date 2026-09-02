"""Integration proof that the data-access foundation behaves identically
against a real PostgreSQL instance, complementing the offline dialect
simulation in `tests/unit/test_db_base.py`.

Named distinctly from that unit-test module (rather than sharing its
basename) because this test tree has no `__init__.py` files: pytest's
default import mode identifies a collected module by basename alone, and
two files named `test_db_base.py` in different directories collide.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base, StandardColumns
from covenant_radar.db.session import SqlAlchemyUnitOfWork
from covenant_radar.db.types import MoneyAmount

pytestmark = pytest.mark.integration


class _IntegrationSampleRecord(Base, StandardColumns):
    """A throwaway model exercising `StandardColumns` and the custom types
    against a real PostgreSQL instance. Kept independent of the matching
    fixture in `tests/unit/test_db_base.py` — a different table name — so
    the two files stay collectible together without a shared import."""

    __tablename__ = "_test_db_base_integration_record"

    label: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(MoneyAmount)


def test_uuid_datetime_and_money_round_trip_on_postgresql(db_session: Session) -> None:
    instant = datetime(2026, 6, 15, 9, 45, 30, 123456, tzinfo=UTC)
    record = _IntegrationSampleRecord(
        label="postgresql-round-trip",
        amount=Decimal("987654321098.7600"),
        created_at=instant,
        updated_at=instant,
        request_id="rq-integration-0001",
    )
    db_session.add(record)
    db_session.commit()
    record_id = record.id
    db_session.expire_all()

    fetched = db_session.scalar(
        select(_IntegrationSampleRecord).where(_IntegrationSampleRecord.id == record_id)
    )

    assert fetched is not None
    assert isinstance(fetched.id, UUID)
    assert fetched.created_at == instant
    assert fetched.created_at.tzinfo is not None
    assert fetched.amount == Decimal("987654321098.7600")


def test_unit_of_work_commits_and_rolls_back_on_postgresql(
    db_session_factory: Callable[[], Session],
) -> None:
    unit_of_work = SqlAlchemyUnitOfWork(db_session_factory)
    committed_id = new_id()

    with unit_of_work:
        unit_of_work.session.add(
            _IntegrationSampleRecord(
                id=committed_id,
                label="committed",
                amount=Decimal("1.0000"),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                request_id="rq-integration-0002",
            )
        )

    rolled_back_id = new_id()

    class _DeliberateFailure(Exception):
        pass

    with pytest.raises(_DeliberateFailure):
        with unit_of_work:
            unit_of_work.session.add(
                _IntegrationSampleRecord(
                    id=rolled_back_id,
                    label="rolled-back",
                    amount=Decimal("2.0000"),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    request_id="rq-integration-0003",
                )
            )
            unit_of_work.session.flush()
            raise _DeliberateFailure

    verification_session = db_session_factory()
    try:
        assert verification_session.get(_IntegrationSampleRecord, committed_id) is not None
        assert verification_session.get(_IntegrationSampleRecord, rolled_back_id) is None
    finally:
        verification_session.close()
