"""Scheduled work that survives a restart, records what it did, and never
runs the same job twice concurrently (`T-120`, `plan.md §5.9`, `spec §R-28`).

`default_registry()` is the process-wide `JobRegistry` `radarctl job run`
triggers against; later tasks (the nightly pipeline, `T-121`) register
their jobs into it at start-up rather than each owning a private registry.
"""

from __future__ import annotations

from covenant_radar.scheduler.jobs import (
    InterruptionPolicy,
    JobDefinition,
    JobHandler,
    JobPolicy,
    JobRegistrationError,
    JobRegistry,
    JobRunContext,
    RetryPolicy,
)
from covenant_radar.scheduler.ledger import (
    ABANDONED,
    FAILED,
    INTERRUPTED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
    JobAlreadyRunningError,
    JobLedger,
)
from covenant_radar.scheduler.runner import (
    JobRunner,
    Scheduler,
    SchedulerShuttingDownError,
    ShutdownReport,
)

_default_registry = JobRegistry()


def default_registry() -> JobRegistry:
    """The process-wide job registry `radarctl job run` triggers against."""
    return _default_registry


__all__ = [
    "ABANDONED",
    "FAILED",
    "INTERRUPTED",
    "RUNNING",
    "SUCCEEDED",
    "TERMINAL_STATES",
    "InterruptionPolicy",
    "JobAlreadyRunningError",
    "JobDefinition",
    "JobHandler",
    "JobLedger",
    "JobPolicy",
    "JobRegistrationError",
    "JobRegistry",
    "JobRunContext",
    "JobRunner",
    "RetryPolicy",
    "Scheduler",
    "SchedulerShuttingDownError",
    "ShutdownReport",
    "default_registry",
]
