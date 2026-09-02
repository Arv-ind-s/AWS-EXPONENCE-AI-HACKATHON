"""Tests for `T-010`: the first Alembic revision, the drift check, and
dual-engine support (`plan.md §5`'s migration rule).

The SQLite half of every test runs against a real on-disk database — a
temp file, never `:memory:`, since each helper in `covenant_radar.cli`
opens its own connection and `:memory:` does not survive that. The
PostgreSQL half runs against the same `COVENANT_RADAR_DATABASE_URL`
instance `tests/integration` requires, and fails loudly rather than
skipping silently when that service is unavailable — the policy
`plan.md`'s open question on CI PostgreSQL settles for exactly this
situation.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from covenant_radar import cli
from covenant_radar.db.base import Base

pytestmark = pytest.mark.migration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_URL_ENV = "COVENANT_RADAR_DATABASE_URL"


def _sqlite_url(tmp_path: Path, name: str = "migration.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def _postgresql_url() -> str:
    database_url = os.environ.get(_DATABASE_URL_ENV)
    if not database_url:
        pytest.fail(f"{_DATABASE_URL_ENV} is required for the PostgreSQL half of this test.")
    return database_url


def _model_table_names() -> set[str]:
    return set(Base.metadata.tables.keys())


def _actual_table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _assert_upgrade_creates_every_table(database_url: str) -> None:
    exit_code = cli.run_migrate_upgrade("head", database_url=database_url, stream=io.StringIO())
    assert exit_code == 0

    engine = create_engine(database_url)
    try:
        actual = _actual_table_names(engine)
    finally:
        engine.dispose()

    expected = _model_table_names()
    missing = expected - actual
    assert not missing, f"tables missing after upgrade: {sorted(missing)}"


def test_upgrade_creates_every_table_both_engines(tmp_path: Path) -> None:
    _assert_upgrade_creates_every_table(_sqlite_url(tmp_path))
    _assert_upgrade_creates_every_table(_postgresql_url())


def test_downgrade_returns_to_base(tmp_path: Path) -> None:
    database_url = _sqlite_url(tmp_path)
    stream = io.StringIO()

    assert cli.run_migrate_upgrade("head", database_url=database_url, stream=stream) == 0
    assert cli.run_migrate_downgrade("base", database_url=database_url, stream=stream) == 0

    assert _current_revision(database_url) is None

    engine = create_engine(database_url)
    try:
        actual = _actual_table_names(engine)
    finally:
        engine.dispose()

    model_tables = _model_table_names()
    assert not (actual & model_tables), (
        f"tables survived downgrade: {sorted(actual & model_tables)}"
    )


def test_check_detects_model_drift(tmp_path: Path) -> None:
    drifted_url = _sqlite_url(tmp_path, "drifted.db")
    drift_stream = io.StringIO()
    drift_exit_code = cli.run_migrate_check(database_url=drifted_url, stream=drift_stream)
    assert drift_exit_code == 1
    assert "Model drift detected" in drift_stream.getvalue()

    clean_url = _sqlite_url(tmp_path, "clean.db")
    clean_stream = io.StringIO()
    assert cli.run_migrate_upgrade("head", database_url=clean_url, stream=clean_stream) == 0

    check_stream = io.StringIO()
    check_exit_code = cli.run_migrate_check(database_url=clean_url, stream=check_stream)
    assert check_exit_code == 0
    assert "No new upgrade operations detected" in check_stream.getvalue()


def test_failed_migration_rolls_back_and_keeps_revision(tmp_path: Path) -> None:
    database_url = _sqlite_url(tmp_path, "conflict.db")

    # `covenant_version` depends on `covenant`, `ratio_definition`, `document`,
    # `document_span` and `app_user`, so several tables are created before it
    # in dependency order — pre-creating it with an incompatible schema
    # forces the real migration to fail *after* real progress, not at the
    # first statement, so a rollback that merely undid nothing would not
    # pass this test by accident.
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE covenant_version (id TEXT)"))
    finally:
        engine.dispose()

    stream = io.StringIO()
    exit_code = cli.run_migrate_upgrade("head", database_url=database_url, stream=stream)
    assert exit_code == 1
    assert "Migration failed" in stream.getvalue()

    assert _current_revision(database_url) is None

    engine = create_engine(database_url)
    try:
        actual = _actual_table_names(engine)
    finally:
        engine.dispose()

    # The pre-existing conflicting table is the only survivor; every table
    # the migration would have created before hitting it was rolled back
    # along with everything after.
    assert actual == {"covenant_version"}


_CREATE_ALL_CALL = "create_all("


def test_create_all_not_used_in_source() -> None:
    offending: list[str] = []
    for path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        text_content = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text_content.splitlines(), start=1):
            if _CREATE_ALL_CALL in line:
                offending.append(f"{path.relative_to(_REPO_ROOT)}:{line_number}: {line.strip()!r}")

    assert offending == [], "metadata.create_all() found outside tests:\n" + "\n".join(offending)
