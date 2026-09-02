"""The nightly pipeline's step handlers (`T-121`, `spec §R-28.a`/`R-28.c`)
and its partial-failure policy (`T-122`, `spec §R-28.b`/`R-28.d`).

`NightlyPipelineService` supplies the six `JobHandler`s
`scheduler.pipeline.register_nightly_pipeline` registers under
`nightly.ingest`, `nightly.test`, `nightly.score`, `nightly.rank`,
`nightly.update_cases` and `nightly.dispatch`. Each handler opens and commits
its own session — a `JobHandler` receives only a `JobRunContext`, no session,
matching every other job this codebase runs (`scheduler.runner.JobRunner`).

**Partial failure (`T-122`).** Every step that loops over borrowers or
covenants isolates each item in its own `Session.begin_nested()` savepoint
(`scheduler.policy.IsolationTracker`): one bad borrower is recorded and
skipped, never lets its exception halt the rest of the book, and every
isolated failure stays visible in that step's own `JobRun.metrics` rather
than being silently folded into a clean-looking success count. Two further
capabilities, `check_deadline` (T12) and `check_recurring_failure`, are not
steps themselves — they read the job ledger's own history and, through the
same `AuditRecorder` boundary every other write in this module already uses,
raise a durable, idempotent-per-run record of a missed deadline or an
unresolved recurring failure. `T-145` is the deferred task that turns that
record into an actual paged alert; this module's job stops at raising it.

**Idempotency by pipeline run id, without a new table.** A pipeline run id
(`JobRunContext.run_id`) is a string shared by every step's own `job_run`
row for that run — `scheduler.ledger.JobLedger` already gives each attempt its
own row while keeping `run_id` stable across retries. `nightly.score` and
`nightly.rank` need a stable link from that string to the `ForecastRun` UUID
their domain tables use; rather than mint one (which `ForecastScoringService`
has no way to accept — it always creates a run with a fresh id, only ever
*resuming* one given an existing id), this module discovers it by joining
`forecast_run.job_run_id` back to the `job_run` rows already recorded for
`(run_id, "nightly.score")`. The first attempt finds none and creates a run;
every later attempt (a retry, or the same run triggered twice) finds the one
already there and resumes or reads it — which is also how a threshold
snapshot changing mid-run stays pinned: the snapshot id embedded in that first
`ForecastRun` row is reused verbatim by every later attempt, never
re-resolved against whatever is active by then.

**Single-borrower runs.** `borrower_id` on `JobRunContext` scopes every step
to one borrower's own data: `nightly.test` tests only their covenants,
`nightly.score`/`nightly.rank` create a `ForecastRun` and `TriageEntry` rows
that reference only their forecasts, and `nightly.update_cases`/
`nightly.dispatch` act only on cases opened from that run. A single-borrower
trigger's `run_id` is never the nightly batch's own, so its `ForecastRun` is
never linked from the batch's `job_run` rows — it does not extend or touch
the portfolio-wide run the batch produced. Existing repositories that read
"the newest complete run for today" (`ForecastRepository.latest_complete_run`,
`TriageRepository`) do not further distinguish a batch run from an ad hoc
one; an operator running a single-borrower recheck after the nightly batch
has already completed for the day should treat it as an off-cycle review,
not a substitute for the next full run — extending those read paths with a
run-kind distinction is follow-on work, not this task's to make.

**No connector yet.** `signal_source` and `statement_lines` are the seams
`nightly.ingest` and `nightly.test` use to reach real source data; both
default to "nothing available" because `T-123`'s connector framework and the
statement-reconstruction pipeline are out of this window's scope. That is an
honest, observable outcome (zero events ingested; a covenant left untested
with a recorded reason) rather than a fabricated one, and needs no change
here once a real provider exists — only a constructor argument.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from covenant_radar.audit.record import AuditRecorder, AuditSubject
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.core.errors import NotFound
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
)
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.forecast import Forecast, ForecastRun
from covenant_radar.db.models.forecast import TriageEntry as TriageEntryModel
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import (
    EvidenceItem,
    EvidenceTransition,
)
from covenant_radar.db.models.signal import (
    SignalEvent as SignalEventModel,
)
from covenant_radar.db.models.workflow import Case, Notification
from covenant_radar.db.repositories.audit import SqlAlchemyAuditStore
from covenant_radar.db.repositories.evidence import EvidenceRepository
from covenant_radar.db.repositories.forecast import COMPLETE as COMPLETE_FORECAST_RUN_STATE
from covenant_radar.db.repositories.trace import TraceRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.db.session import SessionFactory
from covenant_radar.domain.covenants.calendar import ScheduleState
from covenant_radar.domain.covenants.sma import derive_borrower_sma
from covenant_radar.domain.forecast import Observation, Weights, evidence_pressure
from covenant_radar.domain.forecast.predictor import ForecastPredictor
from covenant_radar.domain.signals import SignalEvent
from covenant_radar.domain.signals.evidence import EvidenceFacts
from covenant_radar.domain.signals.persistence import PersistenceThresholds
from covenant_radar.domain.triage.banding import ACT_BAND, TriageThresholds
from covenant_radar.domain.triage.urgency import ForecastFact, TriageInput
from covenant_radar.domain.triage.urgency import rank as rank_triage_entries
from covenant_radar.notifications.templates import BAND_CHANGE_TEMPLATE
from covenant_radar.ports.notifier import NotificationChannel
from covenant_radar.scheduler.jobs import JobHandler, JobRunContext
from covenant_radar.scheduler.ledger import FAILED, SUCCEEDED
from covenant_radar.scheduler.pipeline import (
    PIPELINE_STEPS,
    STEP_DISPATCH,
    STEP_INGEST,
    STEP_RANK,
    STEP_SCORE,
    STEP_TEST,
    STEP_UPDATE_CASES,
)
from covenant_radar.scheduler.policy import (
    DEFAULT_RECURRING_FAILURE_THRESHOLD,
    IsolationTracker,
    PipelineRunStatus,
    evaluate_deadline,
    is_recurring_failure,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind
from covenant_radar.services.engine import EngineService
from covenant_radar.services.ingestion import SignalIngestionService
from covenant_radar.services.ledger import LedgerService, _stage3_trace
from covenant_radar.services.scoring import (
    PREDICTOR_MODES,
    SHADOW_PREDICTOR_MODE,
    ForecastCandidate,
    ForecastScoringService,
)


@runtime_checkable
class ThresholdSnapshotProvider(Protocol):
    """The adapter-neutral threshold surface this service needs.

    `config.thresholds.ThresholdStore` satisfies this directly. Accepting the
    structural shape rather than that concrete class matches how
    `domain.triage.banding.TriageThresholds.from_store` and
    `services.scoring.ForecastScoringService` already treat threshold access
    everywhere else in the codebase — and lets a test double stand in without
    a database-backed `ThresholdRepository`, which nothing in this codebase
    implements yet.
    """

    def get(self, name: str) -> Mapping[str, object]:
        """Return one named threshold's fields (e.g. `"T1"` -> act/amber)."""

    def snapshot_id(self) -> UUID:
        """Return the snapshot id that must be stamped on a decision."""


