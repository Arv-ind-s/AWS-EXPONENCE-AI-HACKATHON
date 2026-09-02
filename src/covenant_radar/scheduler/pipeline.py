"""Nightly pipeline composition: ingest, test, score, rank, update cases and
dispatch, wired as separate `T-120` jobs that share one pipeline run id
(`plan.md §5.9`, `spec §R-28.a`/`R-28.c`).

**Why six jobs, not eight.** `spec §R-28.a`'s prose names eight conceptual
stages — ingest, test, score, forecast, attribute, rank, update cases,
dispatch. `ForecastScoringService` (`T-059`) already computes the forecast
projection and persists driver attribution atomically and resumably, per
covenant, inside one call: splitting "score", "forecast" and "attribute" into
separate retryable jobs would either recompute the same work three times or
register a job with no independent effect of its own — either way a fabricated
step, not a real one. This module therefore composes six real, independently
retryable units: `nightly.ingest`, `nightly.test`, `nightly.score` (which
covers scoring, forecasting and attribution together), `nightly.rank`,
`nightly.update_cases` and `nightly.dispatch`.

**Composition, not business logic.** This module knows the step order and how
to drive `scheduler.runner.JobRunner` through it; it does not know how any
step does its work. `services.nightly.NightlyPipelineService` owns that, and
supplies the `JobHandler` this module registers under each step's name — the
same split `scheduler.jobs`/`scheduler.runner` (policy/execution) already draw
against `services.*` (business logic).

**Idempotency by run id.** Every step handler is idempotent for a given
pipeline run id: rerunning `nightly.test` for the same run either resumes
committed work or is a no-op if that step already finished. This module's own
job is only to *pass the same run id to every step of one pipeline run* and to
halt the sequence at the first step that does not succeed, leaving every later
step — including `nightly.dispatch` — untouched, so the prior day's data keeps
serving the queue (`spec §R-28.a`'s "the run halted at that step ... the prior
day's results still serving the queue").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import select

from covenant_radar.core.context import new_job_run_id
from covenant_radar.db.models.operations import JobRun
from covenant_radar.scheduler.jobs import (
    InterruptionPolicy,
    JobDefinition,
    JobHandler,
    JobPolicy,
    JobRegistry,
    JobRunContext,
    RetryPolicy,
)
from covenant_radar.scheduler.ledger import RUNNING
from covenant_radar.scheduler.runner import JobRunner

STEP_INGEST: Final[str] = "nightly.ingest"
STEP_TEST: Final[str] = "nightly.test"
STEP_SCORE: Final[str] = "nightly.score"
STEP_RANK: Final[str] = "nightly.rank"
STEP_UPDATE_CASES: Final[str] = "nightly.update_cases"
STEP_DISPATCH: Final[str] = "nightly.dispatch"

#: The pipeline's fixed order (`spec §R-28.a`): each step commits before the
#: next one starts, so a halted run never presents a later step's output
#: without its predecessors having actually succeeded.
PIPELINE_STEPS: Final[tuple[str, ...]] = (
    STEP_INGEST,
    STEP_TEST,
    STEP_SCORE,
    STEP_RANK,
    STEP_UPDATE_CASES,
    STEP_DISPATCH,
)

#: The composite, schedulable entry point built by `pipeline_job`. Kept
#: distinct from the six step names so `radarctl job run nightly.test`
#: unambiguously retries one step while `radarctl job run nightly.pipeline`
#: (or a cron tick) runs the whole ordered sequence.
PIPELINE_JOB_NAME: Final[str] = "nightly.pipeline"

_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 1800.0


def default_step_policy(
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = 0.0,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> JobPolicy:
    """The policy every pipeline step registers with, absent an override.

    `InterruptionPolicy.RESUME`: a hard restart continues the same logical
    run under the same run id, so a step-idempotent handler (this module's
    whole point) is asked to finish what it started rather than redo it.
    """

    return JobPolicy(
        retry=RetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff_seconds),
        interruption=InterruptionPolicy.RESUME,
        timeout_seconds=timeout_seconds,
    )


def register_nightly_pipeline(
    registry: JobRegistry,
    handlers: Mapping[str, JobHandler],
    *,
    policy: JobPolicy | None = None,
) -> tuple[JobDefinition, ...]:
    """Register the six pipeline steps against `registry`, in order.

    `handlers` must supply exactly the six names in `PIPELINE_STEPS`,
    typically `NightlyPipelineService(...).handlers()`. None of the six get a
    cron `schedule` of their own — they are driven only by `run_nightly_pipeline`
    (or an operator's `radarctl job run <step> --run-id ...`), never fired
    independently, since a step run outside its pipeline's shared run id
    would not be resuming anything.
    """

    missing = tuple(step for step in PIPELINE_STEPS if step not in handlers)
    if missing:
        raise ValueError(f"register_nightly_pipeline is missing handlers for: {missing!r}.")
    resolved_policy = policy or default_step_policy()
    registered: list[JobDefinition] = []
    for step in PIPELINE_STEPS:
        definition = JobDefinition(name=step, handler=handlers[step], policy=resolved_policy)
        registered.append(registry.register(definition))
    return tuple(registered)


def pipeline_job(
    runner: JobRunner,
    *,
    schedule: str | None = None,
    policy: JobPolicy | None = None,
) -> JobDefinition:
    """Build the one schedulable job that drives the whole ordered sequence.

    Registering this (in addition to `register_nightly_pipeline`'s six steps)
    is what lets `scheduler.runner.Scheduler.schedule` fire the batch on a
    cron tick: the composite job's own run id is the pipeline run id every
    step below it shares.
    """

    def handler(context: JobRunContext) -> Mapping[str, object]:
        result = run_nightly_pipeline(
            runner,
            trigger=context.trigger,
            run_id=context.run_id,
            borrower_id=context.borrower_id,
            as_of=context.as_of,
        )
        if not result.success:
            failed = result.failed_step or "unknown"
            completed = ", ".join(result.completed_steps) or "none"
            raise RuntimeError(
                f"Nightly pipeline halted at {failed!r}; completed steps: {completed}."
            )
        return {
            "success": result.success,
            "completed_steps": list(result.completed_steps),
            "failed_step": result.failed_step,
        }

    return JobDefinition(
        name=PIPELINE_JOB_NAME,
        handler=handler,
        policy=policy or default_step_policy(),
        schedule=schedule,
    )


class PipelineAlreadyRunningError(RuntimeError):
    """Another pipeline run holds the steps this run needs."""

    def __init__(self, job_name: str, run_id: str) -> None:
        super().__init__(
            f"Nightly pipeline step {job_name!r} is already running under run id {run_id!r}; "
            "a second overlapping pipeline run would rank one run's forecasts against "
            "another's."
        )
        self.job_name = job_name
        self.run_id = run_id


def _refuse_overlapping_pipeline(runner: JobRunner, run_id: str) -> None:
    """Refuse to start while a *different* pipeline run holds any step.

    `JobLedger.start_or_refuse` already serialises attempts of one job name,
    but the six step names are six different jobs: without this check a cron
    tick's `nightly.rank` can run against a manually triggered run's
    half-written `nightly.score`, ranking a forecast run that is still being
    written.  The steps' own guards remain the authority — this is the cheap
    check that stops the overlap before any work is done.
    """

    session = runner.session_factory()
    try:
        conflicting = session.execute(
            select(JobRun.job_name, JobRun.run_id)
            .where(
                JobRun.job_name.in_((*PIPELINE_STEPS, PIPELINE_JOB_NAME)),
                JobRun.state == RUNNING,
                JobRun.run_id != run_id,
            )
            .limit(1)
        ).first()
    finally:
        session.close()
    if conflicting is not None:
        raise PipelineAlreadyRunningError(str(conflicting[0]), str(conflicting[1]))


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    """What one call to `run_nightly_pipeline` did, in order."""

    run_id: str
    borrower_id: str | None
    completed_steps: tuple[str, ...]
    failed_step: str | None
    runs: tuple[JobRun, ...]

    @property
    def success(self) -> bool:
        """Whether every step in `PIPELINE_STEPS` succeeded — the only
        condition under which the run is considered complete
        (`spec §R-28.a`'s "marked complete only when every step has
        succeeded")."""

        return self.failed_step is None and len(self.completed_steps) == len(PIPELINE_STEPS)


def run_nightly_pipeline(
    runner: JobRunner,
    *,
    trigger: str,
    run_id: str | None = None,
    borrower_id: str | UUID | None = None,
    as_of: str | None = None,
    actor_id: UUID | None = None,
    steps: tuple[str, ...] = PIPELINE_STEPS,
) -> PipelineRunResult:
    """Run every pipeline step, in order, under one shared run id.

    Halts at the first step whose `JobRun` does not finish `succeeded` —
    a failure, an exhausted retry, or a manual-trigger refusal all stop the
    sequence in the same way, leaving every step from there on untouched.
    Calling this again with the same `run_id` resumes: each step's own
    handler decides, from persisted state, what (if anything) is left to do.
    """

    resolved_run_id = run_id or new_job_run_id()
    resolved_borrower_id = str(borrower_id) if borrower_id is not None else None
    completed: list[str] = []
    runs: list[JobRun] = []
    failed_step: str | None = None

    if not steps or any(step not in PIPELINE_STEPS for step in steps):
        raise ValueError("steps must be a non-empty subset of PIPELINE_STEPS.")
    _refuse_overlapping_pipeline(runner, resolved_run_id)
    for step in steps:
        run = runner.run_now(
            step,
            trigger=trigger,
            run_id=resolved_run_id,
            borrower_id=resolved_borrower_id,
            as_of=as_of,
            actor_id=actor_id,
        )
        runs.append(run)
        if run.state != "succeeded":
            failed_step = step
            break
        completed.append(step)

    return PipelineRunResult(
        run_id=resolved_run_id,
        borrower_id=resolved_borrower_id,
        completed_steps=tuple(completed),
        failed_step=failed_step,
        runs=tuple(runs),
    )


__all__ = [
    "PIPELINE_JOB_NAME",
    "PIPELINE_STEPS",
    "STEP_DISPATCH",
    "STEP_INGEST",
    "STEP_RANK",
    "STEP_SCORE",
    "STEP_TEST",
    "STEP_UPDATE_CASES",
    "PipelineAlreadyRunningError",
    "PipelineRunResult",
    "default_step_policy",
    "pipeline_job",
    "register_nightly_pipeline",
    "run_nightly_pipeline",
]
