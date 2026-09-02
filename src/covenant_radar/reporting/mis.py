"""Board MIS assembly, rendering, scheduled generation and delivery
(`T-134`, `spec §R-31`).

`spec §R-31`'s Board MIS is the portfolio view a credit committee reads
monthly: distribution by band and SMA, migration between periods, the
early-warning lead time actually achieved (`spec §6`'s G1), escalation and
disposition statistics (G3), model performance from the evaluation record,
and connector/data-quality summaries — generated for a stated period from
stored records and delivered to a distribution list on a schedule.

Like `reporting/rfa_pack.py` (`T-133`) and unlike `reporting/crilc.py`
(`T-132`), this module owns both halves itself: `T-134`'s file ownership is
`reporting/mis.py`, `web/templates/exports/mis.html`, the report job wired
into `scheduler/jobs.py`, and this module's own test file, with no
companion service file. The value objects below stay persistence-neutral;
`MisReportService` is the one place that turns a scoped principal's request
into the stored facts those dataclasses need.

**Absence is a value, not an omission.** Every metric on the report is a
`MisMetric`: either a chart carrying its own figures, or an explicit,
worded reason it could not be produced this period. A metric that computed
to a legitimate zero (no warnings raised, no borrowers in the amber band)
is *present* with a zero-valued point — only an empty *population* (no
borrowers in scope, no connectors configured, no prior period to compare
against) is *absent*. Collapsing those two cases would let an empty chart
silently read as "nothing happened" when the true story is "we could not
say" — `spec §R-31`'s own words for CRILC apply here just as much.

**Delivery is retried, then surfaced.** `MisReportDeliveryService` attempts
each distribution-list recipient up to a bounded number of times with
backoff; a recipient still failing on exhaustion is dead-lettered, audited
under `AuditEventType.MIS_REPORT_DELIVERY_FAILED`, and — because the job
handler re-raises once any recipient dead-letters — the job run itself
lands `failed` in `job_run`, which is what the administrator operations
screen (`web/view_models/admin_ops.py`) already surfaces. Nothing is
silently missed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, ExternalServiceError, ValidationError
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.forecast import Forecast
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.operations import Connector, ConnectorRun, EvaluationRun
from covenant_radar.db.models.workflow import Disposition
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.repositories.facility import FacilityRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import SessionFactory, is_database_session
from covenant_radar.domain.covenants.sma import (
    BorrowerSmaDerivation,
    SmaBand,
    derive_borrower_sma,
)
from covenant_radar.ports.notifier import DeliveryResult, DeliveryStatus, Notifier, OutboundMessage
from covenant_radar.scheduler.jobs import JobHandler, JobRunContext
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize

_TEMPLATE_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "web" / "templates"
_TEMPLATE_NAME: Final[str] = "exports/mis.html"
_MAX_RENDERED_HTML_BYTES: Final[int] = 4 * 1024 * 1024

_MIS_REPORT_GENERATED_EVENT: Final[str] = AuditEventType.MIS_REPORT_GENERATED.value
_MIS_REPORT_DELIVERY_FAILED_EVENT: Final[str] = AuditEventType.MIS_REPORT_DELIVERY_FAILED.value
_MIS_SUBJECT_TYPE: Final[str] = "board_mis_report"

#: `spec §6`'s G1: "≥70% flagged ≥30 days before; ≥50% flagged ≥60 days before".
G1_EARLY_LEAD_DAYS: Final[int] = 30
G1_STRONG_LEAD_DAYS: Final[int] = 60

#: `CovenantVerdict` values `spec §R-31`'s "amber-or-worse" covers — a
#: covenant that is not a clean `pass` and not merely `stale`/uncomputable.
_AMBER_OR_WORSE_VERDICTS: Final[frozenset[str]] = frozenset(
    {"warning", "breach", "breach_cure_open"}
)
_DISPOSITION_ACTED_OUTCOME: Final[str] = "acted"
_HUNDRED: Final[Decimal] = Decimal("100")

_LEAD_TIME_BUCKETS: Final[tuple[tuple[str, int, int | None], ...]] = (
    ("Under 30 days", 0, 29),
    ("30-59 days", 30, 59),
    ("60-89 days", 60, 89),
    ("90 days or more", 90, None),
)


class ReportAuditWriter(Protocol):
    """The append-only `C-60` boundary used by report generation."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's transaction."""
        ...


# --------------------------------------------------------------------------
# Persistence-neutral value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MisPeriod:
    """A closed calendar interval `[start, end]` one report is generated for."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if isinstance(self.start, datetime) or not isinstance(self.start, date):
            raise TypeError("MisPeriod.start must be a calendar date.")
        if isinstance(self.end, datetime) or not isinstance(self.end, date):
            raise TypeError("MisPeriod.end must be a calendar date.")
        if self.end < self.start:
            raise ValueError("MisPeriod.end must not be before start.")

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()}"

    @property
    def start_datetime(self) -> datetime:
        return datetime.combine(self.start, time.min, tzinfo=UTC)

    @property
    def end_datetime_exclusive(self) -> datetime:
        """The half-open upper bound: the instant after `end`'s last moment."""
        return datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class MisChartPoint:
    """One labelled figure — the number a chart's bar *is*, not a caption
    added beside it. This is how every `MisMetric` satisfies `spec §R-31`'s
    "every chart accompanied by its figures": the figure and the chart
    point are the same value, so there is no way to render one without
    the other."""

    label: str
    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip() or len(self.label) > 100:
            raise ValueError("MisChartPoint.label must be non-empty text of at most 100 chars.")
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise TypeError("MisChartPoint.value must be a finite Decimal.")
        object.__setattr__(self, "label", self.label.strip())