class _AuditWriterAdapter:
    """Narrows `AuditRecorder.record`'s wider signature to the exact
    `AuditWriter` Protocol each of `EngineService`, `SignalIngestionService`
    and `ForecastScoringService` separately declares (`subject: object`,
    `request_id: str` with no default) — `AuditRecorder` is documented as
    "the single application-facing audit write boundary" but is a wider,
    optional-`request_id` signature than any of those three narrower
    protocols, so nothing else in this codebase actually hands one to any of
    them today. This adapter is that boundary.
    """

    __slots__ = ("_recorder",)

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        # Every caller in this module passes the (subject_type, subject_id)
        # tuple shape `AuditRecorder.record` actually requires; `object` here
        # only widens this adapter's own declared Protocol to match callers'.
        return self._recorder.record(
            event_type,
            subject,  # type: ignore[arg-type]
            payload,
            actor=actor,
            request_id=request_id,
        )


#: A source of raw signal events for `nightly.ingest`; `None` (the default)
#: means no connector is configured yet, so the step honestly ingests nothing.
SignalSourceProvider = Callable[[], Iterable[SignalEvent | Mapping[str, object]]]

#: Resolves the normalized statement lines to test one covenant version as of
#: one date; `None` means "no statement data available for this covenant yet"
#: — the step leaves it untested rather than inventing a result.
StatementLinesProvider = Callable[[CovenantVersion, date], Mapping[str, Decimal] | None]

_MODEL_VERSION: Final[str] = "nightly.pipeline.v1"
# Must name a template the in-app registry knows
# (`notifications.templates`), or the notification centre cannot render the
# row and falls back to "no longer available in your access scope".  An
# act-band case *is* a band change, which is the registered template for it.
_ACT_ALERT_TEMPLATE: Final[str] = BAND_CHANGE_TEMPLATE.name
# The act alert is a workspace notice for the case assignee, so it is written
# to the always-available in-app channel that the notification centre reads
# (`notifications.inapp._IN_APP_CHANNELS`).  Writing it to "email" made it
# invisible in the app *and* undeliverable wherever SMTP is unconfigured,
# leaving every act-band alert stranded as a `pending` row nobody ever saw.
_NOTIFICATION_CHANNEL: Final[str] = NotificationChannel.IN_APP.value
_BREACH_VERDICTS: Final[frozenset[str]] = frozenset({"breach", "breach_cure_open"})
_TEST_HISTORY_LIMIT: Final[int] = 366
#: `Facility` money columns are denominated in ₹ crore; `TriageEntry.exposure`
#: is in rupees.  See `_borrower_exposure`.
_RUPEES_PER_CRORE: Final[Decimal] = Decimal("10000000")

#: T12 (`spec §17.5`): the audit event `check_deadline` raises when a
#: pipeline run is still open past its deadline. `T-145` is the deferred
#: task that turns this durable, replayable record into an actual paged
#: alert; this module's job stops at recording the fact, once, per run.
_DEADLINE_ALERT_EVENT: Final[str] = "nightly.deadline_alert_raised"
_DEADLINE_ALERT_SUBJECT_TYPE: Final[str] = "pipeline_run"

#: The audit event `check_recurring_failure` raises when one step has
#: failed with the same normalized cause on `threshold` consecutive
#: pipeline runs — "a nightly failure that alerts identically every night
#: stops being read" (`spec §R-28.b`).
_RECURRING_FAILURE_EVENT: Final[str] = "nightly.step_failure_escalated"
_RECURRING_FAILURE_SUBJECT_TYPE: Final[str] = "job_step_run"

#: How many of a job's most recent, run-distinct terminal outcomes
#: `check_recurring_failure` reads before deciding — always at least
#: `policy.DEFAULT_RECURRING_FAILURE_THRESHOLD`.
_RECURRING_FAILURE_LOOKBACK: Final[int] = 10


