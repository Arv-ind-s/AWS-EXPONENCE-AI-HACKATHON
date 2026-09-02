"""Fixtures giving each integration test its own isolated transaction
against the PostgreSQL instance CI supplies (`COVENANT_RADAR_DATABASE_URL`),
so tests never see one another's writes and never depend on run order.

Uses SQLAlchemy's own documented recipe for joining a session to an
external transaction: the fixture opens one connection and one outer
transaction per test and always rolls that outer transaction back
afterwards. Every session handed to a test — including the fresh session
`SqlAlchemyUnitOfWork` opens on each `__enter__` — begins its own
`SAVEPOINT` nested inside that outer transaction, and restarts it
automatically the moment it ends, so a real `session.commit()` inside the
code under test only ends the `SAVEPOINT` and never touches the outer
transaction the fixture rolls back at teardown.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.session import SessionTransaction

from covenant_radar.db.base import Base

_DATABASE_URL_ENV = "COVENANT_RADAR_DATABASE_URL"


def _required_database_url() -> str:
    database_url = os.environ.get(_DATABASE_URL_ENV)
    if not database_url:
        pytest.fail(f"{_DATABASE_URL_ENV} is required for integration tests.")
    return database_url


@pytest.fixture(scope="session")
def database_engine() -> Iterator[Engine]:
    """One pooled engine for the whole integration run, against the
    PostgreSQL instance CI supplies. Every table currently declared on the
    shared `Base` is created once and dropped once, rather than per test."""
    engine = create_engine(_required_database_url(), pool_pre_ping=True)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def db_connection(database_engine: Engine) -> Iterator[Connection]:
    """One connection and one outer transaction for a single test, always
    rolled back at the end regardless of what the test did with it."""
    connection = database_engine.connect()
    outer_transaction = connection.begin()
    try:
        yield connection
    finally:
        outer_transaction.rollback()
        connection.close()


@pytest.fixture
def db_session_factory(db_connection: Connection) -> Callable[[], Session]:
    """A session factory bound to `db_connection`.

    Every session it produces opens its own `SAVEPOINT` and restarts it as
    soon as it ends, so code such as `SqlAlchemyUnitOfWork` — which opens a
    fresh session per `__enter__` and calls `session.commit()` on a clean
    exit — can commit, and even open a second unit of work in the same
    test, without ever reaching the connection's outer transaction.
    """
    session_factory = sessionmaker(bind=db_connection, autoflush=False, expire_on_commit=False)

    def make_session() -> Session:
        session = session_factory()
        nested_transaction = db_connection.begin_nested()

        @event.listens_for(session, "after_transaction_end")
        def _restart_savepoint(_session: Session, _transaction: SessionTransaction) -> None:
            nonlocal nested_transaction
            if not nested_transaction.is_active:
                nested_transaction = db_connection.begin_nested()

        return session

    return make_session


@pytest.fixture
def db_session(db_session_factory: Callable[[], Session]) -> Iterator[Session]:
    """One isolated session, for a test that reads and writes directly
    rather than going through a `UnitOfWork`."""
    session = db_session_factory()
    try:
        yield session
    finally:
        session.close()
