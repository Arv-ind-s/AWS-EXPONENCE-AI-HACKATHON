"""The job registry: declared schedule, timeout, retry and interruption
policy for every scheduled or manually triggered job (`plan.md §5.9`,
`spec §R-28`).

A job registered with no declared retry, interruption or timeout policy is
refused outright, here at registration — an undeclared failure policy is a
decision made at 3 a.m. by nobody, and this module never lets one through
by omission.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from covenant_radar.core.errors import NotFound, ValidationError

_MAX_NAME_LENGTH: Final[int] = 100


class InterruptionPolicy(StrEnum):
    """How a job left `running` by a hard restart is resolved.

    `RESUME` continues the same logical run — a fresh attempt is started
    under the same `run_id`, so a step-idempotent job (`T-121`) does not
    redo committed work. `RESTART` abandons that logical run outright and
    begins a brand new one with a fresh `run_id`.
    """

    RESUME = "resume"
    RESTART = "restart"


@dataclass(frozen=True, slots=True)
class JobRunContext:
    """The arguments one job attempt's handler receives."""

    run_id: str
    attempt: int
    trigger: str
    request_id: str
    as_of: str | None = None
    borrower_id: str | None = None


JobHandler = Callable[[JobRunContext], Mapping[str, object] | None]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many attempts a failing job gets, and how long to wait between them."""

    max_attempts: int
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts must be at least 1.")
        if self.backoff_seconds < 0:
            raise ValueError("RetryPolicy.backoff_seconds cannot be negative.")


@dataclass(frozen=True, slots=True)
class JobPolicy:
    """The retry, interruption and timeout policy every registered job
    must declare (`spec §R-28`'s "no undeclared failure policy")."""

    retry: RetryPolicy
    interruption: InterruptionPolicy
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.retry, RetryPolicy):
            raise TypeError("JobPolicy.retry must be a RetryPolicy.")
        if not isinstance(self.interruption, InterruptionPolicy):
            raise TypeError("JobPolicy.interruption must be an InterruptionPolicy.")
        if self.timeout_seconds <= 0:
            raise ValueError("JobPolicy.timeout_seconds must be positive.")


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """One registrable job: its name, optional schedule, handler and policy.

    `policy` defaults to `None` so a caller can construct an otherwise
    valid definition that is missing one — `JobRegistry.register` is the
    layer that refuses it, matching `spec §R-28`'s "refused at
    registration" rather than at construction.

    `schedule` is a five-field cron expression (``"0 1 * * *"``) consumed
    by `scheduler.runner.Scheduler`. `None` means the job only ever runs
    when triggered manually (`radarctl job run`).
    """

    name: str
    handler: JobHandler
    policy: JobPolicy | None = None
    schedule: str | None = None

    def __post_init__(self) -> None:
        if not callable(self.handler):
            raise TypeError("JobDefinition.handler must be callable.")


class JobRegistrationError(ValidationError):
    """Raised when a job definition cannot be registered as declared."""


class JobRegistry:
    """The set of jobs known to the scheduler, keyed by name."""

    def __init__(self) -> None:
        self._definitions: dict[str, JobDefinition] = {}

    def register(self, definition: JobDefinition) -> JobDefinition:
        """Add `definition`, refusing one with no declared policy or a
        name already registered."""
        if not isinstance(definition, JobDefinition):
            raise TypeError("JobRegistry.register requires a JobDefinition.")
        name = definition.name.strip() if isinstance(definition.name, str) else ""
        if not name or len(name) > _MAX_NAME_LENGTH:
            raise JobRegistrationError(
                f"Job name must be 1-{_MAX_NAME_LENGTH} characters.", field="name"
            )
        if definition.policy is None:
            raise JobRegistrationError(
                f"Job {name!r} declares no retry/interruption/timeout policy; "
                "refusing to register a job whose failure behaviour nobody decided.",
                field="policy",
            )
        if name in self._definitions:
            raise JobRegistrationError(f"Job {name!r} is already registered.", field="name")
        self._definitions[name] = definition
        return definition

    def get(self, name: str) -> JobDefinition:
        """The registered definition for `name`, or `NotFound`."""
        try:
            return self._definitions[name]
        except KeyError:
            raise NotFound(f"No job named {name!r} is registered.") from None

    def all(self) -> tuple[JobDefinition, ...]:
        """Every registered definition, in registration order."""
        return tuple(self._definitions.values())

    def names(self) -> tuple[str, ...]:
        """Every registered job name, in registration order."""
        return tuple(self._definitions)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._definitions


