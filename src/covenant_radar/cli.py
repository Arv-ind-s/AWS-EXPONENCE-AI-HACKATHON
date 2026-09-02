"""Command-line entry point for Covenant Radar."""

from __future__ import annotations

import argparse
import errno
import getpass
import json
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Final, TextIO
from uuid import UUID

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError as AlembicCommandError
from argon2 import PasswordHasher
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from covenant_radar.ai import create_provider
from covenant_radar.ai.errors import ProviderError
from covenant_radar.ai.masking import MASKING_MARKER, MaskedPrompt
from covenant_radar.ai.providers.recorded import (
    MAX_CASSETTE_BYTES,
    CassetteError,
    RecordedProvider,
    RecordingProvider,
    cassette_key,
)
from covenant_radar.audit.bundle import verify_bundle
from covenant_radar.config.settings import Settings, SettingsError, get_settings, load_settings
from covenant_radar.core.clock import SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.db.migrations.support import IrreversibleMigrationError
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    IndustryReference,
    Permission,
    Portfolio,
    Role,
    RolePermission,
    UserPortfolioScope,
    UserRole,
)
from covenant_radar.db.seed import (
    ReferenceDataError,
    SeedLoader,
    SeedReport,
    deterministic_catalog_hash,
)
from covenant_radar.db.seed.demo import seed_demo_covenants
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse, LLMProvider
from covenant_radar.scheduler import default_registry
from covenant_radar.scheduler.jobs import JobRegistry
from covenant_radar.scheduler.ledger import JobAlreadyRunningError
from covenant_radar.scheduler.pipeline import PIPELINE_JOB_NAME, PIPELINE_STEPS
from covenant_radar.scheduler.runner import JobRunner, SchedulerShuttingDownError
from covenant_radar.services.nightly_runtime import build_nightly_runtime

COMMAND_TASKS = {
    "serve": "T-022",
    "migrate": "T-010",
    "seed": "T-011",
    "user": "T-011",
    "gate": "T-002",
    "job": "T-120",
    "perf": "T-160",
    "diag": "T-142",
    "cassette": "T-091",
    "bundle": "T-069",
}


@dataclass(frozen=True)
class GateStep:
    """One ordered gate step and its nox session."""

    name: str
    session: str
    implementing_task: str | None = None


FAST_GATE_STEPS: Final[tuple[GateStep, ...]] = (
    GateStep("format", "format"),
    GateStep("lint", "lint"),
    GateStep("type-check", "types"),
    GateStep("import-contracts", "imports"),
    GateStep("unit-property-tests", "tests"),
    GateStep("alembic-drift", "alembic_drift"),
)

FULL_GATE_STEPS: Final[tuple[GateStep, ...]] = FAST_GATE_STEPS + (
    GateStep("integration-tests", "integration", "T-003"),
    GateStep("contract-tests", "contract", "T-135"),
    GateStep("seed-determinism", "seed", "T-011"),
    GateStep("evaluation", "evaluation", "T-104"),
    GateStep("end-to-end-tests", "e2e", "T-073"),
    GateStep("accessibility-tests", "a11y", "T-075"),
    GateStep("security-scanning", "security", "T-019"),
    GateStep("performance", "performance", "T-160"),
)

CommandRunner = Callable[[Sequence[str]], int]
ExecutableFinder = Callable[[str], str | None]
PortAvailabilityChecker = Callable[[str, int], bool]


def _write(stream: TextIO, message: str) -> None:
    stream.write(f"{message}\n")