class NightlyPipelineService:
    """The six step handlers behind the nightly pipeline's job names, plus
    two standing capabilities the batch's partial-failure policy needs:
    `check_deadline` (T12) and `check_recurring_failure` (`spec §R-28.b`).
    Neither is one of the six pipeline steps — both are meant to be called
    by whatever schedules them (`T-145`'s deferred alert wiring, or an
    operator/test calling directly), against a run or job that has already
    been observed to be open or failing.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        threshold_store: ThresholdSnapshotProvider,
        horizons: Sequence[int],
        weights: Weights,
        system_actor_id: UUID,
        clock: Clock | None = None,
        signal_source: SignalSourceProvider | None = None,
        statement_lines: StatementLinesProvider | None = None,
        default_assignee_id: UUID | None = None,
        predictor: ForecastPredictor | None = None,
        predictor_mode: str = SHADOW_PREDICTOR_MODE,
        model_version: str = _MODEL_VERSION,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("NightlyPipelineService requires a callable session_factory.")
        if not isinstance(threshold_store, ThresholdSnapshotProvider):
            raise TypeError("NightlyPipelineService requires a ThresholdSnapshotProvider.")
        if not isinstance(weights, Weights):
            raise TypeError("NightlyPipelineService requires forecast probability Weights.")
        normalised_horizons = tuple(sorted({int(value) for value in horizons}))
        if not normalised_horizons:
            raise ValueError("horizons must be a non-empty sequence of non-negative integers.")
        if any(value < 0 for value in normalised_horizons):
            raise ValueError("horizons must not contain negative values.")
        if not isinstance(system_actor_id, UUID):
            raise TypeError("system_actor_id must be a UUID.")
        if predictor_mode not in PREDICTOR_MODES:
            raise ValueError(
                f"predictor_mode must be one of {sorted(PREDICTOR_MODES)}, "
                f"not {predictor_mode!r}."
            )
        self.session_factory = session_factory
        self.threshold_store = threshold_store
        self.horizons = normalised_horizons
        self.weights = weights
        self.system_actor_id = system_actor_id
        self.clock = clock or SystemClock()
        self.signal_source = signal_source
        self.statement_lines = statement_lines
        self.default_assignee_id = default_assignee_id
        self.predictor = predictor
        self.predictor_mode = predictor_mode
        self.model_version = model_version

    def handlers(self) -> Mapping[str, JobHandler]:
        """The six `JobHandler`s, keyed by the step name each answers to."""

        return {
            STEP_INGEST: self._run_ingest,
            STEP_TEST: self._run_test,
            STEP_SCORE: self._run_score,
            STEP_RANK: self._run_rank,
            STEP_UPDATE_CASES: self._run_update_cases,
            STEP_DISPATCH: self._run_dispatch,
        }

    # -- T12: batch completion deadline --------------------------------

    def check_deadline(
        self,
        run_id: str,
        *,
        as_of: str | None = None,
        request_id: str | None = None,
    ) -> Mapping[str, object]:
        """T12 (`spec §17.5`): raise a deadline alert, once, if `run_id`'s
        pipeline is still open at or past its configured deadline — "the
        run still open at the deadline: alert raised, run continues"
        (`spec §R-28.b`).

        Idempotent per `run_id`: a second call after the alert is already
        recorded is a no-op, so a caller polling this on a schedule
        (`T-145`, which owns actually delivering the alert) never floods
        the audit trail with the same fact twice.
        """

        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string.")
        session = self.session_factory()
        try:
            as_of_date = self._resolve_as_of(as_of)
            status = PipelineRunStatus(
                steps=self._latest_step_states(session, run_id),
                ordered_steps=PIPELINE_STEPS,
            )
            deadline_ist = str(self.threshold_store.get("T12")["deadline_ist"])
            evaluation = evaluate_deadline(
                run_id=run_id,
                as_of_date=as_of_date,
                deadline_ist=deadline_ist,
                now=self.clock.now(),
                is_complete=status.is_complete,
            )
            result: dict[str, object] = {
                "run_id": run_id,
                "is_complete": status.is_complete,
                "breached": evaluation.breached,
                "deadline_at": evaluation.deadline_at.isoformat(),
                "alert_raised": False,
                "already_raised": False,
            }
            if not evaluation.should_alert:
                session.commit()
                return result

            subject = AuditSubject(_DEADLINE_ALERT_SUBJECT_TYPE, run_id)
            already_raised = (
                session.execute(
                    select(AuditEvent.id).where(
                        AuditEvent.event_type == _DEADLINE_ALERT_EVENT,
                        AuditEvent.subject_id == subject.subject_id,
                    )
                ).scalar()
                is not None
            )
            if already_raised:
                result["already_raised"] = True
                session.commit()
                return result

            resolved_request_id = request_id or new_request_id()
            self._audit(session, resolved_request_id).record(
                _DEADLINE_ALERT_EVENT,
                subject,
                {
                    "run_id": run_id,
                    "as_of_date": as_of_date.isoformat(),
                    "deadline_at": evaluation.deadline_at.isoformat(),
                    "checked_at": evaluation.now.isoformat(),
                    "first_incomplete_step": status.first_incomplete_step,
                    "completed_steps": [
                        step for step in PIPELINE_STEPS if status.steps.get(step) == SUCCEEDED
                    ],
                },
                actor=self.system_actor_id,
                request_id=resolved_request_id,
            )
            session.commit()
            result["alert_raised"] = True
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- recurring-failure escalation -----------------------------------

    def check_recurring_failure(
        self,
        job_name: str,
        *,
        run_id: str,
        threshold: int = DEFAULT_RECURRING_FAILURE_THRESHOLD,
        request_id: str | None = None,
    ) -> Mapping[str, object]:
        """`spec §R-28.b`: escalate, once, when `job_name` has failed with
        the same normalized cause on `threshold` consecutive pipeline
        runs, `run_id`'s attempt included — "the same failure recurring
        across nights: escalated, because a nightly failure that alerts
        identically every night stops being read."

        Call this after observing `run_id`'s attempt of `job_name` end
        `failed`; idempotent per `(job_name, run_id)`.
        """

        if not isinstance(job_name, str) or not job_name.strip():
            raise ValueError("job_name must be a non-empty string.")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string.")
        session = self.session_factory()
        try:
            recent = self._recent_step_outcomes(
                session, job_name, limit=max(threshold, _RECURRING_FAILURE_LOOKBACK)
            )
            consecutive_failures: list[str] = []
            for run in recent:
                if run.state != FAILED:
                    break
                consecutive_failures.append(run.error or "")
            escalate = is_recurring_failure(consecutive_failures, threshold=threshold)
            result: dict[str, object] = {
                "job_name": job_name,
                "run_id": run_id,
                "consecutive_failures": len(consecutive_failures),
                "escalated": False,
                "already_escalated": False,
            }
            if not escalate:
                session.commit()
                return result

            subject = AuditSubject(_RECURRING_FAILURE_SUBJECT_TYPE, f"{job_name}:{run_id}")
            already_escalated = (
                session.execute(
                    select(AuditEvent.id).where(
                        AuditEvent.event_type == _RECURRING_FAILURE_EVENT,
                        AuditEvent.subject_id == subject.subject_id,
                    )
                ).scalar()
                is not None
            )
            if already_escalated:
                result["already_escalated"] = True
                session.commit()
                return result

            resolved_request_id = request_id or new_request_id()
            self._audit(session, resolved_request_id).record(
                _RECURRING_FAILURE_EVENT,
                subject,
                {
                    "job_name": job_name,
                    "run_id": run_id,
                    "consecutive_failures": len(consecutive_failures),
                    "threshold": threshold,
                },
                actor=self.system_actor_id,
                request_id=resolved_request_id,
            )
            session.commit()
            result["escalated"] = True
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- ingest ---------------------------------------------------------

    def _run_ingest(self, context: JobRunContext) -> Mapping[str, object]:
        if self.signal_source is None:
            return {
                "received": 0,
                "inserted": 0,
                "duplicates": 0,
                "rejected": 0,
                "note": "no signal source is configured",
            }
        session = self.session_factory()
        try:
            borrower_id = _parse_uuid(context.borrower_id)
            scope = self._scope_for(session, borrower_id)
            service = SignalIngestionService(
                session,
                audit=self._audit(session, context.request_id),
                clock=self.clock,
                request_id=context.request_id,
            )
            events = tuple(self.signal_source())
            report = service.ingest(self._system_principal(), events, scope=scope)
            session.commit()
            return {
                "received": report.received,
                "inserted": report.inserted,
                "duplicates": report.duplicates,
                "rejected": report.rejected,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- test -------------------------------------------------------------

    def _run_test(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            as_of_date = self._resolve_as_of(context.as_of)
            borrower_id = _parse_uuid(context.borrower_id)
            scope = self._scope_for(session, borrower_id)
            engine = EngineService(
                session,
                audit=self._audit(session, context.request_id),
                clock=self.clock,
                request_id=context.request_id,
                scope_resolver=lambda _principal: scope,
            )
            principal = self._system_principal()
            due = self._live_covenant_versions(session, scope, as_of_date, borrower_id)
            tested = 0
            already_tested = 0
            skipped_no_data = 0
            tracker = IsolationTracker()
            for version, _covenant in due:
                if self._already_tested(session, version.id, as_of_date):
                    already_tested += 1
                    continue
                # A `statement_lines` failure is left uncaught: it means the
                # statement source itself is unavailable for every covenant
                # behind it, which is `spec §R-28.a`'s halt-the-step case, not
                # one borrower's own bad data.
                lines = (
                    self.statement_lines(version, as_of_date)
                    if self.statement_lines is not None
                    else None
                )
                if lines is None:
                    skipped_no_data += 1
                    continue
                # `spec §R-28.b`: one covenant's own test failure is isolated
                # in its own savepoint so the rest of the book still gets
                # tested tonight.
                try:
                    with session.begin_nested():
                        engine.test(
                            principal,
                            covenant_version_id=version.id,
                            period=as_of_date,
                            lines=lines,
                            as_of_date=as_of_date,
                            scope=scope,
                        )
                except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
                    tracker.record_failure(version.id, error)
                    continue
                tracker.record_success()
                tested += 1
            session.commit()
            report = tracker.report()
            return {
                "due": len(due),
                "tested": tested,
                "already_tested": already_tested,
                "skipped_no_data": skipped_no_data,
                **report.as_metrics(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- score (projection, probability and driver attribution) -----------

    def _run_score(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            as_of_date = self._resolve_as_of(context.as_of)
            borrower_id = _parse_uuid(context.borrower_id)
            scope = self._scope_for(session, borrower_id)
            evidence_count = self._refresh_evidence(
                session, scope, as_of_date, borrower_id, context
            )
            candidates = self._forecast_candidates(session, scope, as_of_date, borrower_id)
            if not candidates:
                session.commit()
                return {"covenant_count": 0, "evidence_items": evidence_count}

            current_job_run = self._current_job_run(session, context.run_id, STEP_SCORE)
            existing_run = self._existing_forecast_run(session, context.run_id, STEP_SCORE)
            snapshot_id = (
                existing_run.threshold_snapshot_id
                if existing_run is not None
                else self.threshold_store.snapshot_id()
            )
            service = ForecastScoringService(
                session,
                audit=self._audit(session, context.request_id),
                threshold_store=self.threshold_store,
                clock=self.clock,
                request_id=context.request_id,
                predictor=self.predictor,
                predictor_mode=self.predictor_mode,
            )
            result = service.score(
                candidates,
                as_of_date=as_of_date,
                horizons=self.horizons,
                weights=self.weights,
                run_id=existing_run.id if existing_run is not None else None,
                threshold_snapshot_id=snapshot_id,
                job_run_id=current_job_run.id,
                model_version=self.model_version,
                request_id=context.request_id,
            )
            session.commit()
            return {
                "forecast_run_id": str(result.run_id),
                "state": result.state,
                "covenant_count": len(candidates),
                "evidence_items": evidence_count,
                "content_hash": result.content_hash,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _refresh_evidence(
        self,
        session: Session,
        scope: Scope,
        as_of_date: date,
        borrower_id: UUID | None,
        context: JobRunContext,
    ) -> int:
        """Materialise the persisted signal ledger before forecasting.

        The original pipeline had a signal ingestion seam but did not connect
        the immutable events to the stage-3 evidence ledger.  Forecasting now
        refreshes that ledger from the complete, scoped event history, applies
        the approved persistence rule, and records a bounded materiality value
        for the deterministic pressure model.  Raw events are never changed.
        """
        statement = (
            select(SignalEventModel.borrower_id)
            .join(Borrower, Borrower.id == SignalEventModel.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(
                SignalEventModel.event_date <= as_of_date,
                scope.predicate(Portfolio.path),
            )
            .distinct()
        )
        if borrower_id is not None:
            statement = statement.where(SignalEventModel.borrower_id == borrower_id)
        borrower_ids = tuple(session.execute(statement).scalars().all())
        if not borrower_ids:
            return 0

        persistence = PersistenceThresholds.from_store(self.threshold_store)
        evidence = EvidenceRepository(session, audit=self._audit(session, context.request_id))
        principal = self._system_principal()
        total = 0
        for current_borrower_id in borrower_ids:
            ledger = LedgerService(
                session,
                audit=self._audit(session, context.request_id),
                clock=self.clock,
                request_id=context.request_id,
                threshold_store=self.threshold_store,
            )
            # The ledger's contradiction engine is intentionally append-only:
            # one revision per source polarity is persisted between runs.  A
            # first run may contain thousands of daily observations, so feed
            # it one representative (latest adverse, otherwise latest)
            # observation per family.  The complete raw history is still used
            # immediately below for persistence/materiality and remains fully
            # auditable in ``signal_event``.
            all_events = (
                session.execute(
                    select(SignalEventModel).where(
                        SignalEventModel.borrower_id == current_borrower_id,
                        SignalEventModel.event_date <= as_of_date,
                    )
                )
                .scalars()
                .all()
            )
            representative_events: list[SignalEventModel] = []
            by_identity: dict[tuple[str, str], list[SignalEventModel]] = {}
            for event in all_events:
                by_identity.setdefault((event.family, event.event_type), []).append(event)
            for values in by_identity.values():
                adverse_values = [
                    event
                    for event in values
                    if bool((event.payload or {}).get("is_adverse", False))
                ]
                representative_events.append(
                    max(
                        adverse_values or values,
                        key=lambda event: (event.event_date, str(event.id)),
                    )
                )
            ledger_revision = ledger.revise(
                principal,
                current_borrower_id,
                events=representative_events,
                as_of=as_of_date,
                scope=scope,
                request_id=context.request_id,
            )
            # Only the active interpretation is refreshed.  Superseded rows
            # remain immutable history and must never be resurrected by the
            # persistence/materiality pass below.
            rows = list(
                evidence.for_borrower(
                    current_borrower_id,
                    scope=scope,
                    include_superseded=False,
                )
            )
            if not rows:
                continue
            event_rows = all_events
            grouped: dict[tuple[str, str], list[SignalEventModel]] = {}
            for event in event_rows:
                grouped.setdefault((event.family, event.event_type), []).append(event)
            for item in rows:
                events = grouped.get((item.family, item.evidence_type), [])
                # Persistence is calculated from adverse observations only.
                # A stream of healthy observations must not make a warning
                # look sustained merely because the account reported daily.
                adverse = [
                    event
                    for event in events
                    if bool((event.payload or {}).get("is_adverse", False))
                ]
                dates = sorted({event.event_date for event in adverse})
                observed_dates = sorted({event.event_date for event in events})
                consecutive = _longest_consecutive_days(dates)
                window_start = as_of_date.fromordinal(
                    as_of_date.toordinal() - persistence.event_window_days + 1
                )
                window_count = len(
                    {value for value in dates if window_start <= value <= as_of_date}
                )
                sustained = (
                    consecutive >= persistence.sustained_days
                    or window_count >= persistence.sustained_events
                )
                materiality_pct = (
                    _signal_materiality_pct(item.family, adverse) if sustained else Decimal("0")
                )
                next_state = "sustained" if sustained else "transient"
                source_event_ids = [str(event.id) for event in events if event.id is not None]
                changed = (
                    item.first_seen != (observed_dates[0] if observed_dates else as_of_date)
                    or item.last_seen != (observed_dates[-1] if observed_dates else as_of_date)
                    or item.persistence_days != consecutive
                    or item.event_count_window != window_count
                    or item.state != next_state
                    or item.materiality_pct != materiality_pct
                    or item.counts_toward_pressure != (sustained and materiality_pct > 0)
                    or list(item.source_event_ids or []) != source_event_ids
                )
                if not changed:
                    total += 1
                    continue
                previous_state = item.state
                if observed_dates:
                    item.first_seen = observed_dates[0]
                    item.last_seen = observed_dates[-1]
                item.persistence_days = consecutive
                item.event_count_window = window_count
                item.state = next_state
                item.materiality_pct = materiality_pct
                item.decay_factor = Decimal("1")
                item.counts_toward_pressure = sustained and materiality_pct > 0
                item.source_event_ids = source_event_ids
                item.last_scored_at = self.clock.now()
                item.updated_at = item.last_scored_at
                item.updated_by_id = self.system_actor_id
                item.request_id = context.request_id
                item.version += 1
                if previous_state != next_state:
                    session.add(
                        EvidenceTransition(
                            id=new_id(),
                            evidence_id=item.id,
                            from_state=previous_state,
                            to_state=next_state,
                            occurred_on=as_of_date,
                            rule=(
                                "T3.sustained_days_or_events"
                                if next_state == "sustained"
                                else "T3.persistence_not_met"
                            ),
                            threshold_snapshot_id=self.threshold_store.snapshot_id(),
                            created_at=self.clock.now(),
                            updated_at=self.clock.now(),
                            created_by_id=self.system_actor_id,
                            updated_by_id=self.system_actor_id,
                            request_id=context.request_id,
                        )
                    )
                total += 1
            # LedgerService records a stage-3 trace while deriving its
            # identity rows.  The persistence/materiality pass above then
            # enriches those rows with the complete observed window.  Append
            # the corrected trace so Why? shows the exact values that drive
            # forecast pressure, while retaining the original derivation in
            # the immutable trace history.
            trace_items = tuple(
                EvidenceFacts.from_item(item)
                for item in evidence.for_borrower(
                    current_borrower_id,
                    scope=scope,
                    include_superseded=True,
                )
            )
            TraceRepository(
                session,
                clock=self.clock,
                request_id=context.request_id,
            ).write(
                ("borrower", current_borrower_id),
                _stage3_trace(
                    trace_items,
                    ledger_revision.supersessions,
                    as_of_date,
                    self.threshold_store,
                ),
                actor_id=self.system_actor_id,
                request_id=context.request_id,
                occurred_at=self.clock.now(),
            )
        session.flush()
        return total

    # -- rank ---------------------------------------------------------------

    def _run_rank(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            as_of_date = self._resolve_as_of(context.as_of)
            borrower_id = _parse_uuid(context.borrower_id)
            forecast_run = self._existing_forecast_run(session, context.run_id, STEP_SCORE)
            if forecast_run is None:
                session.commit()
                return {"ranked": 0, "note": "no forecast run to rank"}
            # A run whose scoring did not finish has an unknown number of
            # forecasts still to come.  Ranking it would publish a partial
            # book as the day's queue, so the step fails here instead and the
            # pipeline halts with the prior day's run still serving
            # (`spec §R-28.a`).
            if forecast_run.state != COMPLETE_FORECAST_RUN_STATE:
                raise RuntimeError(
                    f"Forecast run {forecast_run.id} is {forecast_run.state!r}, not "
                    f"{COMPLETE_FORECAST_RUN_STATE!r}; ranking an unfinished run would "
                    "publish a partial queue."
                )

            existing_entries = self._entries_for_run(session, forecast_run.id)
            if existing_entries:
                session.commit()
                return {"ranked": len(existing_entries), "resumed": True}

            borrower_ids = self._borrowers_with_forecasts(session, forecast_run.id, borrower_id)
            thresholds = TriageThresholds.from_store(self.threshold_store)
            # `spec §R-28.b`: one borrower's own bad exposure or forecast
            # data (a negative outstanding balance, a malformed fact) is
            # isolated here — excluded from tonight's ranking and recorded
            # — rather than aborting ranking for the whole book.
            tracker = IsolationTracker()
            triage_inputs: list[TriageInput] = []
            for b_id in borrower_ids:
                borrower = session.get(Borrower, b_id)
                if borrower is None:
                    continue
                try:
                    triage_inputs.append(
                        TriageInput(
                            borrower_id=borrower.id,
                            reference=borrower.reference,
                            exposure=self._borrower_exposure(session, borrower.id, as_of_date),
                            forecasts=self._forecast_facts(session, forecast_run.id, borrower.id),
                            sma_band=self._borrower_sma_band(
                                session, borrower.id, as_of_date
                            ),
                        )
                    )
                except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
                    tracker.record_failure(borrower.id, error)
                    continue
                tracker.record_success()
            ranked = rank_triage_entries(triage_inputs, thresholds)
            # A scored book that ranks to nothing is never a legitimate empty
            # day: the queue reads the newest complete run, so committing zero
            # entries here would blank the portfolio screen.  Fail the step and
            # leave the previous run in place instead.
            if borrower_ids and not ranked:
                raise RuntimeError(
                    f"Ranking produced no triage entries for forecast run {forecast_run.id} "
                    f"from {len(borrower_ids)} scored borrowers; refusing to publish an "
                    "empty queue."
                )

            now = self.clock.now()
            for entry in ranked:
                session.add(
                    TriageEntryModel(
                        id=new_id(),
                        run_id=forecast_run.id,
                        borrower_id=entry.borrower_id,
                        worst_covenant_version_id=entry.worst_covenant_version_id,
                        worst_horizon=entry.worst_horizon,
                        probability=entry.probability,
                        confidence=entry.confidence,
                        exposure=entry.exposure,
                        urgency=entry.urgency,
                        band=entry.band,
                        sma_band=entry.sma_band,
                        rank=entry.rank,
                        created_at=now,
                        updated_at=now,
                        created_by_id=self.system_actor_id,
                        updated_by_id=self.system_actor_id,
                        request_id=context.request_id,
                    )
                )
            session.commit()
            report = tracker.report()
            return {"ranked": len(ranked), "resumed": False, **report.as_metrics()}
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _borrower_sma_band(
        self, session: Session, borrower_id: UUID, as_of_date: date
    ) -> str | None:
        """Return a complete current-day SMA derivation for queue ranking.

        ``SmaBand.NONE`` is a real value only when every effective facility
        has a conduct row with days-past-due.  Any absent row/value leaves the
        queue field unknown instead of making missing data look healthy.
        """

        facility_ids = tuple(
            session.scalars(
                select(Facility.id)
                .where(
                    Facility.borrower_id == borrower_id,
                    Facility.effective_from <= as_of_date,
                    or_(Facility.effective_to.is_(None), Facility.effective_to >= as_of_date),
                )
                .order_by(Facility.id)
            ).all()
        )
        if not facility_ids:
            return None
        conduct = tuple(
            session.scalars(
                select(FacilityConduct).where(
                    FacilityConduct.facility_id.in_(facility_ids),
                    FacilityConduct.as_of_date == as_of_date,
                )
            ).all()
        )
        derivation = derive_borrower_sma(
            conduct,
            borrower_id=borrower_id,
            as_of_date=as_of_date,
            facility_ids=facility_ids,
        )
        if derivation.reason is not None:
            return None
        return derivation.band.value

    # -- update cases -------------------------------------------------------

    def _run_update_cases(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            forecast_run = self._existing_forecast_run(session, context.run_id, STEP_SCORE)
            if forecast_run is None:
                session.commit()
                return {"opened": 0}

            act_entries = (
                session.execute(
                    select(TriageEntryModel).where(
                        TriageEntryModel.run_id == forecast_run.id,
                        TriageEntryModel.band == ACT_BAND,
                    )
                )
                .scalars()
                .all()
            )
            now = self.clock.now()
            opened = 0
            already_open = 0
            tracker = IsolationTracker()
            for entry in act_entries:
                open_case = (
                    session.execute(
                        select(Case)
                        .where(Case.borrower_id == entry.borrower_id, Case.state != "closed")
                        .order_by(Case.created_at.desc())
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                if open_case is not None:
                    already_open += 1
                    continue
                # Closed cases still hold their reference, so the next case
                # for this borrower takes the next ordinal rather than
                # colliding with the UNIQUE constraint on `case.reference`.
                case_sequence = (
                    session.scalar(
                        select(func.count())
                        .select_from(Case)
                        .where(Case.borrower_id == entry.borrower_id)
                    )
                    or 0
                ) + 1
                # `spec §R-28.b`: one borrower's case failing to open (a
                # constraint violation on its own row) is isolated so the
                # rest of tonight's act band still gets a case.
                try:
                    with session.begin_nested():
                        session.add(
                            Case(
                                id=new_id(),
                                reference=_case_reference(entry.borrower_id, case_sequence),
                                borrower_id=entry.borrower_id,
                                opened_from_run_id=forecast_run.id,
                                state="open",
                                band_at_open=entry.band,
                                assignee_id=self.default_assignee_id,
                                created_at=now,
                                updated_at=now,
                                created_by_id=self.system_actor_id,
                                updated_by_id=self.system_actor_id,
                                request_id=context.request_id,
                            )
                        )
                        session.flush()
                except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
                    tracker.record_failure(entry.borrower_id, error)
                    continue
                tracker.record_success()
                opened += 1
            session.commit()
            report = tracker.report()
            return {"opened": opened, "already_open": already_open, **report.as_metrics()}
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- dispatch -------------------------------------------------------------

    def _run_dispatch(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            forecast_run = self._existing_forecast_run(session, context.run_id, STEP_SCORE)
            if forecast_run is None:
                session.commit()
                return {"dispatched": 0}

            new_cases = (
                session.execute(select(Case).where(Case.opened_from_run_id == forecast_run.id))
                .scalars()
                .all()
            )
            now = self.clock.now()
            # The band_change template requires the borrower's own reference,
            # not the case reference, so resolve them once for the batch.
            borrower_references = {
                borrower_id: reference
                for borrower_id, reference in session.execute(
                    select(Borrower.id, Borrower.reference).where(
                        Borrower.id.in_({case.borrower_id for case in new_cases})
                    )
                ).all()
            }
            dispatched = 0
            skipped_unassigned = 0
            already_notified = 0
            tracker = IsolationTracker()
            for case in new_cases:
                if case.assignee_id is None:
                    skipped_unassigned += 1
                    continue
                existing = session.execute(
                    select(Notification.id).where(
                        Notification.subject_type == "case",
                        Notification.subject_id == case.id,
                        Notification.template == _ACT_ALERT_TEMPLATE,
                    )
                ).scalar()
                if existing is not None:
                    already_notified += 1
                    continue
                # `spec §R-28.b`: one case's notification failing to send
                # (a bad recipient, a constraint violation) is isolated so
                # the rest of tonight's act band still gets notified.
                try:
                    with session.begin_nested():
                        session.add(
                            Notification(
                                id=new_id(),
                                recipient_id=case.assignee_id,
                                channel=_NOTIFICATION_CHANNEL,
                                template=_ACT_ALERT_TEMPLATE,
                                subject_type="case",
                                subject_id=case.id,
                                payload={
                                    "borrower_reference": borrower_references.get(
                                        case.borrower_id, case.reference
                                    ),
                                    "summary": (
                                        f"Moved into the {case.band_at_open} band; "
                                        f"case {case.reference} is open for review."
                                    ),
                                    "details": f"Case {case.reference}",
                                    "case_reference": case.reference,
                                },
                                state="pending",
                                created_at=now,
                                updated_at=now,
                                created_by_id=self.system_actor_id,
                                updated_by_id=self.system_actor_id,
                                request_id=context.request_id,
                            )
                        )
                        session.flush()
                except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
                    tracker.record_failure(case.id, error)
                    continue
                tracker.record_success()
                dispatched += 1
            session.commit()
            report = tracker.report()
            return {
                "dispatched": dispatched,
                "skipped_unassigned": skipped_unassigned,
                "already_notified": already_notified,
                **report.as_metrics(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- shared queries and adapters -----------------------------------------

    def _latest_step_states(self, session: Session, run_id: str) -> Mapping[str, str]:
        """The latest attempt's recorded state for each of `PIPELINE_STEPS`
        under one pipeline run id. A step never attempted for this run is
        simply absent, distinguishing "not reached yet" from any recorded
        state (`policy.PipelineRunStatus` treats both as incomplete)."""

        rows = session.execute(
            select(JobRun.job_name, JobRun.attempt, JobRun.state).where(
                JobRun.run_id == run_id, JobRun.job_name.in_(PIPELINE_STEPS)
            )
        ).all()
        latest: dict[str, tuple[int, str]] = {}
        for job_name, attempt, state in rows:
            current = latest.get(job_name)
            if current is None or attempt > current[0]:
                latest[job_name] = (attempt, state)
        return {name: state for name, (_attempt, state) in latest.items()}

    def _recent_step_outcomes(self, session: Session, job_name: str, *, limit: int) -> list[JobRun]:
        """The most recent `limit` pipeline runs' *latest* attempt of
        `job_name`, newest first — exactly one row per run id, regardless
        of how many times that run id's attempt was retried."""

        latest_attempt = (
            select(JobRun.run_id, func.max(JobRun.attempt).label("attempt"))
            .where(JobRun.job_name == job_name)
            .group_by(JobRun.run_id)
            .subquery()
        )
        statement = (
            select(JobRun)
            .join(
                latest_attempt,
                (JobRun.run_id == latest_attempt.c.run_id)
                & (JobRun.attempt == latest_attempt.c.attempt),
            )
            .where(JobRun.job_name == job_name)
            .order_by(JobRun.started_at.desc())
            .limit(limit)
        )
        return list(session.execute(statement).scalars().all())

    def _live_covenant_versions(
        self,
        session: Session,
        scope: Scope,
        as_of_date: date,
        borrower_id: UUID | None,
    ) -> list[tuple[CovenantVersion, Covenant]]:
        statement = (
            select(CovenantVersion, Covenant, Borrower.id)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .join(Facility, Facility.id == Covenant.facility_id)
            .join(Borrower, Borrower.id == Facility.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(
                scope.predicate(Portfolio.path),
                CovenantVersion.status == "live",
                Covenant.is_active.is_(True),
                CovenantVersion.effective_from <= as_of_date,
                or_(
                    CovenantVersion.effective_to.is_(None),
                    CovenantVersion.effective_to > as_of_date,
                ),
            )
            .order_by(CovenantVersion.id)
        )
        if borrower_id is not None:
            statement = statement.where(Borrower.id == borrower_id)
        rows = session.execute(statement).all()
        return [(version, covenant) for version, covenant, _borrower_id in rows]

    def _already_tested(
        self, session: Session, covenant_version_id: UUID, as_of_date: date
    ) -> bool:
        statement = (
            select(CovenantTest.id)
            .where(
                CovenantTest.covenant_version_id == covenant_version_id,
                CovenantTest.as_of_date == as_of_date,
            )
            .limit(1)
        )
        if session.execute(statement).scalar() is None:
            return False
        # A statement/restatement queues a fresh ``due`` schedule alongside
        # the historical same-day test.  That explicit trigger must win over
        # this optimization or newly accepted financials never reach a live
        # forecast until tomorrow.
        pending = session.execute(
            select(CovenantSchedule.id)
            .where(
                CovenantSchedule.covenant_version_id == covenant_version_id,
                CovenantSchedule.due_date == as_of_date,
                CovenantSchedule.state == ScheduleState.DUE.value,
            )
            .limit(1)
        ).scalar()
        return pending is None

    def _test_history(
        self, session: Session, covenant_version_id: UUID, as_of_date: date
    ) -> list[CovenantTest]:
        statement = (
            select(CovenantTest)
            .where(
                CovenantTest.covenant_version_id == covenant_version_id,
                CovenantTest.as_of_date <= as_of_date,
            )
            .order_by(CovenantTest.as_of_date.asc())
            .limit(_TEST_HISTORY_LIMIT)
        )
        return list(session.execute(statement).scalars().all())

    def _forecast_candidates(
        self,
        session: Session,
        scope: Scope,
        as_of_date: date,
        borrower_id: UUID | None,
    ) -> list[ForecastCandidate]:
        candidates: list[ForecastCandidate] = []
        due = self._live_covenant_versions(session, scope, as_of_date, borrower_id)
        for version, _covenant in due:
            history = [
                row
                for row in self._test_history(session, version.id, as_of_date)
                if row.value is not None
            ]
            if not history:
                continue
            latest = history[-1]
            series = [Observation(date=row.as_of_date, value=row.value) for row in history]
            evidence_items = self._evidence_for_covenant(session, version, scope)
            candidates.append(
                ForecastCandidate(
                    covenant_version_id=version.id,
                    threshold=version.threshold,
                    direction=version.direction,
                    series=series,
                    data_as_of=latest.as_of_date,
                    computable=True,
                    already_breached=latest.verdict in _BREACH_VERDICTS,
                    pressure=evidence_pressure(evidence_items, version.direction),
                    formula_inputs={
                        "signal_families": [item.family for item in evidence_items],
                        "evidence_ids": [str(item.id) for item in evidence_items],
                    },
                )
            )
        return candidates

    def _evidence_for_covenant(
        self,
        session: Session,
        version: CovenantVersion,
        scope: Scope,
    ) -> tuple[EvidenceItem, ...]:
        """Return scoped borrower evidence for the forecast pressure terms."""
        borrower_id = session.scalar(
            select(Borrower.id)
            .join(Facility, Facility.borrower_id == Borrower.id)
            .join(Covenant, Covenant.facility_id == Facility.id)
            .where(Covenant.id == version.covenant_id)
            .limit(1)
        )
        if borrower_id is None:
            return ()
        return tuple(
            EvidenceRepository(session).for_borrower(
                borrower_id, scope=scope, include_superseded=False
            )
        )

    def _current_job_run(self, session: Session, run_id: str, job_name: str) -> JobRun:
        statement = (
            select(JobRun)
            .where(JobRun.run_id == run_id, JobRun.job_name == job_name, JobRun.state == "running")
            .order_by(JobRun.attempt.desc())
            .limit(1)
        )
        row = session.execute(statement).scalars().first()
        if row is None:
            raise RuntimeError(
                f"No running job_run row for job_name={job_name!r}, run_id={run_id!r}; "
                "this handler must run inside JobRunner."
            )
        return row

    def _existing_forecast_run(
        self, session: Session, run_id: str, job_name: str
    ) -> ForecastRun | None:
        job_run_ids = select(JobRun.id).where(JobRun.run_id == run_id, JobRun.job_name == job_name)
        statement = (
            select(ForecastRun)
            .where(ForecastRun.job_run_id.in_(job_run_ids))
            .order_by(ForecastRun.created_at.desc())
            .limit(1)
        )
        return session.execute(statement).scalars().first()

    def _entries_for_run(self, session: Session, run_id: UUID) -> list[TriageEntryModel]:
        statement = select(TriageEntryModel).where(TriageEntryModel.run_id == run_id)
        return list(session.execute(statement).scalars().all())

    def _borrowers_with_forecasts(
        self, session: Session, forecast_run_id: UUID, borrower_id: UUID | None
    ) -> list[UUID]:
        statement = (
            select(Borrower.id)
            .distinct()
            .join(Facility, Facility.borrower_id == Borrower.id)
            .join(Covenant, Covenant.facility_id == Facility.id)
            .join(CovenantVersion, CovenantVersion.covenant_id == Covenant.id)
            .join(Forecast, Forecast.covenant_version_id == CovenantVersion.id)
            .where(Forecast.run_id == forecast_run_id)
        )
        if borrower_id is not None:
            statement = statement.where(Borrower.id == borrower_id)
        return list(session.execute(statement).scalars().all())

    def _forecast_facts(
        self, session: Session, forecast_run_id: UUID, borrower_id: UUID
    ) -> list[ForecastFact]:
        statement = (
            select(Forecast)
            .join(CovenantVersion, CovenantVersion.id == Forecast.covenant_version_id)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .join(Facility, Facility.id == Covenant.facility_id)
            .where(Forecast.run_id == forecast_run_id, Facility.borrower_id == borrower_id)
        )
        rows = session.execute(statement).scalars().all()
        return [
            ForecastFact(
                covenant_version_id=row.covenant_version_id,
                horizon_days=row.horizon_days,
                probability=row.probability,
                confidence=row.confidence,
                below_confidence_floor=row.below_confidence_floor,
                suppressed=row.below_confidence_floor,
            )
            for row in rows
        ]

    def _borrower_exposure(
        self, session: Session, borrower_id: UUID, as_of_date: date
    ) -> Decimal | None:
        statement = select(Facility.sanctioned_limit, Facility.outstanding).where(
            Facility.borrower_id == borrower_id,
            Facility.effective_from <= as_of_date,
            or_(Facility.effective_to.is_(None), Facility.effective_to > as_of_date),
        )
        total = Decimal("0")
        found = False
        for sanctioned_limit, outstanding in session.execute(statement).all():
            found = True
            total += outstanding if outstanding is not None else sanctioned_limit
        # `Facility` money is held in ₹ crore — the ratio library declares
        # `unit="₹ crore"` on every absolute-amount covenant and bands
        # `drawing_power_headroom` accordingly.  `TriageEntry.exposure` is
        # rupees, which is the unit the queue and the case file format and
        # `tests/integration/test_case_file.py` pins.  This is the one hop
        # between the two, and it was carrying the number across unchanged:
        # a ₹636 crore book reached the queue as the figure 636.30 and
        # rendered as "₹636.30".
        return total * _RUPEES_PER_CRORE if found else None

    def _scope_for(self, session: Session, borrower_id: UUID | None) -> Scope:
        if borrower_id is None:
            return self._full_book_scope(session)
        borrower = session.get(Borrower, borrower_id)
        if borrower is None:
            raise NotFound(f"Borrower {borrower_id} was not found.")
        portfolio = session.get(Portfolio, borrower.portfolio_id)
        if portfolio is None:  # pragma: no cover - referential integrity guarantees this
            raise NotFound(f"Portfolio {borrower.portfolio_id} was not found.")
        return Scope(principal_id=self.system_actor_id, exact_paths=(portfolio.path,))

    def _full_book_scope(self, session: Session) -> Scope:
        statement = select(Portfolio.path).where(Portfolio.parent_id.is_(None))
        roots = session.execute(statement).scalars().all()
        return Scope(principal_id=self.system_actor_id, descendant_paths=tuple(roots))

    def _system_principal(self) -> Principal:
        return Principal(
            id=self.system_actor_id,
            permissions=frozenset({Permission.VIEW_COVENANT, Permission.INGEST_DATA}),
            kind=PrincipalKind.USER,
        )

    def _audit(self, session: Session, request_id: str) -> _AuditWriterAdapter:
        recorder = AuditRecorder(
            SqlAlchemyAuditStore(session), clock=self.clock, request_id=request_id
        )
        return _AuditWriterAdapter(recorder)

    def _resolve_as_of(self, as_of: str | None) -> date:
        if as_of is None:
            return self.clock.now().date()
        return date.fromisoformat(as_of)


def _parse_uuid(value: str | UUID | None) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    return UUID(value)


def _case_reference(borrower_id: UUID, sequence: int = 1) -> str:
    """Build the human-readable case reference for one borrower's Nth case.

    ``case.reference`` is UNIQUE, so a reference derived from the borrower
    alone caps a borrower at exactly one case for the lifetime of the
    database: once that case closes, a borrower who deteriorates again can
    never be raised a second time.  The first case keeps the original
    unsuffixed reference so existing rows and links stay valid, and each
    re-raise appends its ordinal.
    """

    base = f"C-{borrower_id.hex[:12].upper()}"
    return base if sequence <= 1 else f"{base}-{sequence}"


def _longest_consecutive_days(values: Sequence[date]) -> int:
    longest = 0
    current = 0
    previous: date | None = None
    for value in values:
        if previous is not None and value.toordinal() == previous.toordinal() + 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous = value
    return longest


def _signal_materiality_pct(family: str, events: Sequence[SignalEventModel]) -> Decimal:
    """Normalise raw signal magnitudes to the stored percentage-point score."""
    if not events:
        return Decimal("0")
    latest = max(events, key=lambda event: (event.event_date, str(event.id)))
    magnitude = latest.magnitude or Decimal("0")
    scale = {
        "payment": Decimal("2"),
        "utilisation": Decimal("1"),
        "account_activity": Decimal("1"),
        "treasury": Decimal("100"),
        "concentration": Decimal("1"),
        "industry": Decimal("100"),
        "news": Decimal("100"),
    }.get(family, Decimal("1"))
    value = abs(magnitude) * scale
    # A sustained adverse signal is material by construction, but remains
    # bounded so one malformed magnitude cannot dominate the portfolio.
    return min(Decimal("100"), max(Decimal("5"), value))


__all__ = [
    "NightlyPipelineService",
    "SignalSourceProvider",
    "StatementLinesProvider",
]
