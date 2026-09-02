"""Application composition for the production nightly pipeline."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from covenant_radar.config.settings import Settings
from covenant_radar.config.thresholds import DEFAULT_THRESHOLD_PATH, ThresholdStore
from covenant_radar.core.clock import SystemClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser, Role, UserRole
from covenant_radar.db.models.statements import FinancialPeriod, StatementLineValue
from covenant_radar.db.repositories.thresholds import SqlAlchemyThresholdRepository
from covenant_radar.db.session import SessionFactory
from covenant_radar.domain.forecast import Weights
from covenant_radar.domain.forecast.predictor import ForecastPredictor
from covenant_radar.ingestion.signals.file_source import FileSignalSource
from covenant_radar.ml.forecast import SklearnForecastPredictor
from covenant_radar.scheduler import default_registry
from covenant_radar.scheduler.jobs import JobRegistry
from covenant_radar.scheduler.pipeline import (
    PIPELINE_JOB_NAME,
    PIPELINE_STEPS,
    default_step_policy,
    pipeline_job,
    register_nightly_pipeline,
)
from covenant_radar.scheduler.runner import JobRunner, Scheduler
from covenant_radar.services.model_governance import SqlAlchemyModelRegistryRepository
from covenant_radar.services.nightly import NightlyPipelineService
from covenant_radar.services.scoring import CHAMPION_PREDICTOR_MODE, SHADOW_PREDICTOR_MODE

_LOGGER = logging.getLogger(__name__)

SYSTEM_ACTOR_USERNAME = "covenant-radar-system"
SYSTEM_ACTOR_EMAIL = "covenant-radar-system@local.invalid"

#: The model-register component name the forecast challenger is approved under.
#: `ai.registry` already gates the model provider this way; the forecast model
#: is the second thing whose promotion must be a signed-off decision.
ML_FORECAST_COMPONENT = "forecast.ml_challenger"


@dataclass(frozen=True, slots=True)
class NightlyRuntime:
    """All long-lived objects required to trigger the nightly pipeline."""

    registry: JobRegistry
    runner: JobRunner
    service: NightlyPipelineService
    threshold_store: ThresholdStore
    system_actor_id: UUID
    scheduler: Scheduler


def build_nightly_runtime(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    registry: JobRegistry | None = None,
) -> NightlyRuntime:
    """Build and register the real six-step pipeline.

    The function is intentionally shared by the CLI and browser application
    roots, so the demo and the served UI cannot drift onto different job
    handlers or forecast settings.
    """

    if not callable(session_factory):
        raise TypeError("build_nightly_runtime requires a callable session factory.")
    if not isinstance(settings, Settings):
        raise TypeError("build_nightly_runtime requires validated Settings.")

    actor_id = ensure_system_actor(session_factory)
    threshold_session = session_factory()
    try:
        threshold_store = ThresholdStore(
            repository=SqlAlchemyThresholdRepository(threshold_session),
            path=DEFAULT_THRESHOLD_PATH,
        )
        threshold_session.commit()
    except Exception:
        threshold_session.rollback()
        raise
    finally:
        threshold_session.close()

    weights = Weights(
        distance=Decimal(str(settings.forecast.distance_weight)),
        velocity=Decimal(str(settings.forecast.velocity_weight)),
        pressure=Decimal(str(settings.forecast.pressure_weight)),
        max_probability=Decimal(str(settings.forecast.max_probability)),
    )
    predictor = _forecast_predictor(settings)
    service = NightlyPipelineService(
        session_factory,
        threshold_store=threshold_store,
        horizons=settings.forecast.horizons,
        weights=weights,
        system_actor_id=actor_id,
        signal_source=_demo_signal_source(settings),
        statement_lines=_statement_lines_provider(session_factory),
        default_assignee_id=default_case_assignee(session_factory),
        predictor=predictor,
        predictor_mode=_predictor_mode(settings, session_factory),
        model_version=(
            predictor.version
            if isinstance(predictor, SklearnForecastPredictor)
            else "nightly.pipeline.v1"
        ),
    )
    active_registry = registry or default_registry()
    runner = JobRunner(active_registry, session_factory)
    scheduler = Scheduler(runner, database_url=settings.database.url)
    _register_if_needed(scheduler, service)
    return NightlyRuntime(active_registry, runner, service, threshold_store, actor_id, scheduler)


#: The role a newly opened case is assigned to when the pipeline raises it.
#: `spec §7` makes the relationship manager the first responder for a borrower
#: entering the act band, and `_run_dispatch` only notifies an assigned case,
#: so leaving this unresolved silently disables every act-band notification.
CASE_ASSIGNEE_ROLE_CODE = "relationship_manager"


def default_case_assignee(session_factory: SessionFactory) -> UUID | None:
    """Resolve the standing assignee for pipeline-opened cases.

    Returns ``None`` when no active relationship manager exists, which leaves
    cases unassigned exactly as before rather than inventing a recipient; the
    dispatch step reports those as ``skipped_unassigned``.
    """

    if not callable(session_factory):
        raise TypeError("default_case_assignee requires a callable session factory.")
    session = session_factory()
    try:
        return session.scalar(
            select(AppUser.id)
            .join(UserRole, UserRole.user_id == AppUser.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                Role.code == CASE_ASSIGNEE_ROLE_CODE,
                AppUser.is_active.is_(True),
            )
            .order_by(AppUser.username, AppUser.id)
            .limit(1)
        )
    finally:
        session.close()


def ensure_system_actor(session_factory: SessionFactory) -> UUID:
    """Return the non-login system actor used for durable job provenance."""

    session = session_factory()
    try:
        existing = session.scalar(
            select(AppUser.id).where(AppUser.username == SYSTEM_ACTOR_USERNAME)
        )
        if existing is not None:
            session.commit()
            return existing
        now = SystemClock().now()
        user = AppUser(
            username=SYSTEM_ACTOR_USERNAME,
            email=SYSTEM_ACTOR_EMAIL,
            full_name="Covenant Radar system",
            password_hash=None,
            auth_source="local",
            external_subject=None,
            is_active=True,
            mfa_secret_enc=None,
            failed_attempts=0,
            locked_until=None,
            password_changed_at=None,
            must_change_password=False,
            locale="en",
            theme="light",
            created_at=now,
            updated_at=now,
            created_by_id=None,
            updated_by_id=None,
            request_id="system-" + new_id().hex[:26],
            version=1,
        )
        session.add(user)
        session.commit()
        return user.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _register_if_needed(scheduler: Scheduler, service: NightlyPipelineService) -> None:
    """Register the six pipeline steps directly (none carries a cron
    schedule of its own) and the composite job through `scheduler.schedule`
    — the one call that also reaches the live APScheduler loop
    `Scheduler.start` drives, so the nightly cron tick actually fires
    rather than being schedulable by name only."""
    registry = scheduler.runner.registry
    present_steps = tuple(step for step in PIPELINE_STEPS if step in registry)
    if present_steps and len(present_steps) != len(PIPELINE_STEPS):
        missing = tuple(step for step in PIPELINE_STEPS if step not in registry)
        raise RuntimeError(f"Nightly registry is partially configured; missing {missing!r}.")
    if not present_steps:
        register_nightly_pipeline(registry, service.handlers(), policy=default_step_policy())
    if PIPELINE_JOB_NAME not in registry:
        scheduler.schedule(
            pipeline_job(
                scheduler.runner,
                schedule="0 1 * * *",
                policy=default_step_policy(),
            )
        )


def _statement_lines_provider(
    session_factory: SessionFactory,
) -> Callable[[CovenantVersion, date], Mapping[str, Decimal] | None]:
    def provider(version: CovenantVersion, as_of_date: date) -> Mapping[str, Decimal] | None:
        session = session_factory()
        try:
            period = session.scalar(
                select(FinancialPeriod)
                .join(Facility, Facility.borrower_id == FinancialPeriod.borrower_id)
                .join(Covenant, Covenant.facility_id == Facility.id)
                .where(
                    Covenant.id == version.covenant_id,
                    FinancialPeriod.is_complete.is_(True),
                    FinancialPeriod.period_end <= as_of_date,
                )
                .order_by(FinancialPeriod.period_end.desc(), FinancialPeriod.id.desc())
                .limit(1)
            )
            if period is None:
                return None
            rows = session.execute(
                select(StatementLineValue.line_code, StatementLineValue.value).where(
                    StatementLineValue.period_id == period.id
                )
            ).all()
            return {line_code: value for line_code, value in rows} or None
        finally:
            session.close()

    return provider


def _demo_signal_source(settings: Settings) -> Callable[[], Iterable[object]] | None:
    """Return the deterministic local signal source when it is present.

    The runtime intentionally does not invent events when the file is absent;
    operators can point ``ingestion.file_drop_path`` at a real connector and
    continue using the same canonical adapter.  The demo seed creates this
    file under the configured drop directory.
    """

    path = settings.ingestion.file_drop_path / "covenant-radar-demo-signals.json"
    if not path.is_file():
        return None
    source = FileSignalSource(
        path=path,
        mapping={
            "borrower_id": "borrower_id",
            "facility_id": "facility_id",
            "event_date": "event_date",
            "family": "family",
            "event_type": "event_type",
            "magnitude": "magnitude",
            "unit": "unit",
            "payload": "payload",
        },
        source_reference="evaluation/phase-7a-demo-signals-v1",
        file_format="json",
    )
    return source.iter_events


def _forecast_predictor(settings: Settings) -> ForecastPredictor | None:
    """Load the local ML challenger; the deterministic code is the fallback.

    Every failure mode here is the same failure mode: the challenger is
    unavailable and the deterministic model carries the run alone.  A missing
    scikit-learn, a pickle written by another library version, a truncated
    artifact — none of them is a reason to refuse to start the application,
    which is what raising out of this composition root used to do.
    """

    path = settings.forecast.ml_artifact_path
    if not settings.forecast.ml_enabled or path is None or not path.is_file():
        return None
    try:
        return SklearnForecastPredictor(path)
    except Exception:
        _LOGGER.exception(
            "The forecast ML artifact at %s could not be loaded; "
            "scoring continues on the deterministic model alone.",
            path,
        )
        return None


def _predictor_mode(settings: Settings, session_factory: SessionFactory) -> str:
    """Resolve whether the challenger may replace the deterministic value.

    Shadow is the default and the only mode an artifact on disk can reach by
    itself.  Champion additionally requires an approved `model_registration`
    row for `ML_FORECAST_COMPONENT` — the same maker-checker register that
    already gates the model provider — so a model can never become the number
    on a credit officer's screen without a distinct approver having signed it
    off.
    """

    if settings.forecast.ml_mode != CHAMPION_PREDICTOR_MODE:
        return SHADOW_PREDICTOR_MODE
    session = session_factory()
    try:
        record = SqlAlchemyModelRegistryRepository(session).get_by_component(
            ML_FORECAST_COMPONENT
        )
    except Exception:
        _LOGGER.exception(
            "The model register could not be read; the ML challenger stays in shadow mode."
        )
        return SHADOW_PREDICTOR_MODE
    finally:
        session.close()
    if record is None or not record.is_approved:
        _LOGGER.warning(
            "forecast.ml_mode is %r but component %r is %s; the ML challenger stays in "
            "shadow mode and the deterministic probability is served.",
            CHAMPION_PREDICTOR_MODE,
            ML_FORECAST_COMPONENT,
            "not on the model register" if record is None else f"in state {record.state!r}",
        )
        return SHADOW_PREDICTOR_MODE
    return CHAMPION_PREDICTOR_MODE


__all__ = [
    "CASE_ASSIGNEE_ROLE_CODE",
    "NightlyRuntime",
    "SYSTEM_ACTOR_EMAIL",
    "SYSTEM_ACTOR_USERNAME",
    "build_nightly_runtime",
    "default_case_assignee",
    "ensure_system_actor",
]