@dataclass(frozen=True, slots=True)
class MisMetric:
    """One board-visible figure: present with its chart points, or
    explicitly absent with a stated reason (`spec §R-31`'s "every case":
    a period with no data states so rather than rendering an empty chart;
    an uncomputable metric is named with the reason)."""

    name: str
    unit: str | None
    points: tuple[MisChartPoint, ...]
    absent_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("MisMetric.name must be non-empty text.")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "points", tuple(self.points))
        if not all(isinstance(item, MisChartPoint) for item in self.points):
            raise TypeError("MisMetric.points must contain MisChartPoint values.")
        present = bool(self.points)
        if present and self.absent_reason is not None:
            raise ValueError("A present MisMetric cannot also carry an absent_reason.")
        if not present and self.absent_reason is None:
            raise ValueError("An absent MisMetric requires a stated reason.")
        if not present and (
            not isinstance(self.absent_reason, str) or not self.absent_reason.strip()
        ):
            raise ValueError("MisMetric.absent_reason must be non-empty text.")
        if present and (self.unit is None or not self.unit.strip()):
            raise ValueError("A present MisMetric requires a unit.")
        if not present and self.unit is not None:
            raise ValueError("An absent MisMetric cannot carry a unit.")
        if self.unit is not None:
            object.__setattr__(self, "unit", self.unit.strip())
        if self.absent_reason is not None:
            object.__setattr__(self, "absent_reason", self.absent_reason.strip())

    @property
    def is_present(self) -> bool:
        return bool(self.points)

    @classmethod
    def present(cls, name: str, *, unit: str, points: Sequence[MisChartPoint]) -> MisMetric:
        return cls(name=name, unit=unit, points=tuple(points), absent_reason=None)

    @classmethod
    def absent(cls, name: str, *, reason: str) -> MisMetric:
        return cls(name=name, unit=None, points=(), absent_reason=reason)

    @classmethod
    def single(cls, name: str, *, unit: str, value: Decimal) -> MisMetric:
        return cls.present(name, unit=unit, points=(MisChartPoint(label=name, value=value),))

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "unit": self.unit,
            "points": [
                {"label": item.label, "value": format(item.value, "f")} for item in self.points
            ],
            "absent_reason": self.absent_reason,
        }


