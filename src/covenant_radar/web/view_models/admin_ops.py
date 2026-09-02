"""Read models and persistence helpers for the administrator operations screen.

The operations screen is intentionally a projection over durable records.  A
job's state comes from ``job_run`` and a purge's state comes from
``retention_purge_log``; neither is inferred from a log file or from an
in-memory scheduler object.  Retention configuration is stored in the
existing versioned ``config_version`` table so a restart cannot silently
restore an older policy.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, scoped_session

from covenant_radar import __version__
from covenant_radar.config.capabilities import Capabilities
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import ValidationError
from covenant_radar.db.models.audit import AuditEvent, ConfigVersion, TraceRow
from covenant_radar.db.models.covenant import CovenantTest, CovenantVersion
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.forecast import Forecast, ForecastPath, ForecastRun
from covenant_radar.db.models.identity import UserSession
from covenant_radar.db.models.operations import EntityMatch, JobRun, ModelCall, RetentionPurgeLog
from covenant_radar.db.models.signal import SignalEvent
from covenant_radar.db.models.statements import FinancialPeriod, QuarantineRow, StatementLineValue
from covenant_radar.db.models.workflow import Disposition, Memo, Notification, OverrideRecord
from covenant_radar.db.session import is_database_session

_MAX_JOB_HISTORY: Final[int] = 200
_MAX_PURGE_HISTORY: Final[int] = 100
_CONFIG_KEY: Final[str] = "retention"
_REGULATORY_MIN_YEARS: Final[int] = 8
_REGULATORY_MAX_YEARS: Final[int] = 100
_LOG_MIN_DAYS: Final[int] = 180
_LOG_MAX_DAYS: Final[int] = 36_500
_RAW_SIGNAL_MIN_MONTHS: Final[int] = 24
_RAW_SIGNAL_MAX_MONTHS: Final[int] = 24
_FORECAST_MIN_MONTHS: Final[int] = 24
_FORECAST_MAX_MONTHS: Final[int] = 24
_NOTIFICATION_MIN_MONTHS: Final[int] = 12
_NOTIFICATION_MAX_MONTHS: Final[int] = 12
_QUARANTINE_MIN_DAYS: Final[int] = 90
_QUARANTINE_MAX_DAYS: Final[int] = 90
_SESSION_GRACE_DAYS: Final[int] = 30
_TERMINAL_MATCH_DECISIONS: Final[tuple[str, ...]] = (
    "accepted",
    "matched",
    "rejected",
    "discarded",
    "negative",
)


class AuditWriter(Protocol):
    """The application audit boundary used for configuration changes."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an event in the caller's transaction."""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Validated retention values that are safe to persist and display.

    The regulatory and log periods are customer-configurable within the
    documented minimums.  The remaining periods mirror the product's frozen
    lifecycle schedule and are carried in the version so a future change is
    explicit rather than silently changing the meaning of old previews.
    """

    regulatory_period_years: int = _REGULATORY_MIN_YEARS
    logs_min_days: int = _LOG_MIN_DAYS
    raw_signal_months: int = 24
    forecast_months: int = 24
    notification_months: int = 12
    quarantine_days: int = 90

    def __post_init__(self) -> None:
        _validate_integer_range(
            self.regulatory_period_years,
            "regulatory_period_years",
            _REGULATORY_MIN_YEARS,
            _REGULATORY_MAX_YEARS,
        )
        _validate_integer_range(self.logs_min_days, "logs_min_days", _LOG_MIN_DAYS, _LOG_MAX_DAYS)
        _validate_integer_range(
            self.raw_signal_months,
            "raw_signal_months",
            _RAW_SIGNAL_MIN_MONTHS,
            _RAW_SIGNAL_MAX_MONTHS,
        )
        _validate_integer_range(
            self.forecast_months,
            "forecast_months",
            _FORECAST_MIN_MONTHS,
            _FORECAST_MAX_MONTHS,
        )
        _validate_integer_range(
            self.notification_months,
            "notification_months",
            _NOTIFICATION_MIN_MONTHS,
            _NOTIFICATION_MAX_MONTHS,
        )
        _validate_integer_range(
            self.quarantine_days,
            "quarantine_days",
            _QUARANTINE_MIN_DAYS,
            _QUARANTINE_MAX_DAYS,
        )

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, object],
        *,
        base: RetentionPolicy | None = None,
    ) -> RetentionPolicy:
        """Build a policy from a partial update, failing on unknown keys."""

        if not isinstance(values, Mapping):
            raise ValidationError("Retention values must be an object.", field="retention")
        if _CONFIG_KEY in values:
            nested = values[_CONFIG_KEY]
            if not isinstance(nested, Mapping):
                raise ValidationError("retention must contain an object.", field="retention")
            values = nested

        current = base or cls()
        canonical: dict[str, int] = {}
        aliases = {
            "regulatory_years": "regulatory_period_years",
            "logs_days": "logs_min_days",
            "notifications_months": "notification_months",
        }
        allowed = frozenset(
            {
                "regulatory_period_years",
                "regulatory_years",
                "logs_min_days",
                "logs_days",
                "raw_signal_months",
                "forecast_months",
                "notification_months",
                "notifications_months",
                "quarantine_days",
            }
        )
        for raw_key, value in values.items():
            if not isinstance(raw_key, str) or raw_key not in allowed:
                raise ValidationError(f"Unknown retention setting {raw_key!r}.", field="retention")
            key = aliases.get(raw_key, raw_key)
            canonical[key] = _parse_integer(value, key)

        result = current.as_dict()
        result.update(canonical)
        try:
            return cls(**result)
        except (TypeError, ValueError) as error:
            raise ValidationError(
                f"Invalid retention configuration: {error}.", field="retention"
            ) from error

    def as_dict(self) -> dict[str, int]:
        """Return the canonical JSON-safe representation."""

        return {
            "regulatory_period_years": self.regulatory_period_years,
            "logs_min_days": self.logs_min_days,
            "raw_signal_months": self.raw_signal_months,
            "forecast_months": self.forecast_months,
            "notification_months": self.notification_months,
            "quarantine_days": self.quarantine_days,
        }


@dataclass(frozen=True, slots=True)
class JobRunView:
    """Safe, display-ready projection of one job attempt."""

    id: UUID
    job_name: str
    run_id: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    state: str
    attempt: int
    error: str | None
    metrics: Mapping[str, object] | None
    duration_seconds: float | None

    @property
    def outcome(self) -> str:
        """Compatibility name for the job-history outcome column."""

        return self.state

    @property
    def can_retry(self) -> bool:
        """Whether this terminal failure can be offered to an administrator."""

        return self.state == "failed"

    @property
    def duration(self) -> float | None:
        """Compatibility spelling for callers that use ``duration``."""

        return self.duration_seconds


@dataclass(frozen=True, slots=True)
class JobDefinitionView:
    """The declared operational policy for one registered job."""

    name: str
    schedule: str | None
    max_attempts: int | None
    timeout_seconds: float | None
    interruption: str | None


@dataclass(frozen=True, slots=True)
class CapabilityView:
    """A capability state and its actionable unconfigured explanation."""

    name: str
    configured: bool
    detail: str
    enables: str

    @property
    def status(self) -> str:
        """Stable display state used by templates and accessibility tests."""

        return "configured" if self.configured else "not configured"


@dataclass(frozen=True, slots=True)
class DependencyView:
    """One internal or external dependency health result."""

    name: str
    healthy: bool
    detail: str

    @property
    def status(self) -> str:
        """Human-readable dependency state."""

        return "healthy" if self.healthy else "unavailable"


@dataclass(frozen=True, slots=True)
class HealthView:
    """Complete health projection for the administrator."""

    version: str
    capabilities: tuple[CapabilityView, ...]
    dependencies: tuple[DependencyView, ...]

    @property
    def healthy(self) -> bool:
        """Whether all probed dependencies are available."""

        return all(item.healthy for item in self.dependencies)


@dataclass(frozen=True, slots=True)
class QueueDepthView:
    """One actionable queue depth."""

    name: str
    count: int
    detail: str


@dataclass(frozen=True, slots=True)
class RetentionCountView:
    """Count of rows eligible under one retention rule at preview time."""

    entity: str
    rule: str
    count: int
    cutoff: date | datetime

    @property
    def purgeable_count(self) -> int:
        """Compatibility spelling for the preview table."""

        return self.count

    @property
    def cutoff_date(self) -> date:
        """Return a date for compact, timezone-independent display."""

        return self.cutoff.date() if isinstance(self.cutoff, datetime) else self.cutoff


@dataclass(frozen=True, slots=True)
class RetentionPreviewView:
    """Non-persistent retention impact preview."""

    policy: RetentionPolicy
    as_of: datetime
    counts: tuple[RetentionCountView, ...]
    token: str

    @property
    def total_count(self) -> int:
        """Total rows eligible across all displayed entities."""

        return sum(row.count for row in self.counts)


@dataclass(frozen=True, slots=True)
class RetentionConfigurationView:
    """Current persisted retention policy and its provenance."""

    policy: RetentionPolicy
    config_id: UUID | None
    applied_at: datetime | None
    applied_by_id: UUID | None


@dataclass(frozen=True, slots=True)
class PurgeLogView:
    """Immutable purge evidence shown without a re-run action."""

    id: UUID
    entity: str
    criteria: Mapping[str, object]
    purged_count: int
    executed_at: datetime
    executed_by: str


@dataclass(frozen=True, slots=True)
class AdminOpsView:
    """Complete view model for ``/admin/jobs``."""

    job_runs: tuple[JobRunView, ...]
    jobs: tuple[JobDefinitionView, ...]
    health: HealthView
    queue_depths: tuple[QueueDepthView, ...]
    retention_policy: RetentionConfigurationView
    retention_preview: RetentionPreviewView | None
    purge_logs: tuple[PurgeLogView, ...]

    @property
    def quarantine_depth(self) -> int:
        """Return the unresolved quarantine queue depth."""

        return _queue_count(self.queue_depths, "quarantine")

    @property
    def entity_match_depth(self) -> int:
        """Return the pending entity-match review depth."""

        return _queue_count(self.queue_depths, "entity matches")


def load_admin_ops_view(
    session: Session | scoped_session[Session],
    *,
    settings: object | None = None,
    capabilities: Capabilities | None = None,
    runtime: object | None = None,
    clock: Clock | None = None,
    retention_preview: RetentionPreviewView | None = None,
) -> AdminOpsView:
    """Read all operational facts without executing or mutating a job."""

    if not is_database_session(session):
        raise TypeError("load_admin_ops_view requires a SQLAlchemy Session.")
    db_session = _concrete_session(session)
    now = _aware((clock or SystemClock()).now(), "clock.now()")
    policy, config = _load_retention_configuration(db_session)
    job_runs = tuple(
        _job_run_view(row, now)
        for row in db_session.scalars(
            select(JobRun)
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(_MAX_JOB_HISTORY)
        ).all()
    )
    jobs = _job_definitions(runtime)
    health = _health_view(db_session, settings, capabilities, runtime)
    queue_depths = (
        QueueDepthView(
            name="quarantine",
            count=_count_rows(
                db_session,
                select(func.count())
                .select_from(QuarantineRow)
                .where(QuarantineRow.resolved_at.is_(None)),
            ),
            detail="Unresolved source rows awaiting steward action.",
        ),
        QueueDepthView(
            name="entity matches",
            count=_count_rows(
                db_session,
                select(func.count())
                .select_from(EntityMatch)
                .where(~EntityMatch.decision.in_(_TERMINAL_MATCH_DECISIONS)),
            ),
            detail="Candidate matches awaiting a human decision.",
        ),
    )
    purge_logs = tuple(
        _purge_log_view(row)
        for row in db_session.scalars(
            select(RetentionPurgeLog)
            .order_by(RetentionPurgeLog.executed_at.desc(), RetentionPurgeLog.id.desc())
            .limit(_MAX_PURGE_HISTORY)
        ).all()
    )
    return AdminOpsView(
        job_runs=job_runs,
        jobs=jobs,
        health=health,
        queue_depths=queue_depths,
        retention_policy=RetentionConfigurationView(
            policy=policy,
            config_id=config.id if config is not None else None,
            applied_at=_utc_or_none(config.applied_at if config is not None else None),
            applied_by_id=config.applied_by_id if config is not None else None,
        ),
        retention_preview=retention_preview,
        purge_logs=purge_logs,
    )


def preview_retention(
    session: Session | scoped_session[Session],
    policy: RetentionPolicy,
    *,
    clock: Clock | None = None,
    as_of: datetime | None = None,
) -> RetentionPreviewView:
    """Calculate a tamper-evident, non-writing purge preview."""

    if not is_database_session(session):
        raise TypeError("preview_retention requires a SQLAlchemy Session.")
    if not isinstance(policy, RetentionPolicy):
        raise TypeError("preview_retention requires a RetentionPolicy.")
    timestamp = _aware(as_of or (clock or SystemClock()).now(), "as_of")
    counts = _retention_counts(_concrete_session(session), policy, timestamp)
    token = _preview_token(policy, counts)
    return RetentionPreviewView(policy=policy, as_of=timestamp, counts=counts, token=token)


def apply_retention_policy(
    session: Session | scoped_session[Session],
    policy: RetentionPolicy,
    *,
    actor_id: UUID,
    audit: AuditWriter,
    clock: Clock | None = None,
    applied_at: datetime | None = None,
    request_id: str | None = None,
) -> ConfigVersion:
    """Persist a validated policy version and emit its audit event."""

    if not is_database_session(session):
        raise TypeError("apply_retention_policy requires a SQLAlchemy Session.")
    if not isinstance(policy, RetentionPolicy):
        raise TypeError("apply_retention_policy requires a RetentionPolicy.")
    if not isinstance(actor_id, UUID):
        raise TypeError("apply_retention_policy requires an actor UUID.")
    if not callable(getattr(audit, "record", None)):
        raise TypeError("apply_retention_policy requires an audit writer.")
    timestamp = _aware(applied_at or (clock or SystemClock()).now(), "applied_at")
    event_request_id = request_id or get_request_id() or new_request_id()
    if not isinstance(event_request_id, str) or not 1 <= len(event_request_id) <= 40:
        raise ValueError("request_id must be between 1 and 40 characters.")
    db_session = _concrete_session(session)
    previous, previous_config = _load_retention_configuration(db_session)
    row = ConfigVersion(
        values_redacted={_CONFIG_KEY: policy.as_dict()},
        applied_at=timestamp,
        applied_by_id=actor_id,
        checksum=_configuration_checksum(policy),
        version=previous_config.version + 1 if previous_config is not None else 1,
        created_at=timestamp,
        updated_at=timestamp,
        created_by_id=actor_id,
        updated_by_id=actor_id,
        request_id=event_request_id,
    )
    db_session.add(row)
    db_session.flush()
    audit.record(
        "retention_policy_changed",
        ("config_version", row.id),
        {
            "before": previous.as_dict(),
            "after": policy.as_dict(),
            "config_version_id": str(row.id),
        },
        actor=actor_id,
        request_id=event_request_id,
    )
    return row


def current_retention_policy(
    session: Session | scoped_session[Session],
) -> RetentionPolicy:
    """Return the active policy, including the documented default."""

    if not is_database_session(session):
        raise TypeError("current_retention_policy requires a SQLAlchemy Session.")
    return _load_retention_configuration(_concrete_session(session))[0]


def _health_view(
    session: Session,
    settings: object | None,
    capabilities: Capabilities | None,
    runtime: object | None,
) -> HealthView:
    resolved_capabilities = capabilities or _capabilities_from_settings(settings)
    capability_rows = tuple(
        _capability_view(resolved_capabilities, name, enables)
        for name, enables in (
            ("model_provider", "grounded language-model stages and memo drafting"),
            ("sso", "single sign-on identity mapping"),
            ("ocr", "scanned-document text extraction"),
            ("smtp", "email digests and job-failure delivery"),
            ("webhooks", "signed webhook delivery"),
            ("document_store", "encrypted document persistence"),
        )
    )
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        database = DependencyView(
            name="Database",
            healthy=False,
            detail="The database health check failed; operational writes are unavailable.",
        )
    else:
        database = DependencyView(
            name="Database",
            healthy=True,
            detail="Database connection is responding.",
        )
    registry = getattr(runtime, "registry", None) if runtime is not None else None
    runner = getattr(runtime, "runner", None) if runtime is not None else None
    scheduler_ready = callable(getattr(registry, "all", None)) and callable(
        getattr(runner, "submit", None)
    )
    scheduler = DependencyView(
        name="Scheduler",
        healthy=scheduler_ready,
        detail=(
            "Job registry and asynchronous runner are available."
            if scheduler_ready
            else "The scheduler is not configured in this process."
        ),
    )
    return HealthView(
        version=__version__,
        capabilities=capability_rows,
        dependencies=(database, scheduler),
    )


def _capabilities_from_settings(settings: object | None) -> Capabilities | None:
    if settings is None:
        return None
    value = getattr(settings, "capabilities", None)
    if isinstance(value, Capabilities):
        return value
    return None


def _capability_view(
    capabilities: Capabilities | None,
    name: str,
    enables: str,
) -> CapabilityView:
    capability = getattr(capabilities, name, None) if capabilities is not None else None
    configured = bool(getattr(capability, "configured", False))
    detail = getattr(capability, "detail", "not configured")
    return CapabilityView(
        name=name.replace("_", " ").title(),
        configured=configured,
        detail=str(detail),
        enables=enables,
    )


def _job_definitions(runtime: object | None) -> tuple[JobDefinitionView, ...]:
    registry = getattr(runtime, "registry", None) if runtime is not None else None
    definitions = getattr(registry, "all", lambda: ())()
    result: list[JobDefinitionView] = []
    for definition in definitions:
        policy = getattr(definition, "policy", None)
        retry = getattr(policy, "retry", None)
        interruption = getattr(policy, "interruption", None)
        result.append(
            JobDefinitionView(
                name=str(getattr(definition, "name", "")),
                schedule=getattr(definition, "schedule", None),
                max_attempts=getattr(retry, "max_attempts", None),
                timeout_seconds=getattr(policy, "timeout_seconds", None),
                interruption=str(interruption) if interruption is not None else None,
            )
        )
    return tuple(result)


def _job_run_view(row: JobRun, now: datetime) -> JobRunView:
    started_at = _aware(row.started_at, "job_run.started_at")
    finished_at = _utc_or_none(row.finished_at)
    end = finished_at or now
    duration = max((end - started_at).total_seconds(), 0.0)
    return JobRunView(
        id=row.id,
        job_name=row.job_name,
        run_id=row.run_id,
        trigger=row.trigger,
        started_at=started_at,
        finished_at=finished_at,
        state=row.state,
        attempt=row.attempt,
        error=row.error,
        metrics=dict(row.metrics) if isinstance(row.metrics, Mapping) else None,
        duration_seconds=duration,
    )


def _purge_log_view(row: RetentionPurgeLog) -> PurgeLogView:
    criteria = row.criteria if isinstance(row.criteria, Mapping) else {}
    return PurgeLogView(
        id=row.id,
        entity=row.entity,
        criteria=dict(criteria),
        purged_count=row.purged_count,
        executed_at=_aware(row.executed_at, "retention_purge_log.executed_at"),
        executed_by=row.executed_by,
    )


def _load_retention_configuration(session: Session) -> tuple[RetentionPolicy, ConfigVersion | None]:
    rows = session.scalars(
        select(ConfigVersion)
        .order_by(ConfigVersion.applied_at.desc(), ConfigVersion.id.desc())
        .limit(200)
    ).all()
    for row in rows:
        values = row.values_redacted
        if not isinstance(values, Mapping) or _CONFIG_KEY not in values:
            continue
        try:
            policy = RetentionPolicy.from_values(values[_CONFIG_KEY])  # type: ignore[arg-type]
        except (TypeError, ValueError, ValidationError) as error:
            raise ValidationError(
                f"Stored retention configuration {row.id} is invalid: {error}.",
                field="retention",
            ) from error
        return policy, row
    return RetentionPolicy(), None


def _retention_counts(
    session: Session,
    policy: RetentionPolicy,
    as_of: datetime,
) -> tuple[RetentionCountView, ...]:
    regulatory_cutoff = _subtract_years(as_of, policy.regulatory_period_years)
    log_cutoff = as_of - timedelta(days=policy.logs_min_days)
    signal_cutoff = _subtract_months(as_of, policy.raw_signal_months)
    forecast_cutoff = _subtract_months(as_of, policy.forecast_months)
    notification_cutoff = _subtract_months(as_of, policy.notification_months)
    session_cutoff = as_of - timedelta(days=_SESSION_GRACE_DAYS)
    quarantine_cutoff = as_of - timedelta(days=policy.quarantine_days)
    counts: list[RetentionCountView] = []

    def add(entity: str, rule: str, count: int, cutoff: date | datetime) -> None:
        counts.append(RetentionCountView(entity, rule, count, cutoff))

    for model, column, entity in (
        (AuditEvent, AuditEvent.occurred_at, "audit events"),
        (TraceRow, TraceRow.occurred_at, "trace rows"),
        (CovenantVersion, CovenantVersion.created_at, "covenant versions"),
        (CovenantTest, CovenantTest.computed_at, "covenant tests"),
        (FinancialPeriod, FinancialPeriod.period_end, "financial periods"),
        (Memo, Memo.created_at, "memos"),
        (OverrideRecord, OverrideRecord.created_at, "overrides"),
        (Disposition, Disposition.created_at, "dispositions"),
    ):
        add(
            entity,
            f"regulatory period ({policy.regulatory_period_years} years)",
            _count_before(session, model, column, regulatory_cutoff),
            regulatory_cutoff,
        )

    add(
        "statement lines",
        f"regulatory period ({policy.regulatory_period_years} years)",
        _count_rows(
            session,
            select(func.count())
            .select_from(StatementLineValue)
            .join(FinancialPeriod, FinancialPeriod.id == StatementLineValue.period_id)
            .where(FinancialPeriod.period_end < regulatory_cutoff.date()),
        ),
        regulatory_cutoff,
    )

    add(
        "documents",
        "document purge date",
        _count_rows(
            session,
            select(func.count())
            .select_from(Document)
            .where(Document.purge_after.is_not(None), Document.purge_after < as_of.date()),
        ),
        as_of.date(),
    )
    add(
        "raw signal events",
        f"raw signal window ({policy.raw_signal_months} months)",
        _count_before(session, SignalEvent, SignalEvent.event_date, signal_cutoff.date()),
        signal_cutoff,
    )
    forecast_models: tuple[tuple[type[Any], str], ...] = (
        (Forecast, "forecasts"),
        (ForecastPath, "forecast paths"),
    )
    for model, entity in forecast_models:
        add(
            entity,
            f"forecast window ({policy.forecast_months} months)",
            _count_rows(
                session,
                select(func.count())
                .select_from(model)
                .join(ForecastRun, ForecastRun.id == model.run_id)
                .where(
                    ForecastRun.finished_at.is_not(None), ForecastRun.finished_at < forecast_cutoff
                ),
            ),
            forecast_cutoff,
        )
    add(
        "notifications",
        f"notification window ({policy.notification_months} months)",
        _count_before(session, Notification, Notification.created_at, notification_cutoff),
        notification_cutoff,
    )
    add(
        "sessions",
        "expiry plus 30 days",
        _count_rows(
            session,
            select(func.count())
            .select_from(UserSession)
            .where(UserSession.expires_at < session_cutoff),
        ),
        session_cutoff,
    )
    add(
        "quarantine rows",
        f"resolved quarantine window ({policy.quarantine_days} days)",
        _count_rows(
            session,
            select(func.count())
            .select_from(QuarantineRow)
            .where(
                QuarantineRow.resolved_at.is_not(None),
                QuarantineRow.resolved_at < quarantine_cutoff,
            ),
        ),
        quarantine_cutoff,
    )
    add(
        "model-call logs",
        f"log window ({policy.logs_min_days} days)",
        _count_before(session, ModelCall, ModelCall.created_at, log_cutoff),
        log_cutoff,
    )
    add(
        "job runs",
        f"job-history window ({policy.logs_min_days} days)",
        _count_before(session, JobRun, JobRun.started_at, log_cutoff),
        log_cutoff,
    )
    return tuple(counts)


def _count_before(session: Session, model: type[Any], column: Any, cutoff: Any) -> int:
    return _count_rows(
        session,
        select(func.count()).select_from(model).where(column < cutoff),
    )


def _count_rows(session: Session, statement: Select[Any]) -> int:
    value = session.scalar(statement)
    return int(value or 0)


def _preview_token(policy: RetentionPolicy, counts: tuple[RetentionCountView, ...]) -> str:
    payload = {
        "policy": policy.as_dict(),
        "counts": [{"entity": row.entity, "rule": row.rule, "count": row.count} for row in counts],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _configuration_checksum(policy: RetentionPolicy) -> str:
    canonical = json.dumps(
        policy.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _subtract_months(value: datetime, months: int) -> datetime:
    total = value.year * 12 + value.month - 1 - months
    year, month_zero_based = divmod(total, 12)
    month = month_zero_based + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _subtract_years(value: datetime, years: int) -> datetime:
    year = value.year - years
    day = min(value.day, calendar.monthrange(year, value.month)[1])
    return value.replace(year=year, day=day)


def _validate_integer_range(value: object, field: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} through {maximum}.")


def _parse_integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer.", field=f"retention.{field}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as error:
            raise ValidationError(
                f"{field} must be an integer.", field=f"retention.{field}"
            ) from error
    raise ValidationError(f"{field} must be an integer.", field=f"retention.{field}")


def _concrete_session(session: Session | scoped_session[Session]) -> Session:
    return session if isinstance(session, Session) else session()


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


def _utc_or_none(value: datetime | None) -> datetime | None:
    return _aware(value, "timestamp") if value is not None else None


def _queue_count(rows: tuple[QueueDepthView, ...], name: str) -> int:
    return next((row.count for row in rows if row.name == name), 0)


__all__ = [
    "AdminOpsView",
    "AuditWriter",
    "CapabilityView",
    "DependencyView",
    "HealthView",
    "JobDefinitionView",
    "JobRunView",
    "PurgeLogView",
    "QueueDepthView",
    "RetentionConfigurationView",
    "RetentionCountView",
    "RetentionPolicy",
    "RetentionPreviewView",
    "apply_retention_policy",
    "current_retention_policy",
    "load_admin_ops_view",
    "preview_retention",
]
