"""Unit tests for the data-access foundation: the portable column types,
the standard-column mixin, the unit of work, and the scope-carrying
repository shape.

The SQLite half of every dual-engine assertion runs against a real
in-memory database. The PostgreSQL half runs against an offline
`postgresql.dialect()` instance — SQLAlchemy's own documented technique for
exercising a dialect's bind/result behaviour without a live connection —
so this file stays a fast, network-free unit test; the same types are
proven again against a real PostgreSQL instance in
`tests/integration/test_db_base.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import String, create_engine, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Mapped, Session, mapped_column

from covenant_radar.core.errors import ExternalServiceError
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base, StandardColumns
from covenant_radar.db.session import SqlAlchemyUnitOfWork, create_session_factory
from covenant_radar.db.types import GUID, AwareDateTime, MoneyAmount
from covenant_radar.ports.repository import Repository

_SQLITE_DIALECT = sqlite.dialect()
_POSTGRESQL_DIALECT = postgresql.dialect()


class _SampleRecord(Base, StandardColumns):
    """A throwaway model exercising `StandardColumns` and the custom types
    together, exactly as a real model (`T-007` onward) will."""

    __tablename__ = "_test_db_base_sample_record"

    label: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(MoneyAmount)


def _round_trip(type_, dialect, value):
    """Bind `value` through `type_`'s processors for `dialect`, then read
    it back through the matching result processor."""
    bind = type_.bind_processor(dialect)
    result = type_.result_processor(dialect, None)
    bound = bind(value) if bind is not None else value
    return result(bound) if result is not None else bound


def _sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[_SampleRecord.__table__])
    return engine


def test_uuid_type_round_trip_both_engines() -> None:
    original = new_id()

    sqlite_result = _round_trip(GUID(), _SQLITE_DIALECT, original)
    assert sqlite_result == original
    assert isinstance(sqlite_result, UUID)

    postgresql_result = _round_trip(GUID(), _POSTGRESQL_DIALECT, original)
    assert postgresql_result == original
    assert isinstance(postgresql_result, UUID)

    engine = _sqlite_engine()
    with Session(engine) as session:
        record = _SampleRecord(
            label="uuid-round-trip",
            amount=Decimal("1.0000"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            request_id="rq-0000000000000000",
        )
        session.add(record)
        session.commit()
        record_id = record.id

        fetched = session.scalar(select(_SampleRecord).where(_SampleRecord.id == record_id))
        assert fetched is not None
        assert fetched.id == record_id
        assert isinstance(fetched.id, UUID)


def test_datetime_type_is_aware_utc() -> None:
    naive = datetime(2026, 3, 5, 12, 30, 0)  # noqa: DTZ001 -- the refusal itself is under test
    with pytest.raises(ValueError, match="naive"):
        _round_trip(AwareDateTime(), _SQLITE_DIALECT, naive)

    ist = timezone(timedelta(hours=5, minutes=30))
    aware_ist = datetime(2026, 3, 5, 18, 0, 0, tzinfo=ist)

    sqlite_result = _round_trip(AwareDateTime(), _SQLITE_DIALECT, aware_ist)
    assert sqlite_result.tzinfo is not None
    assert sqlite_result.astimezone(UTC) == aware_ist.astimezone(UTC)

    postgresql_result = _round_trip(AwareDateTime(), _POSTGRESQL_DIALECT, aware_ist)
    assert postgresql_result.tzinfo is not None
    assert postgresql_result.astimezone(UTC) == aware_ist.astimezone(UTC)

    engine = _sqlite_engine()
    instant = datetime(2026, 6, 15, 9, 45, 30, 123456, tzinfo=UTC)
    with Session(engine) as session:
        record = _SampleRecord(
            label="datetime-round-trip",
            amount=Decimal("0.0000"),
            created_at=instant,
            updated_at=instant,
            request_id="rq-0000000000000001",
        )
        session.add(record)
        session.commit()
        session.expire_all()

        fetched = session.scalar(select(_SampleRecord).where(_SampleRecord.id == record.id))
        assert fetched is not None
        assert fetched.created_at.tzinfo is not None
        assert fetched.created_at == instant


def test_money_type_preserves_scale() -> None:
    original = Decimal("123456789012.3400")

    sqlite_result = _round_trip(MoneyAmount(), _SQLITE_DIALECT, original)
    assert sqlite_result == original
    assert sqlite_result.as_tuple().exponent == -4

    postgresql_result = _round_trip(MoneyAmount(), _POSTGRESQL_DIALECT, original)
    assert postgresql_result == original

    with pytest.raises(TypeError, match="Decimal"):
        _round_trip(MoneyAmount(), _SQLITE_DIALECT, 1.5)

    engine = _sqlite_engine()
    with Session(engine) as session:
        record = _SampleRecord(
            label="money-round-trip",
            amount=Decimal("1000.5"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            request_id="rq-0000000000000002",
        )
        session.add(record)
        session.commit()
        session.expire_all()

        fetched = session.scalar(select(_SampleRecord).where(_SampleRecord.id == record.id))
        assert fetched is not None
        assert fetched.amount == Decimal("1000.5000")
        assert fetched.amount.as_tuple().exponent == -4


def test_nested_unit_of_work_raises() -> None:
    engine = _sqlite_engine()
    unit_of_work = SqlAlchemyUnitOfWork(create_session_factory(engine))

    with unit_of_work:
        with pytest.raises(RuntimeError, match="already open") as excinfo:
            unit_of_work.__enter__()

    message = str(excinfo.value)
    assert "entered at" in message
    assert "attempted" in message
    assert __file__ in message


def test_uow_rolls_back_on_exception() -> None:
    engine = _sqlite_engine()
    session_factory = create_session_factory(engine)
    unit_of_work = SqlAlchemyUnitOfWork(session_factory)
    record_id = new_id()

    class _DeliberateFailure(Exception):
        pass

    with pytest.raises(_DeliberateFailure):
        with unit_of_work:
            unit_of_work.session.add(
                _SampleRecord(
                    id=record_id,
                    label="should-not-persist",
                    amount=Decimal("5.0000"),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    request_id="rq-0000000000000003",
                )
            )
            unit_of_work.session.flush()
            raise _DeliberateFailure

    with Session(engine) as verification_session:
        survivor = verification_session.get(_SampleRecord, record_id)
        assert survivor is None

    # A loopback address with nothing listening: local traffic only, so it
    # is unaffected by the test suite's outbound-network guard
    # (`tests/conftest.py`), and a short `connect_timeout` keeps the
    # failure fast and deterministic rather than depending on how long the
    # underlying driver waits for a connection nothing will ever accept.
    unreachable_engine = create_engine(
        "postgresql+psycopg://nobody:secret-password@127.0.0.1:1/nonexistent",
        connect_args={"connect_timeout": 2},
    )
    unreachable_unit_of_work = SqlAlchemyUnitOfWork(create_session_factory(unreachable_engine))
    with pytest.raises(ExternalServiceError, match="127.0.0.1:1") as external_error:
        with unreachable_unit_of_work:
            pass
    assert "secret-password" not in str(external_error.value)


def test_repository_read_requires_scope() -> None:
    class _Scope:
        pass

    class _InMemoryRepository(Repository[str, _Scope]):
        def get(self, entity_id: UUID, *, scope: _Scope) -> str | None:
            return None

        def find(self, *, scope: _Scope, **criteria: object) -> str | None:
            return None

        def list(self, *, scope: _Scope) -> list[str]:
            return []

        def add(self, entity: str) -> None:
            pass

    repository = _InMemoryRepository()
    entity_id = uuid4()
    scope = _Scope()

    with pytest.raises(TypeError, match="scope"):
        repository.get(entity_id)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="scope"):
        repository.list()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="scope"):
        repository.find()  # type: ignore[call-arg]

    assert repository.get(entity_id, scope=scope) is None
    assert repository.list(scope=scope) == []