@dataclass(frozen=True, slots=True)
class MisDistributionSection:
    """Portfolio distribution by covenant band and by SMA band."""

    as_of_date: date
    covenant_band: MisMetric
    sma_band: MisMetric

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "covenant_band": self.covenant_band.as_dict(),
            "sma_band": self.sma_band.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MisMigrationSection:
    """Band and SMA migration between the prior period and this one."""

    previous_as_of: date | None
    current_as_of: date
    covenant_band_migration: MisMetric
    sma_migration: MisMetric

    def as_dict(self) -> dict[str, object]:
        return {
            "previous_as_of": self.previous_as_of.isoformat() if self.previous_as_of else None,
            "current_as_of": self.current_as_of.isoformat(),
            "covenant_band_migration": self.covenant_band_migration.as_dict(),
            "sma_migration": self.sma_migration.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MisLeadTimeSection:
    """G1: early-warning lead time actually achieved this period."""

    lead_time_distribution: MisMetric
    at_least_30_days_pct: MisMetric
    at_least_60_days_pct: MisMetric

    def as_dict(self) -> dict[str, object]:
        return {
            "lead_time_distribution": self.lead_time_distribution.as_dict(),
            "at_least_30_days_pct": self.at_least_30_days_pct.as_dict(),
            "at_least_60_days_pct": self.at_least_60_days_pct.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MisEscalationSection:
    """G3: escalation volume and disposition statistics this period."""

    warnings_raised: MisMetric
    amber_or_worse_pct: MisMetric
    disposition_outcomes: MisMetric
    acted_on_pct: MisMetric

    def as_dict(self) -> dict[str, object]:
        return {
            "warnings_raised": self.warnings_raised.as_dict(),
            "amber_or_worse_pct": self.amber_or_worse_pct.as_dict(),
            "disposition_outcomes": self.disposition_outcomes.as_dict(),
            "acted_on_pct": self.acted_on_pct.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MisModelPerformanceSection:
    """Model performance drawn from the evaluation record."""

    commit_sha: str | None
    arm: str | None
    passed: bool | None
    executed_at: datetime | None
    scores: MisMetric

    def as_dict(self) -> dict[str, object]:
        return {
            "commit_sha": self.commit_sha,
            "arm": self.arm,
            "passed": self.passed,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "scores": self.scores.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MisConnectorEntry:
    """One connector's most recent reconciliation, for the detail table."""

    name: str
    connector_type: str
    is_active: bool
    latest_run_state: str | None
    latest_run_started_at: datetime | None
    record_count: int | None
    reject_count: int | None
    lag_seconds: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "connector_type": self.connector_type,
            "is_active": self.is_active,
            "latest_run_state": self.latest_run_state,
            "latest_run_started_at": (
                self.latest_run_started_at.isoformat() if self.latest_run_started_at else None
            ),
            "record_count": self.record_count,
            "reject_count": self.reject_count,
            "lag_seconds": self.lag_seconds,
        }


@dataclass(frozen=True, slots=True)
class MisConnectorSection:
    """Connector and data-quality summaries: reconciliation totals, lag,
    rejects (`ConnectorRun`'s own fields *are* the data-quality proxy —
    there is no separate data-quality table in this schema)."""

    entries: tuple[MisConnectorEntry, ...]
    record_counts: MisMetric
    reject_counts: MisMetric
    lag_seconds: MisMetric

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if not all(isinstance(item, MisConnectorEntry) for item in self.entries):
            raise TypeError("MisConnectorSection.entries must contain MisConnectorEntry values.")

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": [item.as_dict() for item in self.entries],
            "record_counts": self.record_counts.as_dict(),
            "reject_counts": self.reject_counts.as_dict(),
            "lag_seconds": self.lag_seconds.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MisReport:
    """The complete, deterministic result of one Board MIS generation."""

    period: MisPeriod
    distribution: MisDistributionSection
    migration: MisMigrationSection
    lead_time: MisLeadTimeSection
    escalations: MisEscalationSection
    model_performance: MisModelPerformanceSection
    connectors: MisConnectorSection

    def as_dict(self) -> dict[str, object]:
        return {
            "period": {"start": self.period.start.isoformat(), "end": self.period.end.isoformat()},
            "distribution": self.distribution.as_dict(),
            "migration": self.migration.as_dict(),
            "lead_time": self.lead_time.as_dict(),
            "escalations": self.escalations.as_dict(),
            "model_performance": self.model_performance.as_dict(),
            "connectors": self.connectors.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        """Deterministic content bytes, with no wall-clock artefact, for
        hashing. `spec §R-31`'s "a report regenerated for a past date
        reproduces the original" made checkable: identical stored facts for
        the same period always reproduce identical bytes."""
        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class MisReportExportResult:
    """One rendered, audited generation of a `MisReport`."""

    report: MisReport
    html: str
    content_hash: str
    generated_at: datetime
    audit_event: object


@dataclass(frozen=True, slots=True)
class MisDeliveryOutcome:
    """The result of attempting delivery to one distribution-list recipient."""

    recipient_id: UUID
    delivered: bool
    attempts: int
    reason: str | None = None


# --------------------------------------------------------------------------
# Generation service
# --------------------------------------------------------------------------


class MisReportService:
    """Assemble, render and audit one Board MIS generation for a period.

    The service never commits — one call runs inside the caller's existing
    transaction, exactly like every other service in this application.
    """

    def __init__(
        self,
        session: Session,
        *,
        audit: ReportAuditWriter,
        clock: Clock | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        request_id: str | None = None,
        template_directory: Path | str = _TEMPLATE_ROOT,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("MisReportService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("MisReportService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("MisReportService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("MisReportService scope_resolver must be callable.")
        directory = Path(template_directory).expanduser().resolve()
        template_path = directory / _TEMPLATE_NAME
        if not template_path.is_file():
            raise FileNotFoundError(f"Board MIS export template does not exist: {template_path}")

        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )
        self.borrowers = BorrowerRepository(session)
        self.facilities = FacilityRepository(session)
        self.environment = Environment(
            loader=FileSystemLoader(str(directory)),
            autoescape=select_autoescape(("html", "xml")),
            undefined=StrictUndefined,
        )

    # ---- public API -----------------------------------------------------

    def generate(
        self,
        principal: Principal,
        *,
        period: MisPeriod,
        previous_period: MisPeriod | None = None,
        scope: Scope | None = None,
    ) -> MisReport:
        """Assemble one deterministic Board MIS for `period` from stored facts."""

        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.EXPORT_EVIDENCE)
        if not isinstance(period, MisPeriod):
            raise TypeError("MisReportService.generate requires a MisPeriod.")
        if previous_period is not None and not isinstance(previous_period, MisPeriod):
            raise TypeError(
                "MisReportService.generate previous_period must be a MisPeriod or None."
            )
        resolved_scope = self._validated_scope(principal, scope)

        distribution, current_covenant, current_sma = self._distribution_section(
            resolved_scope, period.end
        )
        migration = self._migration_section(
            resolved_scope, previous_period, period.end, current_covenant, current_sma
        )
        lead_time = self._lead_time_section(resolved_scope, period)
        escalations = self._escalation_section(resolved_scope, period, current_covenant)
        model_performance = self._model_performance_section(period)
        connectors = self._connector_section(period)

        return MisReport(
            period=period,
            distribution=distribution,
            migration=migration,
            lead_time=lead_time,
            escalations=escalations,
            model_performance=model_performance,
            connectors=connectors,
        )

    def export(
        self,
        principal: Principal,
        *,
        period: MisPeriod,
        previous_period: MisPeriod | None = None,
        scope: Scope | None = None,
        generated_at: datetime | None = None,
        request_id: str | None = None,
    ) -> MisReportExportResult:
        """Generate, render and audit one Board MIS export."""

        report = self.generate(
            principal, period=period, previous_period=previous_period, scope=scope
        )
        html = self._render_html(report)
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
        instant = self._now() if generated_at is None else _aware(generated_at, "generated_at")
        effective_request_id = request_id or self.request_id

        event = self.audit.record(
            _MIS_REPORT_GENERATED_EVENT,
            (_MIS_SUBJECT_TYPE, _period_subject_key(period)),
            {
                "period_start": period.start.isoformat(),
                "period_end": period.end.isoformat(),
                "generated_by": str(principal.id),
                "content_hash": report.content_hash(),
                "rendered_content_hash": content_hash,
            },
            actor=principal.id,
            request_id=effective_request_id,
        )
        return MisReportExportResult(
            report=report,
            html=html,
            content_hash=content_hash,
            generated_at=instant,
            audit_event=event,
        )

    # ---- section assembly -------------------------------------------------

    def _distribution_section(
        self, scope: Scope, as_of_date: date
    ) -> tuple[MisDistributionSection, dict[UUID, tuple[str, UUID]], dict[UUID, SmaBand]]:
        covenant_verdicts = self._latest_covenant_verdicts(scope, as_of_date)
        sma_bands = self._borrower_sma_bands(scope, as_of_date)

        if not covenant_verdicts:
            covenant_metric = MisMetric.absent(
                "Covenant band distribution",
                reason=f"No covenant tests are recorded on or before {as_of_date.isoformat()}.",
            )
        else:
            counts = _count_by(value for value, _borrower_id in covenant_verdicts.values())
            covenant_metric = MisMetric.present(
                "Covenant band distribution",
                unit="covenants",
                points=_points_from_counts(counts),
            )

        if not sma_bands:
            sma_metric = MisMetric.absent(
                "SMA band distribution",
                reason=f"No borrowers are in scope as of {as_of_date.isoformat()}.",
            )
        else:
            sma_counts = _count_by(band.value for band in sma_bands.values())
            sma_metric = MisMetric.present(
                "SMA band distribution", unit="borrowers", points=_points_from_counts(sma_counts)
            )

        section = MisDistributionSection(
            as_of_date=as_of_date, covenant_band=covenant_metric, sma_band=sma_metric
        )
        return section, covenant_verdicts, sma_bands

    def _migration_section(
        self,
        scope: Scope,
        previous_period: MisPeriod | None,
        current_as_of: date,
        current_covenant: Mapping[UUID, tuple[str, UUID]],
        current_sma: Mapping[UUID, SmaBand],
    ) -> MisMigrationSection:
        if previous_period is None:
            no_prior = "No prior period was supplied for a migration comparison."
            return MisMigrationSection(
                previous_as_of=None,
                current_as_of=current_as_of,
                covenant_band_migration=MisMetric.absent(
                    "Covenant band migration", reason=no_prior
                ),
                sma_migration=MisMetric.absent("SMA band migration", reason=no_prior),
            )

        previous_covenant = self._latest_covenant_verdicts(scope, previous_period.end)
        previous_sma = self._borrower_sma_bands(scope, previous_period.end)

        covenant_transitions: dict[str, int] = {}
        for covenant_id, (from_verdict, _borrower) in previous_covenant.items():
            current = current_covenant.get(covenant_id)
            if current is None:
                continue
            key = f"{from_verdict} → {current[0]}"
            covenant_transitions[key] = covenant_transitions.get(key, 0) + 1
        if not covenant_transitions:
            covenant_metric = MisMetric.absent(
                "Covenant band migration",
                reason=(
                    "No covenant has a recorded test in both "
                    f"{previous_period.end.isoformat()} and {current_as_of.isoformat()}."
                ),
            )
        else:
            covenant_metric = MisMetric.present(
                "Covenant band migration",
                unit="covenants",
                points=_points_from_counts(covenant_transitions),
            )

        sma_transitions: dict[str, int] = {}
        for borrower_id, from_band in previous_sma.items():
            to_band = current_sma.get(borrower_id)
            if to_band is None:
                continue
            key = f"{from_band.value} → {to_band.value}"
            sma_transitions[key] = sma_transitions.get(key, 0) + 1
        if not sma_transitions:
            sma_metric = MisMetric.absent(
                "SMA band migration",
                reason=(
                    "No borrower is in scope in both "
                    f"{previous_period.end.isoformat()} and {current_as_of.isoformat()}."
                ),
            )
        else:
            sma_metric = MisMetric.present(
                "SMA band migration", unit="borrowers", points=_points_from_counts(sma_transitions)
            )

        return MisMigrationSection(
            previous_as_of=previous_period.end,
            current_as_of=current_as_of,
            covenant_band_migration=covenant_metric,
            sma_migration=sma_metric,
        )

    def _lead_time_section(self, scope: Scope, period: MisPeriod) -> MisLeadTimeSection:
        ownership = ownership_path_for(Forecast)
        statement = ownership.apply(select(Forecast)).where(
            scope.predicate(ownership.path_column),
            Forecast.below_confidence_floor.is_(False),
            Forecast.created_at >= period.start_datetime,
            Forecast.created_at < period.end_datetime_exclusive,
        )
        warnings = tuple(self.session.execute(statement).scalars().all())

        lead_times: list[int] = []
        for warning in warnings:
            breach_date = self._earliest_subsequent_breach(
                scope, warning.covenant_version_id, warning.created_at.date()
            )
            if breach_date is not None:
                lead_times.append((breach_date - warning.created_at.date()).days)

        if not lead_times:
            reason = (
                "No forecast raised in this period was later confirmed by an actual "
                "covenant breach, so lead time could not be measured."
            )
            return MisLeadTimeSection(
                lead_time_distribution=MisMetric.absent("Lead time achieved", reason=reason),
                at_least_30_days_pct=MisMetric.absent(
                    f"Flagged at least {G1_EARLY_LEAD_DAYS} days early", reason=reason
                ),
                at_least_60_days_pct=MisMetric.absent(
                    f"Flagged at least {G1_STRONG_LEAD_DAYS} days early", reason=reason
                ),
            )

        bucket_counts: dict[str, int] = {label: 0 for label, _low, _high in _LEAD_TIME_BUCKETS}
        for days in lead_times:
            for label, low, high in _LEAD_TIME_BUCKETS:
                if days >= low and (high is None or days <= high):
                    bucket_counts[label] += 1
                    break
        distribution = MisMetric.present(
            "Lead time achieved", unit="warnings", points=_points_from_counts(bucket_counts)
        )
        total = len(lead_times)
        at_least_30 = sum(1 for days in lead_times if days >= G1_EARLY_LEAD_DAYS)
        at_least_60 = sum(1 for days in lead_times if days >= G1_STRONG_LEAD_DAYS)
        return MisLeadTimeSection(
            lead_time_distribution=distribution,
            at_least_30_days_pct=MisMetric.single(
                f"Flagged at least {G1_EARLY_LEAD_DAYS} days early",
                unit="%",
                value=_percentage(at_least_30, total),
            ),
            at_least_60_days_pct=MisMetric.single(
                f"Flagged at least {G1_STRONG_LEAD_DAYS} days early",
                unit="%",
                value=_percentage(at_least_60, total),
            ),
        )

    def _earliest_subsequent_breach(
        self, scope: Scope, covenant_version_id: UUID, on_or_after: date
    ) -> date | None:
        ownership = ownership_path_for(CovenantTest)
        statement = ownership.apply(select(CovenantTest)).where(
            scope.predicate(ownership.path_column),
            CovenantTest.covenant_version_id == covenant_version_id,
            CovenantTest.as_of_date >= on_or_after,
            CovenantTest.verdict.in_(("breach", "breach_cure_open")),
        )
        statement = statement.order_by(CovenantTest.as_of_date, CovenantTest.id).limit(1)
        row = self.session.execute(statement).scalars().first()
        return row.as_of_date if row is not None else None

    def _escalation_section(
        self, scope: Scope, period: MisPeriod, current_covenant: Mapping[UUID, tuple[str, UUID]]
    ) -> MisEscalationSection:
        ownership = ownership_path_for(Forecast)
        statement = ownership.apply(select(Forecast)).where(
            scope.predicate(ownership.path_column),
            Forecast.below_confidence_floor.is_(False),
            Forecast.created_at >= period.start_datetime,
            Forecast.created_at < period.end_datetime_exclusive,
        )
        warnings = tuple(self.session.execute(statement).scalars().all())
        warnings_metric = MisMetric.single(
            "Warnings raised", unit="warnings", value=Decimal(len(warnings))
        )

        if not current_covenant:
            amber_metric = MisMetric.absent(
                "Portfolio amber-or-worse",
                reason=f"No covenant tests are recorded on or before {period.end.isoformat()}.",
            )
        else:
            borrower_worst: dict[UUID, bool] = {}
            for verdict, borrower_id in current_covenant.values():
                is_amber = verdict in _AMBER_OR_WORSE_VERDICTS
                borrower_worst[borrower_id] = borrower_worst.get(borrower_id, False) or is_amber
            amber_count = sum(1 for value in borrower_worst.values() if value)
            amber_metric = MisMetric.single(
                "Portfolio amber-or-worse",
                unit="%",
                value=_percentage(amber_count, len(borrower_worst)),
            )

        warning_ids = {warning.id for warning in warnings}
        if warning_ids:
            disposition_statement = select(Disposition).where(
                Disposition.subject_type == "forecast",
                Disposition.subject_id.in_(warning_ids),
                Disposition.created_at >= period.start_datetime,
                Disposition.created_at < period.end_datetime_exclusive,
            )
            dispositions = tuple(self.session.execute(disposition_statement).scalars().all())
        else:
            dispositions = ()

        if not dispositions:
            reason = "No dispositions were recorded against a warning in this period."
            outcomes_metric = MisMetric.absent("Disposition outcomes", reason=reason)
            acted_on_metric = MisMetric.absent("Escalations acted on", reason=reason)
        else:
            outcome_counts = _count_by(row.outcome for row in dispositions)
            outcomes_metric = MisMetric.present(
                "Disposition outcomes",
                unit="dispositions",
                points=_points_from_counts(outcome_counts),
            )
            acted = sum(1 for row in dispositions if row.outcome == _DISPOSITION_ACTED_OUTCOME)
            acted_on_metric = MisMetric.single(
                "Escalations acted on", unit="%", value=_percentage(acted, len(dispositions))
            )

        return MisEscalationSection(
            warnings_raised=warnings_metric,
            amber_or_worse_pct=amber_metric,
            disposition_outcomes=outcomes_metric,
            acted_on_pct=acted_on_metric,
        )

    def _model_performance_section(self, period: MisPeriod) -> MisModelPerformanceSection:
        statement = (
            select(EvaluationRun)
            .where(EvaluationRun.executed_at < period.end_datetime_exclusive)
            .order_by(EvaluationRun.executed_at.desc(), EvaluationRun.id.desc())
            .limit(1)
        )
        run = self.session.execute(statement).scalars().first()
        if run is None:
            reason = f"No evaluation run has been recorded as of {period.end.isoformat()}."
            return MisModelPerformanceSection(
                commit_sha=None,
                arm=None,
                passed=None,
                executed_at=None,
                scores=MisMetric.absent("Evaluation scores", reason=reason),
            )
        points = _numeric_points(run.scores if isinstance(run.scores, Mapping) else {})
        if not points:
            scores_metric = MisMetric.absent(
                "Evaluation scores",
                reason=f"Evaluation run {run.id} recorded no numeric scores.",
            )
        else:
            scores_metric = MisMetric.present("Evaluation scores", unit="score", points=points)
        return MisModelPerformanceSection(
            commit_sha=run.commit_sha,
            arm=run.arm,
            passed=run.passed,
            executed_at=_aware(run.executed_at, "evaluation_run.executed_at"),
            scores=scores_metric,
        )

    def _connector_section(self, period: MisPeriod) -> MisConnectorSection:
        connector_rows = tuple(
            self.session.execute(select(Connector).order_by(Connector.name)).scalars().all()
        )
        if not connector_rows:
            reason = "No connectors are configured."
            return MisConnectorSection(
                entries=(),
                record_counts=MisMetric.absent("Connector record counts", reason=reason),
                reject_counts=MisMetric.absent("Connector reject counts", reason=reason),
                lag_seconds=MisMetric.absent("Connector lag", reason=reason),
            )

        entries: list[MisConnectorEntry] = []
        record_points: list[MisChartPoint] = []
        reject_points: list[MisChartPoint] = []
        lag_points: list[MisChartPoint] = []
        for connector in connector_rows:
            run_statement = (
                select(ConnectorRun)
                .where(
                    ConnectorRun.connector_id == connector.id,
                    ConnectorRun.started_at < period.end_datetime_exclusive,
                )
                .order_by(ConnectorRun.started_at.desc(), ConnectorRun.id.desc())
                .limit(1)
            )
            run = self.session.execute(run_statement).scalars().first()
            entries.append(
                MisConnectorEntry(
                    name=connector.name,
                    connector_type=connector.connector_type,
                    is_active=connector.is_active,
                    latest_run_state=run.state if run is not None else None,
                    latest_run_started_at=(
                        _aware(run.started_at, "connector_run.started_at")
                        if run is not None
                        else None
                    ),
                    record_count=run.record_count if run is not None else None,
                    reject_count=run.reject_count if run is not None else None,
                    lag_seconds=run.lag_seconds if run is not None else None,
                )
            )
            if run is not None and run.record_count is not None:
                record_points.append(
                    MisChartPoint(label=connector.name, value=Decimal(run.record_count))
                )
            if run is not None and run.reject_count is not None:
                reject_points.append(
                    MisChartPoint(label=connector.name, value=Decimal(run.reject_count))
                )
            if run is not None and run.lag_seconds is not None:
                lag_points.append(
                    MisChartPoint(label=connector.name, value=Decimal(run.lag_seconds))
                )

        no_run_reason = (
            f"No connector reconciliation run had completed by {period.end.isoformat()}."
        )
        return MisConnectorSection(
            entries=tuple(entries),
            record_counts=(
                MisMetric.present("Connector record counts", unit="records", points=record_points)
                if record_points
                else MisMetric.absent("Connector record counts", reason=no_run_reason)
            ),
            reject_counts=(
                MisMetric.present("Connector reject counts", unit="rejects", points=reject_points)
                if reject_points
                else MisMetric.absent("Connector reject counts", reason=no_run_reason)
            ),
            lag_seconds=(
                MisMetric.present("Connector lag", unit="seconds", points=lag_points)
                if lag_points
                else MisMetric.absent("Connector lag", reason=no_run_reason)
            ),
        )

    # ---- shared fact gathering -------------------------------------------

    def _latest_covenant_verdicts(
        self, scope: Scope, as_of_date: date
    ) -> dict[UUID, tuple[str, UUID]]:
        """Return `{covenant_id: (latest_verdict, borrower_id)}` as of `as_of_date`."""

        covenant_ownership = ownership_path_for(Covenant)
        covenant_statement = covenant_ownership.apply(select(Covenant)).where(
            scope.predicate(covenant_ownership.path_column)
        )
        covenants = tuple(self.session.execute(covenant_statement).scalars().all())
        if not covenants:
            return {}
        facility_borrower = {row.id: row.borrower_id for row in self.facilities.list(scope=scope)}
        covenant_facility = {row.id: row.facility_id for row in covenants}

        version_ownership = ownership_path_for(CovenantVersion)
        version_statement = version_ownership.apply(select(CovenantVersion)).where(
            scope.predicate(version_ownership.path_column)
        )
        version_to_covenant = {
            row.id: row.covenant_id
            for row in self.session.execute(version_statement).scalars().all()
        }

        test_ownership = ownership_path_for(CovenantTest)
        test_statement = test_ownership.apply(select(CovenantTest)).where(
            scope.predicate(test_ownership.path_column), CovenantTest.as_of_date <= as_of_date
        )
        test_statement = test_statement.order_by(
            CovenantTest.as_of_date.desc(), CovenantTest.id.desc()
        )
        result: dict[UUID, tuple[str, UUID]] = {}
        for row in self.session.execute(test_statement).scalars().all():
            covenant_id = version_to_covenant.get(row.covenant_version_id)
            if covenant_id is None or covenant_id in result:
                continue
            facility_id = covenant_facility.get(covenant_id)
            borrower_id = facility_borrower.get(facility_id) if facility_id is not None else None
            if borrower_id is None:
                continue
            result[covenant_id] = (row.verdict, borrower_id)
        return result

    def _borrower_sma_bands(self, scope: Scope, as_of_date: date) -> dict[UUID, SmaBand]:
        borrower_rows = self.borrowers.list(scope=scope)
        if not borrower_rows:
            return {}
        facility_rows = self.facilities.list(scope=scope)
        by_borrower: dict[UUID, list[Facility]] = {}
        for facility in facility_rows:
            if _is_effective(facility, as_of_date):
                by_borrower.setdefault(facility.borrower_id, []).append(facility)

        all_facility_ids = tuple(
            facility.id for facilities in by_borrower.values() for facility in facilities
        )
        conduct_by_facility: dict[object, FacilityConduct] = {}
        if all_facility_ids:
            conduct_ownership = ownership_path_for(FacilityConduct)
            conduct_statement = conduct_ownership.apply(select(FacilityConduct)).where(
                scope.predicate(conduct_ownership.path_column),
                FacilityConduct.facility_id.in_(all_facility_ids),
                FacilityConduct.as_of_date == as_of_date,
            )
            conduct_by_facility = {
                row.facility_id: row
                for row in self.session.execute(conduct_statement).scalars().all()
            }

        result: dict[UUID, SmaBand] = {}
        for borrower in borrower_rows:
            facilities = by_borrower.get(borrower.id, [])
            derivation: BorrowerSmaDerivation = derive_borrower_sma(
                conduct_by_facility,
                as_of_date=as_of_date,
                borrower_id=borrower.id,
                facility_ids=tuple(facility.id for facility in facilities),
            )
            result[borrower.id] = derivation.band
        return result

    # ---- rendering and plumbing -------------------------------------------

    def _render_html(self, report: MisReport) -> str:
        context = _template_context(report)
        html = self.environment.get_template(_TEMPLATE_NAME).render(**context)
        if len(html.encode("utf-8")) > _MAX_RENDERED_HTML_BYTES:
            raise ValueError("Board MIS export exceeds the maximum rendered document size.")
        return html

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        resolved = self.scope_resolver(principal) if scope is None else scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied scope does not belong to the authenticated principal."
            )
        return resolved

    def _now(self) -> datetime:
        instant = self.clock.now()
        return _aware(instant, "clock.now()")


# --------------------------------------------------------------------------
# Delivery service
# --------------------------------------------------------------------------


class MisReportDeliveryService:
    """Deliver one generation to a distribution list, retrying each
    recipient up to a bounded number of attempts and surfacing exhaustion
    rather than dropping it (`spec §R-31`'s "every case": a scheduled
    delivery failing is retried and, on exhaustion, surfaced to the
    administrator)."""

    def __init__(
        self,
        session: Session,
        notifier: Notifier,
        *,
        audit: ReportAuditWriter,
        clock: Clock | None = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = lambda _seconds: None,
        retry_base_seconds: float = 1.0,
        dead_letter_alert: Callable[[UUID, str], object] | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("MisReportDeliveryService requires a SQLAlchemy Session.")
        if notifier is None or not callable(getattr(notifier, "send", None)):
            raise TypeError("MisReportDeliveryService requires a Notifier.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("MisReportDeliveryService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("MisReportDeliveryService clock must expose now().")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer.")
        if not callable(sleep):
            raise TypeError("sleep must be callable.")
        if dead_letter_alert is not None and not callable(dead_letter_alert):
            raise TypeError("dead_letter_alert must be callable.")

        self.session = session
        self.notifier = notifier
        self.audit = audit
        self.clock = clock or SystemClock()
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.retry_base_seconds = retry_base_seconds
        self.dead_letter_alert = dead_letter_alert

    def deliver(
        self,
        export: MisReportExportResult,
        distribution_list: Sequence[UUID],
        *,
        actor_id: UUID,
        request_id: str | None = None,
    ) -> tuple[MisDeliveryOutcome, ...]:
        """Attempt delivery to every recipient; never raises for a
        recipient failure — the caller decides what to do with a
        non-delivered outcome."""

        if isinstance(distribution_list, str | bytes):
            raise TypeError("distribution_list must be a sequence of recipient UUIDs.")
        recipient_ids = tuple(distribution_list)
        if not recipient_ids:
            raise ValidationError("A distribution list must name at least one recipient.")
        if not all(isinstance(item, UUID) for item in recipient_ids):
            raise TypeError("distribution_list must contain UUID recipient ids.")
        effective_request_id = request_id or get_request_id() or new_request_id()

        recipients = {
            row.id: row
            for row in self.session.execute(select(AppUser).where(AppUser.id.in_(recipient_ids)))
            .scalars()
            .all()
        }
        missing = [str(item) for item in recipient_ids if item not in recipients]
        if missing:
            raise ValidationError(f"Distribution list recipient not found: {missing[0]}")

        outcomes: list[MisDeliveryOutcome] = []
        for recipient_id in recipient_ids:
            user = recipients[recipient_id]
            if not user.is_active:
                outcomes.append(
                    MisDeliveryOutcome(
                        recipient_id=recipient_id,
                        delivered=False,
                        attempts=0,
                        reason="recipient is inactive",
                    )
                )
                continue
            outcomes.append(self._deliver_one(export, recipient_id, actor_id, effective_request_id))
        return tuple(outcomes)

    def _deliver_one(
        self, export: MisReportExportResult, recipient_id: UUID, actor_id: UUID, request_id: str
    ) -> MisDeliveryOutcome:
        message = OutboundMessage(
            recipient_id=recipient_id,
            channel="email",
            template="board_mis",
            subject=f"Board MIS — {export.report.period.label}",
            body=_plain_text_summary(export.report),
            payload={"content_hash": export.content_hash},
        )
        last_reason = "delivery failed"
        for attempt in range(1, self.max_attempts + 1):
            try:
                result: DeliveryResult = self.notifier.send(message)
            except Exception as error:  # noqa: BLE001 - reported through the outcome, not swallowed
                last_reason = str(error).strip()[:2000] or "delivery raised an exception"
                result = DeliveryResult(DeliveryStatus.RETRY, error=last_reason)
            if result.status is DeliveryStatus.SENT:
                return MisDeliveryOutcome(
                    recipient_id=recipient_id, delivered=True, attempts=attempt
                )
            last_reason = result.error or last_reason
            if result.status is DeliveryStatus.DEAD_LETTERED:
                break
            if attempt < self.max_attempts:
                self.sleep(self.retry_base_seconds * (2 ** (attempt - 1)))

        self.audit.record(
            _MIS_REPORT_DELIVERY_FAILED_EVENT,
            (_MIS_SUBJECT_TYPE, _period_subject_key(export.report.period)),
            {
                "recipient_id": str(recipient_id),
                "attempts": self.max_attempts,
                "reason": last_reason,
                "content_hash": export.content_hash,
            },
            actor=actor_id,
            request_id=request_id,
        )
        if self.dead_letter_alert is not None:
            self.dead_letter_alert(recipient_id, last_reason)
        return MisDeliveryOutcome(
            recipient_id=recipient_id,
            delivered=False,
            attempts=self.max_attempts,
            reason=last_reason,
        )


# --------------------------------------------------------------------------
# Module-level helpers
# --------------------------------------------------------------------------


def _is_effective(facility: Facility, as_of_date: date) -> bool:
    return facility.effective_from <= as_of_date and (
        facility.effective_to is None or as_of_date < facility.effective_to
    )


def _count_by(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _points_from_counts(counts: Mapping[str, int]) -> tuple[MisChartPoint, ...]:
    return tuple(
        MisChartPoint(label=label, value=Decimal(counts[label])) for label in sorted(counts)
    )


def _percentage(count: int, total: int) -> Decimal:
    if total <= 0:
        raise ValueError("_percentage requires a positive total.")
    return (Decimal(count) * _HUNDRED / Decimal(total)).quantize(Decimal("0.01"))


def _numeric_points(scores: Mapping[str, object]) -> tuple[MisChartPoint, ...]:
    points: list[MisChartPoint] = []
    for key in sorted(scores):
        value = scores[key]
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            points.append(MisChartPoint(label=key, value=Decimal(str(value))))
        elif isinstance(value, Decimal):
            points.append(MisChartPoint(label=key, value=value))
    return tuple(points)


def _plain_text_summary(report: MisReport) -> str:
    lines = [f"Board MIS — {report.period.label}", ""]
    for section_title, metrics in (
        (
            "Portfolio distribution",
            (report.distribution.covenant_band, report.distribution.sma_band),
        ),
        (
            "Early-warning lead time",
            (report.lead_time.at_least_30_days_pct, report.lead_time.at_least_60_days_pct),
        ),
        (
            "Escalations",
            (
                report.escalations.warnings_raised,
                report.escalations.amber_or_worse_pct,
                report.escalations.acted_on_pct,
            ),
        ),
        ("Model performance", (report.model_performance.scores,)),
    ):
        lines.append(section_title + ":")
        for metric in metrics:
            if metric.is_present:
                figures = ", ".join(
                    f"{point.label}={point.value}{metric.unit}" for point in metric.points
                )
                lines.append(f"  {metric.name}: {figures}")
            else:
                lines.append(f"  {metric.name}: not available — {metric.absent_reason}")
        lines.append("")
    return "\n".join(lines).strip()


def _template_context(report: MisReport) -> dict[str, object]:
    def metric_context(metric: MisMetric) -> dict[str, object]:
        return {
            "name": metric.name,
            "unit": metric.unit,
            "is_present": metric.is_present,
            "absent_reason": metric.absent_reason,
            "points": [
                {"label": point.label, "value": format(point.value, "f")} for point in metric.points
            ],
        }

    return {
        "period_label": report.period.label,
        "distribution_as_of": report.distribution.as_of_date.isoformat(),
        "sections": [
            {
                "title": "Portfolio distribution by band and SMA",
                "metrics": [
                    metric_context(report.distribution.covenant_band),
                    metric_context(report.distribution.sma_band),
                ],
            },
            {
                "title": "Migration since the prior period",
                "metrics": [
                    metric_context(report.migration.covenant_band_migration),
                    metric_context(report.migration.sma_migration),
                ],
            },
            {
                "title": "Early-warning lead time achieved",
                "metrics": [
                    metric_context(report.lead_time.lead_time_distribution),
                    metric_context(report.lead_time.at_least_30_days_pct),
                    metric_context(report.lead_time.at_least_60_days_pct),
                ],
            },
            {
                "title": "Escalations and dispositions",
                "metrics": [
                    metric_context(report.escalations.warnings_raised),
                    metric_context(report.escalations.amber_or_worse_pct),
                    metric_context(report.escalations.disposition_outcomes),
                    metric_context(report.escalations.acted_on_pct),
                ],
            },
            {
                "title": "Model performance",
                "metrics": [metric_context(report.model_performance.scores)],
            },
            {
                "title": "Connectors and data quality",
                "metrics": [
                    metric_context(report.connectors.record_counts),
                    metric_context(report.connectors.reject_counts),
                    metric_context(report.connectors.lag_seconds),
                ],
            },
        ],
        "model_performance_meta": [
            ("Commit", report.model_performance.commit_sha or "—"),
            ("Arm", report.model_performance.arm or "—"),
            (
                "Passed",
                "—"
                if report.model_performance.passed is None
                else ("Yes" if report.model_performance.passed else "No"),
            ),
            (
                "Executed at",
                report.model_performance.executed_at.isoformat()
                if report.model_performance.executed_at
                else "—",
            ),
        ],
        "connector_entries": [
            {
                "name": entry.name,
                "connector_type": entry.connector_type,
                "is_active": "active" if entry.is_active else "inactive",
                "latest_run_state": entry.latest_run_state or "no run recorded",
                "record_count": entry.record_count if entry.record_count is not None else "—",
                "reject_count": entry.reject_count if entry.reject_count is not None else "—",
                "lag_seconds": entry.lag_seconds if entry.lag_seconds is not None else "—",
            }
            for entry in report.connectors.entries
        ],
    }


# --------------------------------------------------------------------------
# Scheduled job composition
# --------------------------------------------------------------------------


def previous_calendar_month(today: date) -> MisPeriod:
    """The default reporting period: the whole calendar month before `today`."""

    first_of_this_month = today.replace(day=1)
    end = first_of_this_month - timedelta(days=1)
    start = end.replace(day=1)
    return MisPeriod(start=start, end=end)


def build_board_mis_job_handler(
    session_factory: SessionFactory,
    *,
    distribution_list: Sequence[UUID],
    system_actor_id: UUID,
    notifier: Notifier,
    audit_factory: Callable[[Session], ReportAuditWriter],
    scope_resolver: Callable[[Principal], Scope] | None = None,
    clock: Clock | None = None,
    period_for: Callable[[date], MisPeriod] = previous_calendar_month,
) -> JobHandler:
    """Build the `JobHandler` `scheduler.jobs.board_mis_report_job` schedules.

    Composition, not business logic: this wires a session, the audit
    writer, the reporting period and the distribution list into
    `MisReportService`/`MisReportDeliveryService` for one job attempt. Any
    recipient still undelivered after `MisReportDeliveryService`'s own
    bounded retries fails the whole attempt, so `scheduler.runner.JobRunner`
    applies the job's own retry policy on top and — on exhaustion — leaves
    the run `failed` in `job_run`, where the administrator operations
    screen already surfaces it.
    """

    if not callable(session_factory):
        raise TypeError("build_board_mis_job_handler requires a callable session_factory.")
    if not isinstance(system_actor_id, UUID):
        raise TypeError("build_board_mis_job_handler requires a system_actor_id UUID.")
    if not callable(audit_factory):
        raise TypeError("build_board_mis_job_handler requires a callable audit_factory.")
    resolved_clock = clock or SystemClock()

    def handler(context: JobRunContext) -> Mapping[str, object]:
        session = session_factory()
        try:
            audit = audit_factory(session)
            today = (
                date.fromisoformat(context.as_of) if context.as_of else resolved_clock.now().date()
            )
            period = period_for(today)
            previous_period = period_for(period.start)

            principal = Principal.user(system_actor_id, (Permission.EXPORT_EVIDENCE,))
            report_service = MisReportService(
                session,
                audit=audit,
                clock=resolved_clock,
                scope_resolver=scope_resolver,
                request_id=context.request_id,
            )
            export = report_service.export(
                principal,
                period=period,
                previous_period=previous_period,
                request_id=context.request_id,
            )

            delivery_service = MisReportDeliveryService(
                session, notifier, audit=audit, clock=resolved_clock
            )
            outcomes = delivery_service.deliver(
                export,
                distribution_list,
                actor_id=system_actor_id,
                request_id=context.request_id,
            )
            session.commit()

            failed = tuple(item for item in outcomes if not item.delivered)
            metrics: dict[str, object] = {
                "period_start": period.start.isoformat(),
                "period_end": period.end.isoformat(),
                "content_hash": export.content_hash,
                "recipients_delivered": len(outcomes) - len(failed),
                "recipients_failed": len(failed),
            }
            if failed:
                raise ExternalServiceError(
                    "Board MIS delivery failed for "
                    f"{len(failed)} of {len(outcomes)} recipient(s) after retries: "
                    + ", ".join(str(item.recipient_id) for item in failed)
                )
            return metrics
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return handler


def _period_subject_key(period: MisPeriod) -> str:
    return f"{period.start.isoformat()}:{period.end.isoformat()}"


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


__all__ = [
    "G1_EARLY_LEAD_DAYS",
    "G1_STRONG_LEAD_DAYS",
    "MisChartPoint",
    "MisConnectorEntry",
    "MisConnectorSection",
    "MisDeliveryOutcome",
    "MisDistributionSection",
    "MisEscalationSection",
    "MisLeadTimeSection",
    "MisMetric",
    "MisMigrationSection",
    "MisModelPerformanceSection",
    "MisPeriod",
    "MisReport",
    "MisReportDeliveryService",
    "MisReportExportResult",
    "MisReportService",
    "ReportAuditWriter",
    "build_board_mis_job_handler",
    "previous_calendar_month",
]