def _port_is_available(host: str, port: int) -> bool:
    """Check every address family uvicorn could bind before starting it."""
    try:
        addresses = socket.getaddrinfo(
            host or "0.0.0.0",
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except OSError:
        return False
    if not addresses:
        return False

    for family, socket_type, protocol, _, address in addresses:
        probe: socket.socket | None = None
        try:
            probe = socket.socket(family, socket_type, protocol)
            probe.bind(address)
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return False
            return False
        finally:
            if probe is not None:
                probe.close()
    return True


def _run_preflight_self_checks(settings: Settings, stream: TextIO) -> int:
    """The subset of `T-149`'s startup self-checks that need only a
    database connection, run once in `radarctl serve`'s own process before
    any worker is spawned — naming the failing check and refusing to start
    rather than launching N workers that would all fail the same way.

    `document_store` and `scheduler` are checked again, alongside these,
    inside each worker's own ASGI startup event
    (`web.application.create_production_app`'s `ApplicationLifecycle`),
    since those need the full composition root this lightweight pre-flight
    deliberately avoids building.
    """
    from covenant_radar.db.session import create_database_engine
    from covenant_radar.lifecycle import (
        clock_skew_self_check,
        configuration_self_check,
        database_self_check,
        migrations_at_head_self_check,
        perform_startup,
    )

    engine = create_database_engine(settings.database)
    try:
        checks = (
            configuration_self_check(settings),
            migrations_at_head_self_check(settings.database.url),
            database_self_check(engine),
            clock_skew_self_check(engine),
        )
        return perform_startup(checks, stream=stream)
    finally:
        engine.dispose()


def run_serve(
    *,
    settings: Settings | None = None,
    host: str | None = None,
    port: int | None = None,
    workers: int | None = None,
    port_checker: PortAvailabilityChecker = _port_is_available,
    server_runner: Callable[..., object] | None = None,
    self_check_runner: Callable[[Settings, TextIO], int] = _run_preflight_self_checks,
    stream: TextIO = sys.stdout,
) -> int:
    """Run the ASGI service, returning a stable code for a busy listener
    or a failed startup self-check (`C-70`, `spec §N-06.b`)."""
    resolved = settings or get_settings()
    listen_host = host if host is not None else resolved.web.host
    listen_port = port if port is not None else resolved.web.port
    process_count = workers if workers is not None else resolved.web.workers
    if not 1 <= listen_port <= 65535:
        _write(stream, f"Invalid service port: {listen_port}. Expected 1 through 65535.")
        return 2
    if process_count < 1:
        _write(stream, f"Invalid worker count: {process_count}. Expected a positive integer.")
        return 2
    if not port_checker(listen_host, listen_port):
        _write(stream, f"Configured port {listen_port} is busy; server was not started.")
        return 3

    self_check_exit_code = self_check_runner(resolved, stream)
    if self_check_exit_code != 0:
        return self_check_exit_code

    if server_runner is None:
        import uvicorn

        server_runner = uvicorn.run
    try:
        server_runner(
            "covenant_radar.web.application:create_production_app",
            host=listen_host,
            port=listen_port,
            workers=process_count,
            factory=True,
            access_log=False,
            log_config=None,
        )
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            _write(stream, f"Configured port {listen_port} became busy; server was not started.")
            return 3
        _write(stream, f"Server failed to start: {error.__class__.__name__}.")
        return 1
    return 0


def _run_command(command: Sequence[str]) -> int:
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        return 127


def _select_gate_steps(fast: bool, selected_steps: Sequence[str]) -> tuple[GateStep, ...] | None:
    available_steps = FAST_GATE_STEPS if fast else FULL_GATE_STEPS
    if not selected_steps:
        return available_steps

    steps_by_name = {step.name: step for step in available_steps}
    if any(step_name not in steps_by_name for step_name in selected_steps):
        return None

    selected_names = set(selected_steps)
    return tuple(step for step in available_steps if step.name in selected_names)


def run_gate(
    *,
    fast: bool,
    selected_steps: Sequence[str] = (),
    command_runner: CommandRunner = _run_command,
    executable_finder: ExecutableFinder = which,
    stream: TextIO = sys.stdout,
) -> int:
    """Run gate sessions in their contractual order and stop at the first failure."""
    steps = _select_gate_steps(fast, selected_steps)
    if steps is None:
        valid_steps = FAST_GATE_STEPS if fast else FULL_GATE_STEPS
        valid_names = ", ".join(step.name for step in valid_steps)
        _write(stream, f"Unknown gate step. Valid steps: {valid_names}")
        return 2

    nox_command = executable_finder("nox")
    if nox_command is None:
        _write(stream, "Required tool unavailable: nox")
        return 127

    for step in steps:
        if step.implementing_task is not None:
            _write(
                stream,
                f"SKIP {step.name} — not yet implemented ({step.implementing_task})",
            )
            continue

        _write(stream, f"RUN {step.name}")
        exit_code = command_runner((nox_command, "--session", step.session))
        if exit_code == 127:
            _write(stream, "Required tool unavailable: nox")
            return exit_code
        if exit_code != 0:
            _write(stream, f"FAIL {step.name} — exit {exit_code}")
            return exit_code

    return 0


_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_ALEMBIC_INI_PATH: Final[Path] = _REPO_ROOT / "alembic.ini"
_MIGRATIONS_SCRIPT_LOCATION: Final[Path] = Path(__file__).resolve().parent / "db" / "migrations"


def _alembic_config(database_url: str, stream: TextIO) -> AlembicConfig:
    """Build an Alembic `Config` pointed at this package's migrations,
    independent of the caller's working directory."""
    config = AlembicConfig(str(_ALEMBIC_INI_PATH), stdout=stream)
    config.set_main_option("script_location", str(_MIGRATIONS_SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _resolve_database_url(override: str | None) -> str:
    """The database `radarctl migrate` acts on: an explicit override —
    `--database-url`, chiefly for `tests/migration` — or else the
    application's own configured database, so this command never becomes
    a second, competing source of truth for where the database lives."""
    if override is not None:
        return override
    return get_settings().database.url


def _current_revision(database_url: str) -> str | None:
    """The revision the database is stamped at, or `None` for an empty
    (unmigrated) database."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _refuse_unknown_current_revision(
    script: ScriptDirectory, current: str | None, target: str, stream: TextIO
) -> bool:
    """Refuse to migrate a database stamped at a revision this migration
    history does not contain, naming both — the one case none of
    Alembic's own commands name on their own. Returns whether it refused."""
    if current is None:
        return False
    try:
        script.get_revision(current)
    except AlembicCommandError:
        _write(
            stream,
            f"Refusing to migrate: the database is at revision {current!r}, which this "
            f"migration history does not contain (requested target {target!r}). The "
            "migration history may have been rewritten, or the database restored "
            "from a different branch.",
        )
        return True
    return False


def run_migrate_upgrade(
    revision: str = "head",
    *,
    database_url: str | None = None,
    stream: TextIO = sys.stdout,
) -> int:
    """`radarctl migrate upgrade [REVISION]` (`C-71`): reach `revision`,
    refusing a database at an unknown revision and leaving the recorded
    revision unchanged if the migration fails part-way."""
    resolved_url = _resolve_database_url(database_url)
    config = _alembic_config(resolved_url, stream)
    script = ScriptDirectory.from_config(config)
    if _refuse_unknown_current_revision(script, _current_revision(resolved_url), revision, stream):
        return 2

    try:
        alembic_command.upgrade(config, revision)
    except (AlembicCommandError, SQLAlchemyError) as error:
        _write(stream, f"Migration failed: {error}")
        return 1
    return 0


def run_migrate_downgrade(
    revision: str = "base",
    *,
    database_url: str | None = None,
    stream: TextIO = sys.stdout,
) -> int:
    """`radarctl migrate downgrade [REVISION]` (`C-71`): reverse to
    `revision`, refusing a database at an unknown revision and refusing a
    revision declared irreversible rather than silently doing nothing."""
    resolved_url = _resolve_database_url(database_url)
    config = _alembic_config(resolved_url, stream)
    script = ScriptDirectory.from_config(config)
    if _refuse_unknown_current_revision(script, _current_revision(resolved_url), revision, stream):
        return 2

    try:
        alembic_command.downgrade(config, revision)
    except IrreversibleMigrationError as error:
        _write(stream, str(error))
        return 3
    except (AlembicCommandError, SQLAlchemyError) as error:
        _write(stream, f"Downgrade failed: {error}")
        return 1
    return 0


def run_migrate_check(*, database_url: str | None = None, stream: TextIO = sys.stdout) -> int:
    """`radarctl migrate check` (`C-71`): fail, naming the difference, if
    the models and the migration head disagree."""
    resolved_url = _resolve_database_url(database_url)
    config = _alembic_config(resolved_url, stream)
    try:
        alembic_command.check(config)
    except AlembicCommandError as error:
        _write(stream, f"Model drift detected: {error}")
        return 1
    return 0


def run_migrate_current(*, database_url: str | None = None, stream: TextIO = sys.stdout) -> int:
    """`radarctl migrate current` (`C-71`): print the current revision."""
    resolved_url = _resolve_database_url(database_url)
    config = _alembic_config(resolved_url, stream)
    alembic_command.current(config)
    return 0


def _build_migrate_parser(parser: argparse.ArgumentParser) -> None:
    """Attach the `migrate` command group's `upgrade`/`downgrade`/`check`/
    `current` subcommands to the already-created `migrate` subparser."""
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override the configured database URL (chiefly for tests/migration).",
    )
    migrate_commands = parser.add_subparsers(dest="migrate_command", title="migrate commands")

    upgrade_parser = migrate_commands.add_parser("upgrade", help="Upgrade to a later revision")
    upgrade_parser.add_argument("revision", nargs="?", default="head")

    downgrade_parser = migrate_commands.add_parser(
        "downgrade", help="Downgrade to an earlier revision"
    )
    downgrade_parser.add_argument("revision", nargs="?", default="base")

    migrate_commands.add_parser("check", help="Fail if models and the migration head disagree")
    migrate_commands.add_parser("current", help="Print the current revision")


def _run_migrate(args: argparse.Namespace, stream: TextIO = sys.stdout) -> int:
    if args.migrate_command is None:
        _write(stream, "migrate requires a command: upgrade, downgrade, check, or current.")
        return 2
    if args.migrate_command == "upgrade":
        return run_migrate_upgrade(args.revision, database_url=args.database_url, stream=stream)
    if args.migrate_command == "downgrade":
        return run_migrate_downgrade(args.revision, database_url=args.database_url, stream=stream)
    if args.migrate_command == "check":
        return run_migrate_check(database_url=args.database_url, stream=stream)
    if args.migrate_command == "current":
        return run_migrate_current(database_url=args.database_url, stream=stream)
    raise AssertionError(f"unhandled migrate command: {args.migrate_command}")


def _is_development_database(database_url: str) -> bool:
    """Return whether a URL is unambiguously a development database.

    SQLite is the application's offline/development engine.  PostgreSQL is
    never inferred to be safe for destructive operations from a hostname or
    database name, because production deployments commonly use local or
    otherwise non-descriptive PostgreSQL endpoints.
    """
    return database_url.lower().split(":", maxsplit=1)[0] in {"sqlite", "sqlite+pysqlite"}


def _reset_reference_data(session: Session) -> None:
    """Remove only system reference rows, refusing rows that are in use."""
    system_role_ids = list(session.scalars(select(Role.id).where(Role.is_system.is_(True))).all())
    if system_role_ids:
        assigned_user = session.scalar(
            select(UserRole.user_id).where(UserRole.role_id.in_(system_role_ids)).limit(1)
        )
        if assigned_user is not None:
            raise ReferenceDataError(
                "Cannot reset system roles while a user assignment exists; "
                "remove the assignments through the administration workflow first."
            )
        session.execute(delete(RolePermission).where(RolePermission.role_id.in_(system_role_ids)))
        session.execute(delete(Role).where(Role.id.in_(system_role_ids)))

    session.execute(delete(Permission))

    referenced_industry = session.scalar(
        select(Borrower.id)
        .join(IndustryReference, Borrower.industry_code == IndustryReference.code)
        .limit(1)
    )
    if referenced_industry is not None:
        raise ReferenceDataError(
            "Cannot reset the industry taxonomy while a borrower references it; "
            "resetting would break historical resolution."
        )
    session.execute(delete(IndustryReference))


def _write_seed_report(report: SeedReport, stream: TextIO) -> None:
    """Print a stable, non-sensitive seed summary."""
    if report.changed:
        _write(
            stream,
            f"Seeded reference data: {report.total_changes} change(s); "
            f"catalog hash {report.catalog_hash}.",
        )
    else:
        _write(stream, f"No changes: reference data is already current ({report.catalog_hash}).")
    retained = sum(report.retained.values())
    if retained:
        _write(stream, f"Retained {retained} superseded industry row(s) for historical resolution.")


def run_seed(
    *,
    database_url: str | None = None,
    reference_portfolio: bool = False,
    demo_covenants: bool = False,
    reset: bool = False,
    i_understand: bool = False,
    check_deterministic: bool = False,
    signal_days: int | None = None,
    stream: TextIO = sys.stdout,
) -> int:
    """Implement ``radarctl seed`` (`C-72`)."""
    if demo_covenants:
        return _run_demo_covenant_seed(database_url=database_url, stream=stream)
    if reference_portfolio:
        return _run_reference_portfolio_seed(
            database_url=database_url,
            reset=reset,
            i_understand=i_understand,
            check_deterministic=check_deterministic,
            signal_days=signal_days,
            stream=stream,
        )

    resolved_url = _resolve_database_url(database_url)
    if reset and not _is_development_database(resolved_url) and not i_understand:
        _write(
            stream,
            "Refusing --reset on a non-development database. "
            "Repeat with --i-understand after verifying the target.",
        )
        return 2

    if check_deterministic:
        try:
            first_hash = deterministic_catalog_hash()
            second_hash = deterministic_catalog_hash()
        except ReferenceDataError as error:
            _write(stream, f"Seed determinism check failed: {error}")
            return 1
        if first_hash != second_hash:
            _write(
                stream,
                f"Seed determinism check failed: hashes differ ({first_hash} != {second_hash}).",
            )
            return 1
        _write(stream, f"Seed determinism check passed: {first_hash}.")

    engine = create_engine(resolved_url, pool_pre_ping=True)
    try:
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with session_factory() as session:
            try:
                if reset:
                    with session.begin():
                        _reset_reference_data(session)
                        report = SeedLoader(
                            session,
                            clock=SystemClock(),
                            request_id="seed-" + new_request_id(),
                        ).load()
                else:
                    report = SeedLoader(
                        session,
                        clock=SystemClock(),
                        request_id="seed-" + new_request_id(),
                    ).load()
            except ReferenceDataError as error:
                _write(stream, f"Seed failed: {error}")
                return 1
            except (IntegrityError, SQLAlchemyError) as error:
                _write(
                    stream,
                    f"Seed failed: database rejected the operation ({error.__class__.__name__}).",
                )
                return 1
        _write_seed_report(report, stream)
        return 0
    except SQLAlchemyError as error:
        _write(stream, f"Seed failed: database unavailable ({error.__class__.__name__}).")
        return 1
    finally:
        engine.dispose()


def _run_demo_covenant_seed(*, database_url: str | None, stream: TextIO) -> int:
    """Populate the showcase data through the real registry and engine."""

    resolved_url = _resolve_database_url(database_url)
    engine = create_engine(resolved_url, pool_pre_ping=True)
    try:
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        system_actor_id = _ensure_demo_system_actor(session_factory)
        with session_factory() as session:
            try:
                with session.begin():
                    report = seed_demo_covenants(
                        session,
                        system_actor_id=system_actor_id,
                        clock=SystemClock(),
                        signal_path="var/inbox/covenant-radar-demo-signals.json",
                    )
            except (ReferenceDataError, ValueError, IntegrityError, SQLAlchemyError) as error:
                _write(stream, f"Demo covenant seed failed: {error}")
                return 1
        _write(
            stream,
            "Seeded Phase 7A demo: "
            f"{report.borrowers} borrowers, {report.covenants_created} covenants, "
            f"{report.periods_created} periods, {report.tests_created} tests, "
            f"{report.signal_events} signal events, "
            f"threshold snapshot {report.threshold_snapshot_id}.",
        )
        return 0
    except SQLAlchemyError as error:
        _write(
            stream,
            "Demo covenant seed failed: database unavailable "
            f"({error.__class__.__name__}).",
        )
        return 1
    finally:
        engine.dispose()


def _ensure_demo_system_actor(session_factory: Callable[[], Session]) -> UUID:
    """Resolve the durable system actor without importing web composition."""

    from covenant_radar.services.nightly_runtime import ensure_system_actor

    return ensure_system_actor(session_factory)


def _run_reference_portfolio_seed(
    *,
    database_url: str | None,
    reset: bool,
    i_understand: bool,
    check_deterministic: bool,
    signal_days: int | None,
    stream: TextIO,
) -> int:
    """Generate and load the offline evaluation portfolio atomically."""
    from evaluation.reference_portfolio import (
        ReferencePortfolioError,
        generate_reference_portfolio,
        load_reference_portfolio,
    )
    from evaluation.reference_portfolio.generator import clear_reference_portfolio
    from evaluation.reference_portfolio.signals import DEFAULT_SIGNAL_DAYS

    # Resolved here rather than as a parameter default so `cli` keeps its lazy
    # dependency on the optional `evaluation` package.
    resolved_signal_days = DEFAULT_SIGNAL_DAYS if signal_days is None else signal_days
    if resolved_signal_days < 1:
        _write(stream, "--signal-days must be a positive number of days.")
        return 2

    resolved_url = _resolve_database_url(database_url)
    if reset and not _is_development_database(resolved_url) and not i_understand:
        _write(
            stream,
            "Refusing --reset on a non-development database. "
            "Repeat with --i-understand after verifying the target.",
        )
        return 2

    try:
        portfolio = generate_reference_portfolio()
        if check_deterministic:
            comparison = generate_reference_portfolio()
            first_hashes = portfolio.content_hashes()
            second_hashes = comparison.content_hashes()
            if first_hashes != second_hashes:
                _write(
                    stream,
                    "Reference portfolio determinism check failed: table hashes differ.",
                )
                return 1
            table_hashes = ", ".join(
                f"{name}={first_hashes[name]}" for name in sorted(first_hashes)
            )
            _write(
                stream,
                "Reference portfolio determinism check passed: "
                f"{table_hashes}; overall={portfolio.content_hash}.",
            )
    except ReferencePortfolioError as error:
        _write(stream, f"Reference portfolio generation failed: {error}")
        return 1

    engine = create_engine(resolved_url, pool_pre_ping=True)
    try:
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with session_factory() as session:
            try:
                with session.begin():
                    if reset:
                        clear_reference_portfolio(session)
                    SeedLoader(
                        session,
                        clock=SystemClock(),
                        request_id="seed-" + new_request_id(),
                    ).load()
                    load_reference_portfolio(
                        session,
                        portfolio,
                        clock=SystemClock(),
                        request_id="reference-" + new_request_id(),
                        signal_days=resolved_signal_days,
                    )
            except (ReferenceDataError, ReferencePortfolioError) as error:
                _write(stream, f"Reference portfolio seed failed: {error}")
                return 1
            except (IntegrityError, SQLAlchemyError) as error:
                _write(
                    stream,
                    "Reference portfolio seed failed: database rejected the operation "
                    f"({error.__class__.__name__}).",
                )
                return 1
        _write(
            stream,
            f"Loaded synthetic reference portfolio: {len(portfolio.borrowers)} borrowers, "
            f"{len(portfolio.facilities)} facilities, "
            f"{len(portfolio.financials)} financial periods; hash {portfolio.content_hash}.",
        )
        return 0
    except SQLAlchemyError as error:
        _write(
            stream,
            f"Reference portfolio seed failed: database unavailable ({error.__class__.__name__}).",
        )
        return 1
    finally:
        engine.dispose()


def _validate_user_input(
    username: str,
    role: str,
    email: str | None,
    full_name: str | None,
    portfolios: Sequence[str],
) -> tuple[str, str, str, str, tuple[str, ...]]:
    normalized_username = username.strip()
    if not normalized_username or len(normalized_username) > 64:
        raise ReferenceDataError("username must be between 1 and 64 characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized_username):
        raise ReferenceDataError("username contains a control character.")
    normalized_role = role.strip()
    if not normalized_role:
        raise ReferenceDataError("role must be a non-empty system role code.")
    normalized_email = (email or f"{normalized_username}@local.invalid").strip()
    normalized_name = (full_name or normalized_username).strip()
    if not normalized_email or len(normalized_email) > 254:
        raise ReferenceDataError("email must be between 1 and 254 characters.")
    if not normalized_name or len(normalized_name) > 200:
        raise ReferenceDataError("full_name must be between 1 and 200 characters.")
    normalized_portfolios = tuple(
        dict.fromkeys(value.strip() for value in portfolios if value.strip())
    )
    if len(normalized_portfolios) != len(portfolios):
        raise ReferenceDataError("portfolio codes must be non-empty and may not be repeated.")
    return (
        normalized_username,
        normalized_role,
        normalized_email,
        normalized_name,
        normalized_portfolios,
    )


def run_user_create(
    *,
    username: str,
    role: str,
    portfolios: Sequence[str] = (),
    email: str | None = None,
    full_name: str | None = None,
    database_url: str | None = None,
    password_reader: Callable[[str], str] | None = None,
    stream: TextIO = sys.stdout,
) -> int:
    """Create a local user without ever accepting a password as an argument."""
    try:
        normalized = _validate_user_input(username, role, email, full_name, portfolios)
        read_password = password_reader or getpass.getpass
        initial_password = read_password("Initial password (input is hidden): ")
        confirmation = read_password("Repeat initial password (input is hidden): ")
        if not initial_password or initial_password != confirmation:
            _write(stream, "User creation refused: passwords must be non-empty and match.")
            return 2
        if len(initial_password) < 12:
            _write(
                stream,
                "User creation refused: the initial password must be at least 12 characters.",
            )
            return 2
        password_hash = PasswordHasher().hash(initial_password)
    except (EOFError, OSError) as error:
        _write(
            stream,
            f"User creation refused: password input is unavailable ({error.__class__.__name__}).",
        )
        return 2
    except ReferenceDataError as error:
        _write(stream, f"User creation refused: {error}")
        return 2

    resolved_url = _resolve_database_url(database_url)
    engine = create_engine(resolved_url, pool_pre_ping=True)
    try:
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with session_factory() as session:
            try:
                with session.begin():
                    username_value, role_value, email_value, full_name_value, portfolio_values = (
                        normalized
                    )
                    if session.scalar(select(AppUser.id).where(AppUser.username == username_value)):
                        raise ReferenceDataError(f"username {username_value!r} already exists.")
                    if session.scalar(
                        select(AppUser.id).where(
                            AppUser.email == email_value,
                            AppUser.is_active.is_(True),
                        )
                    ):
                        raise ReferenceDataError(f"active email {email_value!r} already exists.")
                    role_record = session.scalar(select(Role).where(Role.code == role_value))
                    if role_record is None:
                        raise ReferenceDataError(
                            f"role {role_value!r} does not exist; run radarctl seed first."
                        )
                    created_at = SystemClock().now()
                    request_id = new_request_id()
                    user = AppUser(
                        username=username_value,
                        email=email_value,
                        full_name=full_name_value,
                        password_hash=password_hash,
                        auth_source="local",
                        external_subject=None,
                        is_active=True,
                        mfa_secret_enc=None,
                        failed_attempts=0,
                        locked_until=None,
                        password_changed_at=None,
                        must_change_password=True,
                        locale="en",
                        theme="light",
                        created_at=created_at,
                        updated_at=created_at,
                        created_by_id=None,
                        updated_by_id=None,
                        request_id=request_id,
                        version=1,
                    )
                    session.add(user)
                    session.flush()
                    session.add(
                        UserRole(
                            user_id=user.id,
                            role_id=role_record.id,
                            granted_by_id=None,
                            granted_at=user.created_at,
                            created_at=user.created_at,
                            updated_at=user.created_at,
                            created_by_id=None,
                            updated_by_id=None,
                            request_id=user.request_id,
                        )
                    )
                    for portfolio_code in portfolio_values:
                        portfolio = session.scalar(
                            select(Portfolio).where(Portfolio.code == portfolio_code)
                        )
                        if portfolio is None:
                            raise ReferenceDataError(
                                f"portfolio {portfolio_code!r} does not exist; no user was created."
                            )
                        session.add(
                            UserPortfolioScope(
                                user_id=user.id,
                                portfolio_id=portfolio.id,
                                include_descendants=True,
                                created_at=user.created_at,
                                updated_at=user.created_at,
                                created_by_id=None,
                                updated_by_id=None,
                                request_id=user.request_id,
                                version=1,
                            )
                        )
            except ReferenceDataError as error:
                _write(stream, f"User creation refused: {error}")
                return 2
            except (IntegrityError, SQLAlchemyError) as error:
                _write(
                    stream,
                    "User creation failed: database rejected the operation "
                    f"({error.__class__.__name__}).",
                )
                return 1
        _write(
            stream,
            f"Created user {normalized[0]!r} with role {normalized[1]!r}; "
            "password change required.",
        )
        return 0
    except SQLAlchemyError as error:
        _write(stream, f"User creation failed: database unavailable ({error.__class__.__name__}).")
        return 1
    finally:
        engine.dispose()


def _build_seed_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--demo-covenants",
        action="store_true",
        help="Seed the Phase 7A named borrower/covenant demo data.",
    )
    parser.add_argument(
        "--reference-portfolio",
        action="store_true",
        help="Generate the synthetic reference portfolio when that module is installed.",
    )
    parser.add_argument("--reset", action="store_true", help="Reset seeded reference rows first.")
    parser.add_argument(
        "--i-understand",
        action="store_true",
        help="Confirm a reset target after verifying the database environment.",
    )
    parser.add_argument(
        "--check-deterministic",
        action="store_true",
        help="Build the catalogs twice and compare their canonical hashes.",
    )
    parser.add_argument(
        "--signal-days",
        type=int,
        default=None,
        help=(
            "Days of daily signal history to generate per borrower with "
            "--reference-portfolio (default 365). Each day produces six events "
            "per borrower, so this is the main lever on seed size and time."
        ),
    )
    parser.add_argument("--database-url", default=None, help=argparse.SUPPRESS)


def _build_cassette_parser(parser: argparse.ArgumentParser) -> None:
    """Build the offline model-response cassette command group."""
    commands = parser.add_subparsers(dest="cassette_command", title="cassette commands")

    record = commands.add_parser("record", help="Record one masked request and live response")
    record.add_argument(
        "--path",
        "--output",
        dest="cassette_path",
        type=Path,
        default=Path("evaluation/cassettes"),
        help="Cassette file or directory to update",
    )
    record.add_argument(
        "--request",
        type=Path,
        default=None,
        help="JSON file containing one already-masked request",
    )
    record.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional settings TOML file selecting the live provider",
    )

    replay = commands.add_parser("replay", help="Replay a response from a cassette")
    replay.add_argument(
        "--path",
        dest="cassette_path",
        type=Path,
        default=Path("evaluation/cassettes"),
        help="Cassette file or directory to read",
    )
    replay.add_argument(
        "--request",
        type=Path,
        default=None,
        help="JSON file containing the masked request to replay",
    )
    replay.add_argument(
        "--key",
        default=None,
        help="Explicit cassette key (use --request for hash-verified replay)",
    )


def _load_masked_request(path: Path) -> tuple[MaskedPrompt, CompletionRequest]:
    """Read the strict request format used by ``radarctl cassette``."""
    try:
        if not path.is_file():
            raise ValueError("request file was not found")
        if path.stat().st_size > MAX_CASSETTE_BYTES:
            raise ValueError("request file exceeds the 16 MiB limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error):
            raise ValueError(f"request file could not be read: {error}") from error
        raise ValueError("request file could not be read safely") from error

    if not isinstance(payload, dict):
        raise ValueError("request file must contain a JSON object")
    request_payload = payload.get("request", payload)
    if not isinstance(request_payload, dict):
        raise ValueError("request object is invalid")
    if request_payload.get("masking_marker") != MASKING_MARKER:
        raise ValueError("request must carry the masking marker")
    version = request_payload.get("prompt_version")
    model = request_payload.get("model")
    messages = request_payload.get("messages")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("request prompt_version is required")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("request model is required")
    if not isinstance(messages, list):
        raise ValueError("request messages are required")

    try:
        prompt = MaskedPrompt(
            messages=messages,
            version=version,
            marker=MASKING_MARKER,
        )
        request = CompletionRequest(
            messages=prompt.messages,
            model=model,
            max_tokens=request_payload.get("max_tokens", 2048),
            temperature=request_payload.get("temperature", 0.0),
            timeout_seconds=request_payload.get("timeout_seconds"),
            prompt_version=version,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"request shape is invalid: {error}") from error
    return prompt, request


def _response_summary(response: object) -> str:
    """Render only the normalised response fields for CLI output."""
    if not isinstance(response, CompletionResponse):
        raise TypeError("response is not a completion response")
    value = {
        "text": response.text,
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": response.latency_ms,
        "from_cassette": response.from_cassette,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def run_cassette_record(
    *,
    cassette_path: Path | str = Path("evaluation/cassettes"),
    request_path: Path | str | None = None,
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    config_path: Path | str | None = None,
    stream: TextIO = sys.stdout,
) -> int:
    """Record one provider response, refusing when no live provider exists."""
    live_provider = provider
    if live_provider is None:
        try:
            resolved_settings = (
                load_settings(config_path)
                if config_path is not None
                else settings or get_settings()
            )
            ai_settings = resolved_settings.ai
            if ai_settings.provider in {"none", "recorded"}:
                _write(stream, "Cassette record refused: no live provider is configured.")
                return 2
            live_provider = create_provider(ai_settings)
        except (OSError, ProviderError, SettingsError, TypeError, ValueError) as error:
            _write(stream, f"Cassette record refused: {error}")
            return 2

    if request_path is None:
        _write(stream, "Cassette record refused: --request is required.")
        return 2
    try:
        masked_prompt, request = _load_masked_request(Path(request_path))
    except ValueError as error:
        _write(stream, f"Cassette record refused: {error}")
        return 2

    try:
        with RecordingProvider(live_provider, cassette_path) as recorder:
            response = recorder.complete_masked(
                masked_prompt,
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                timeout_seconds=request.timeout_seconds,
            )
            key = cassette_key(request)
            if recorder.store.get(key) is None:
                _write(stream, "Cassette record failed: the response was not persisted.")
                return 1
    except (CassetteError, ProviderError, TypeError, ValueError, OSError) as error:
        _write(stream, f"Cassette record failed: {error}")
        return 1
    _write(stream, f"Recorded cassette {key}.")
    _write(stream, _response_summary(response))
    return 0


def run_cassette_replay(
    *,
    cassette_path: Path | str = Path("evaluation/cassettes"),
    request_path: Path | str | None = None,
    key: str | None = None,
    stream: TextIO = sys.stdout,
) -> int:
    """Replay one hash-verified response, or list the usable entry count."""
    if request_path is not None and key is not None:
        _write(stream, "Cassette replay refused: use either --request or --key, not both.")
        return 2
    try:
        provider = RecordedProvider(cassette_path=cassette_path)
        if request_path is None and key is None:
            count = provider.cassette_store.size if provider.cassette_store else 0
            _write(stream, f"Loaded {count} cassette entrie(s).")
            return 0
        if request_path is not None:
            _, request = _load_masked_request(Path(request_path))
            response = provider.replay(request)
            resolved_key = cassette_key(request)
        else:
            if key is None:
                raise ValueError("a replay key is required")
            response = provider.response_for_key(key)
            resolved_key = key
    except ProviderError as error:
        if error.reason == "cassette miss":
            _write(stream, "Cassette replay miss: no recorded response matches the request.")
            return 1
        _write(stream, f"Cassette replay failed: {error}")
        return 2
    except (CassetteError, TypeError, ValueError, OSError) as error:
        _write(stream, f"Cassette replay failed: {error}")
        return 2
    _write(stream, f"Replayed cassette {resolved_key}.")
    _write(stream, _response_summary(response))
    return 0


def _run_cassette(args: argparse.Namespace, stream: TextIO = sys.stdout) -> int:
    if args.cassette_command == "record":
        return run_cassette_record(
            cassette_path=args.cassette_path,
            request_path=args.request,
            config_path=args.config,
            stream=stream,
        )
    if args.cassette_command == "replay":
        return run_cassette_replay(
            cassette_path=args.cassette_path,
            request_path=args.request,
            key=args.key,
            stream=stream,
        )
    _write(stream, "cassette requires a command: record or replay.")
    return 2


def _build_user_parser(parser: argparse.ArgumentParser) -> None:
    user_commands = parser.add_subparsers(dest="user_command", title="user commands")
    create_parser = user_commands.add_parser("create", help="Create a local user interactively")
    create_parser.add_argument("--username", required=True)
    create_parser.add_argument("--role", required=True)
    create_parser.add_argument("--portfolio", action="append", default=[])
    create_parser.add_argument("--email", default=None)
    create_parser.add_argument("--full-name", default=None)
    create_parser.add_argument("--database-url", default=None, help=argparse.SUPPRESS)


def _run_user(args: argparse.Namespace, stream: TextIO = sys.stdout) -> int:
    if args.user_command is None:
        _write(stream, "user requires a command: create.")
        return 2
    if args.user_command == "create":
        return run_user_create(
            username=args.username,
            role=args.role,
            portfolios=args.portfolio,
            email=args.email,
            full_name=args.full_name,
            database_url=args.database_url,
            stream=stream,
        )
    raise AssertionError(f"unhandled user command: {args.user_command}")


def run_job_trigger(
    *,
    name: str,
    as_of: str | None = None,
    borrower: str | None = None,
    database_url: str | None = None,
    registry: JobRegistry | None = None,
    runner: JobRunner | None = None,
    stream: TextIO = sys.stdout,
) -> int:
    """`radarctl job run <name> [--as-of] [--borrower]` (`C-74`): trigger
    one job outside its schedule, writing a `job_run` row.

    Idempotent through the same guard `T-121`'s pipeline steps rely on: a
    job already running is refused, naming the running instance, rather
    than started a second time.
    """
    active_registry = registry if registry is not None else default_registry()
    active_runner = runner
    engine = None
    if active_runner is None:
        resolved_url = _resolve_database_url(database_url)
        engine = create_engine(resolved_url, pool_pre_ping=True)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        if registry is None and name in (PIPELINE_JOB_NAME, *PIPELINE_STEPS):
            settings = get_settings()
            if database_url is not None:
                settings = settings.model_copy(
                    update={
                        "database": settings.database.model_copy(update={"url": resolved_url})
                    }
                )
            active_runner = build_nightly_runtime(
                session_factory,
                settings,
                registry=active_registry,
            ).runner
        else:
            active_runner = JobRunner(active_registry, session_factory)

    if name not in active_registry:
        _write(stream, f"Job trigger refused: no job named {name!r} is registered.")
        if engine is not None:
            engine.dispose()
        return 2

    try:
        run = active_runner.run_now(name, trigger="manual", as_of=as_of, borrower_id=borrower)
    except (JobAlreadyRunningError, SchedulerShuttingDownError) as error:
        _write(stream, f"Job trigger refused: {error}")
        return 2
    finally:
        if engine is not None:
            engine.dispose()

    if run.state != "succeeded":
        _write(stream, f"Job {name!r} run {run.run_id} finished as {run.state}: {run.error}")
        return 1
    _write(stream, f"Job {name!r} run {run.run_id} succeeded (attempt {run.attempt}).")
    return 0


def _build_job_parser(parser: argparse.ArgumentParser) -> None:
    job_commands = parser.add_subparsers(dest="job_command", title="job commands")
    run_parser = job_commands.add_parser("run", help="Trigger one job outside its schedule")
    run_parser.add_argument("name")
    run_parser.add_argument("--as-of", default=None)
    run_parser.add_argument("--borrower", default=None)
    run_parser.add_argument("--database-url", default=None, help=argparse.SUPPRESS)


def _run_job(args: argparse.Namespace, stream: TextIO = sys.stdout) -> int:
    if args.job_command is None:
        _write(stream, "job requires a command: run.")
        return 2
    if args.job_command == "run":
        return run_job_trigger(
            name=args.name,
            as_of=args.as_of,
            borrower=args.borrower,
            database_url=args.database_url,
            stream=stream,
        )
    raise AssertionError(f"unhandled job command: {args.job_command}")


def _build_bundle_parser(parser: argparse.ArgumentParser) -> None:
    bundle_commands = parser.add_subparsers(dest="bundle_command", title="bundle commands")
    verify_parser = bundle_commands.add_parser(
        "verify",
        aliases=["verification"],
        help="Verify an evidence bundle without application access",
    )
    verify_parser.add_argument("path", type=Path, help="Path to the evidence bundle ZIP")


def _run_bundle(args: argparse.Namespace, stream: TextIO = sys.stdout) -> int:
    if args.bundle_command is None:
        _write(stream, "bundle requires a command: verify.")
        return 2
    if args.bundle_command not in {"verify", "verification"}:
        raise AssertionError(f"unhandled bundle command: {args.bundle_command}")
    result = verify_bundle(args.path)
    if not result.valid:
        _write(stream, f"FAIL: {result.message}")
        return 1
    _write(stream, f"PASS: {result.message}")
    if result.chain_verified is False:
        _write(stream, f"WARNING: {result.chain_failure or 'audit chain verification failed'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the baseline parser and its future command groups."""
    parser = argparse.ArgumentParser(prog="radarctl", description="Covenant Radar operations")
    subcommands = parser.add_subparsers(dest="command", title="command groups")
    for command, task in COMMAND_TASKS.items():
        command_parser = subcommands.add_parser(command, help=f"Implemented by {task}")
        if command == "serve":
            command_parser.add_argument("--host", default=None, help="Listener host override")
            command_parser.add_argument(
                "--port", type=int, default=None, help="Listener port override"
            )
            command_parser.add_argument(
                "--workers", type=int, default=None, help="Worker process count override"
            )
        if command == "gate":
            command_parser.add_argument(
                "--fast",
                nargs="*",
                metavar="STEP",
                help="Run the first six gate steps, or only the named fast steps.",
            )
        if command == "migrate":
            _build_migrate_parser(command_parser)
        if command == "seed":
            _build_seed_parser(command_parser)
        if command == "cassette":
            _build_cassette_parser(command_parser)
        if command == "user":
            _build_user_parser(command_parser)
        if command == "job":
            _build_job_parser(command_parser)
        if command == "bundle":
            _build_bundle_parser(command_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command group or display the available groups."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "serve":
        return run_serve(host=args.host, port=args.port, workers=args.workers)

    if args.command == "gate":
        return run_gate(fast=args.fast is not None, selected_steps=args.fast or ())

    if args.command == "migrate":
        return _run_migrate(args)

    if args.command == "seed":
        return run_seed(
            database_url=args.database_url,
            reference_portfolio=args.reference_portfolio,
            demo_covenants=args.demo_covenants,
            reset=args.reset,
            i_understand=args.i_understand,
            check_deterministic=args.check_deterministic,
            signal_days=args.signal_days,
        )

    if args.command == "user":
        return _run_user(args)

    if args.command == "job":
        return _run_job(args)

    if args.command == "bundle":
        return _run_bundle(args)

    if args.command == "cassette":
        return _run_cassette(args)

    task = COMMAND_TASKS[args.command]
    _write(sys.stderr, f"{args.command} is not yet implemented; implemented by {task}.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
