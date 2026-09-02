"""Alembic's migration environment.

Resolves the database URL from `config.set_main_option("sqlalchemy.url",
...)` when a caller has set one (`radarctl migrate --database-url`, or
`tests/migration` pointing at a throwaway database) and otherwise from the
application's own settings (`config/settings.py`) — the same configuration
precedence every other entry point uses, so this file is never a second,
competing source of truth for where the database lives.

Importing `covenant_radar.db.models` registers every table on
`Base.metadata` (see that package's own docstring) before either migration
path below runs, which is what both online migration and `alembic check`'s
autogenerate comparison need to see the complete schema.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import engine_from_config, event, pool
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

import covenant_radar.db.models  # noqa: F401 -- registers every table on Base.metadata
from covenant_radar.config.settings import get_settings
from covenant_radar.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Compared once here rather than per call site below.
_CONFIGURED_URL = config.get_main_option("sqlalchemy.url")


def _render_item(item_type: str, obj: Any, autogen_context: AutogenContext) -> Any:
    """Register the import a custom column type needs.

    Alembic's default renderer already writes a fully qualified reference
    for a type it does not recognise as a SQLAlchemy built-in — e.g.
    ``covenant_radar.db.types.AwareDateTime(timezone=True)`` — but it does
    *not* add the matching ``import`` statement, so the generated
    migration references a name nothing ever bound. Returning `False`
    here leaves the rendering itself to that default; this hook only
    records the module every one of our own types needs imported, so a
    future `alembic revision --autogenerate` involving a new custom type
    produces a migration that actually runs, without a hand edit.
    """
    if item_type == "type" and type(obj).__module__.startswith("covenant_radar."):
        autogen_context.imports.add(f"import {type(obj).__module__}")
    return False


def _use_transactional_sqlite_ddl(engine: Engine) -> None:
    """Make `CREATE TABLE`/`DROP TABLE` roll back on SQLite, same as any
    other statement in a failed transaction.

    Python's `sqlite3` driver, in its default transaction-control mode,
    implicitly commits before every schema-changing statement — so a
    `CREATE TABLE` already run before a later statement fails would
    survive `ROLLBACK` unless that legacy mode is disabled. This is
    SQLAlchemy's own documented recipe for genuinely transactional DDL on
    SQLite (`Serializable isolation, savepoints, transactional DDL` in
    its SQLite dialect docs): take over `BEGIN` explicitly once the
    driver's own implicit one is turned off. Every case `T-010` names
    that depends on "a migration failing part-way rolls back whole"
    needs this — without it, only the missing `alembic_version` stamp
    would be true, not the schema rollback itself.
    """

    @event.listens_for(engine, "connect")
    def _disable_pysqlite_legacy_transaction_control(
        dbapi_connection: DBAPIConnection, _connection_record: ConnectionPoolEntry
    ) -> None:
        # `isolation_level` is pysqlite's own attribute, not part of the
        # generic `DBAPIConnection` protocol every dialect's "connect"
        # event shares — this listener is only ever registered for the
        # SQLite dialect (see the `dialect.name == "sqlite"` guard below).
        dbapi_connection.isolation_level = None  # type: ignore[attr-defined]

    @event.listens_for(engine, "begin")
    def _emit_explicit_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")


def _include_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """Keep non-schema tables out of autogenerate and `alembic check`.

    A table can opt out with ``__table_args__ = {"info": {"skip_autogenerate":
    True}}``.  The throwaway models that test modules register on the shared
    declarative `Base` do exactly that: they exist to exercise `Base` itself,
    they are never migrated, and without this every drift check run inside a
    full test session reported them as pending schema changes — standing noise
    that would hide a real difference.
    """

    del name, reflected, compare_to
    if type_ == "table":
        return not bool(getattr(obj, "info", {}).get("skip_autogenerate", False))
    return True


def _database_url() -> str:
    """The URL to migrate: an explicit override if one was set on the
    `Config`, otherwise the application's configured database."""
    if _CONFIGURED_URL:
        return _CONFIGURED_URL
    return get_settings().database.url


def run_migrations_offline() -> None:
    """Emit migration SQL against a URL without opening a connection —
    `alembic upgrade head --sql`, used to review DDL before it runs."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_item=_render_item,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with an actual database connection.

    `render_as_batch` applies only on SQLite: its `ALTER TABLE` support is
    narrow enough that Alembic's batch mode (rebuild-and-swap) is the only
    portable way a later revision can alter an existing column, and it is
    a harmless no-op for the single `CREATE TABLE`-only revision this
    module ships with today.
    """
    url = _database_url()
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    if connectable.dialect.name == "sqlite":
        _use_transactional_sqlite_ddl(connectable)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=connection.dialect.name == "sqlite",
            render_item=_render_item,
            include_object=_include_object,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