#: `T-134`'s schedulable name for the Board MIS generation-and-delivery job.
#: The handler itself (assembling the report, rendering it and delivering it
#: to a distribution list) lives in `reporting.mis`, which this module does
#: not import — this stays a thin, generic job-definition builder exactly
#: like every other job here, and the caller composing the runtime supplies
#: the concrete handler.
BOARD_MIS_REPORT_JOB_NAME: Final[str] = "reports.board_mis"

_DEFAULT_BOARD_MIS_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_BOARD_MIS_TIMEOUT_SECONDS: Final[float] = 900.0


def board_mis_report_job(
    handler: JobHandler,
    *,
    schedule: str | None = "0 6 1 * *",
    policy: JobPolicy | None = None,
) -> JobDefinition:
    """Build the schedulable Board MIS generation-and-delivery job.

    Defaults to firing once a month, at 06:00 UTC on the 1st — "the
    portfolio view a credit committee reads monthly" (`spec §R-31`). A
    failed attempt is retried a bounded number of times and then left
    `failed` (`InterruptionPolicy.RESTART`: a hard restart starts a fresh
    logical run rather than resuming a half-delivered one, since delivery
    is not safely re-orderable mid-recipient) — `job_run`'s own ledger is
    what the administrator operations screen surfaces a spent retry budget
    through, on top of `reporting.mis`'s own per-recipient dead-letter audit
    trail.
    """

    return JobDefinition(
        name=BOARD_MIS_REPORT_JOB_NAME,
        handler=handler,
        policy=policy
        or JobPolicy(
            retry=RetryPolicy(max_attempts=_DEFAULT_BOARD_MIS_MAX_ATTEMPTS, backoff_seconds=60.0),
            interruption=InterruptionPolicy.RESTART,
            timeout_seconds=_DEFAULT_BOARD_MIS_TIMEOUT_SECONDS,
        ),
        schedule=schedule,
    )


#: `T-150`'s schedulable name for the nightly data-integrity verification
#: job. Like `BOARD_MIS_REPORT_JOB_NAME` above, the handler (assembling and
#: running every check) lives in `services.integrity`, which this module
#: does not import — this stays a thin job-definition builder, and the
#: caller composing the runtime supplies the concrete handler.
INTEGRITY_CHECK_JOB_NAME: Final[str] = "integrity.checks"

_DEFAULT_INTEGRITY_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_INTEGRITY_BACKOFF_SECONDS: Final[float] = 60.0
_DEFAULT_INTEGRITY_TIMEOUT_SECONDS: Final[float] = 1_800.0


def integrity_check_job(
    handler: JobHandler,
    *,
    schedule: str | None = "30 2 * * *",
    policy: JobPolicy | None = None,
) -> JobDefinition:
    """Build the schedulable data-integrity verification job (`T-150`).

    Defaults to firing nightly at 02:30 UTC, after the batch jobs that write
    most of a day's rows. `InterruptionPolicy.RESUME` picks a hard-restart's
    logical run back up rather than starting over: the check's own per-check
    watermark (`services.integrity.IntegrityService`) already makes
    re-verifying already-clean data avoidable, so a restart should not force
    a full re-scan of the audit chain or the document store.
    """

    return JobDefinition(
        name=INTEGRITY_CHECK_JOB_NAME,
        handler=handler,
        policy=policy
        or JobPolicy(
            retry=RetryPolicy(
                max_attempts=_DEFAULT_INTEGRITY_MAX_ATTEMPTS,
                backoff_seconds=_DEFAULT_INTEGRITY_BACKOFF_SECONDS,
            ),
            interruption=InterruptionPolicy.RESUME,
            timeout_seconds=_DEFAULT_INTEGRITY_TIMEOUT_SECONDS,
        ),
        schedule=schedule,
    )


__all__ = [
    "BOARD_MIS_REPORT_JOB_NAME",
    "INTEGRITY_CHECK_JOB_NAME",
    "InterruptionPolicy",
    "JobDefinition",
    "JobHandler",
    "JobPolicy",
    "JobRegistrationError",
    "JobRegistry",
    "JobRunContext",
    "RetryPolicy",
    "board_mis_report_job",
    "integrity_check_job",
]
