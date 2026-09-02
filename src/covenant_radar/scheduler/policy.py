"""Partial-failure policy: per-item isolation, T12 deadline evaluation and
recurring-failure escalation for the nightly batch (`T-122`, `spec §R-28.b`
and `R-28.d`).

This module is the pure decision layer `services.nightly.NightlyPipelineService`
calls into. It performs no I/O and knows nothing about SQLAlchemy, the job
ledger's tables or the threshold store's persistence — it only takes the
facts a caller has already read (a step's per-item outcomes, a pipeline run's
per-step states, a job's recent terminal outcomes) and returns a decision,
matching the split `domain.cases.sla` already draws between "the numbers"
(this module) and "where they come from" (the caller).

**Why isolation lives here, not in `scheduler.jobs`.** `jobs.JobPolicy`
governs one job *attempt* — retry, timeout, interruption. `IsolationTracker`
governs what happens *inside* one attempt, when that attempt processes many
independent items (borrowers, covenants) and one item's failure must not
undo the rest — a different axis entirely, and the one `spec §R-28.b`'s "a
per-borrower failure isolated so one bad borrower does not stop the book"
names.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from covenant_radar.scheduler.ledger import SUCCEEDED

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

#: T12's default: two consecutive nights of the same normalized failure is
#: what `spec §17.5`'s "a nightly failure that alerts identically every
#: night stops being read" is guarding against — the third identical night
#: is not more informative than the second, so escalation fires on the
#: second.
DEFAULT_RECURRING_FAILURE_THRESHOLD: Final[int] = 2

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"\d+")


# -- per-item isolation -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemFailure:
    """One isolated item's failure, kept apart from the items that succeeded."""

    item_id: str
    error: str


@dataclass(frozen=True, slots=True)
class IsolationReport:
    """What a step did across every item it attempted."""

    attempted: int
    succeeded: int
    failures: tuple[ItemFailure, ...]

    def __post_init__(self) -> None:
        if self.attempted < 0 or self.succeeded < 0:
            raise ValueError("IsolationReport counts cannot be negative.")
        if self.succeeded + len(self.failures) != self.attempted:
            raise ValueError(
                "IsolationReport.attempted must equal succeeded plus the number of failures."
            )

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def all_succeeded(self) -> bool:
        return self.failed == 0

    def as_metrics(self) -> Mapping[str, object]:
        """The run-report shape: surfaced in `JobRun.metrics` and, through
        the existing admin operations read model, on the admin screen."""

        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "failures": [
                {"item_id": failure.item_id, "error": failure.error} for failure in self.failures
            ],
        }


@dataclass(slots=True)
class IsolationTracker:
    """Accumulates one step's per-item outcomes as the caller's own loop
    isolates each item in its own savepoint and reports success or failure
    back here. Not itself transactional — `services.nightly` pairs this
    with `Session.begin_nested()` around each item so one item's rollback
    never touches another's committed work.
    """

    _attempted: int = field(default=0, init=False)
    _succeeded: int = field(default=0, init=False)
    _failures: list[ItemFailure] = field(default_factory=list, init=False)

    def record_success(self) -> None:
        self._attempted += 1
        self._succeeded += 1

    def record_failure(self, item_id: object, error: BaseException | str) -> None:
        self._attempted += 1
        message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        self._failures.append(ItemFailure(item_id=str(item_id), error=message))

    def report(self) -> IsolationReport:
        return IsolationReport(
            attempted=self._attempted,
            succeeded=self._succeeded,
            failures=tuple(self._failures),
        )


# -- T12: batch completion deadline --------------------------------------


def deadline_instant(as_of_date: date, deadline_ist: str) -> datetime:
    """Return T12's deadline (`spec §17.5`, e.g. ``"07:00"``) for
    `as_of_date` as an aware UTC instant.

    `deadline_ist` is interpreted on `as_of_date`'s own calendar day in
    India Standard Time — a batch scheduled to finish before sunrise IST,
    which is what T12's default (07:00 IST) is calibrated against.
    """

    if not isinstance(as_of_date, date):
        raise TypeError("as_of_date must be a date.")
    local_time = _parse_hhmm(deadline_ist)
    local_instant = datetime.combine(as_of_date, local_time, tzinfo=IST)
    return local_instant.astimezone(UTC)


def is_past_deadline(now: datetime, deadline_at: datetime) -> bool:
    """T12's boundary is inclusive: "Exactly at: Alert at the deadline"."""

    return _aware(now, "now") >= _aware(deadline_at, "deadline_at")


@dataclass(frozen=True, slots=True)
class PipelineRunStatus:
    """One pipeline run's per-step outcome, as of the moment it was read."""

    steps: Mapping[str, str | None]
    ordered_steps: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.ordered_steps:
            raise ValueError("PipelineRunStatus.ordered_steps must be non-empty.")

    @property
    def is_complete(self) -> bool:
        return all(self.steps.get(step) == SUCCEEDED for step in self.ordered_steps)

    @property
    def is_open(self) -> bool:
        """Not yet complete — still running, still to be retried, or
        halted on a failed step; T12 does not distinguish these, since a
        portfolio that is not fully scored is not fully scored either way."""

        return not self.is_complete

    @property
    def first_incomplete_step(self) -> str | None:
        """The first step, in order, that has not succeeded — `None` once
        every step has. This is whatever is blocking completion, whether
        that step is failed, still running, or simply not reached yet;
        callers that need "genuinely failed, not merely pending" should
        also check the step's own recorded state."""

        for step in self.ordered_steps:
            if self.steps.get(step) != SUCCEEDED:
                return step
        return None


@dataclass(frozen=True, slots=True)
class DeadlineEvaluation:
    """T12 applied once to one pipeline run's current status."""

    run_id: str
    as_of_date: date
    deadline_at: datetime
    now: datetime
    is_complete: bool
    breached: bool

    @property
    def should_alert(self) -> bool:
        return self.breached and not self.is_complete


def evaluate_deadline(
    *,
    run_id: str,
    as_of_date: date,
    deadline_ist: str,
    now: datetime,
    is_complete: bool,
) -> DeadlineEvaluation:
    """Apply T12 to one pipeline run: `spec §17.5`'s "Above: Alert raised,
    run continues" and "Exactly at: Alert at the deadline"."""

    if not run_id or not isinstance(run_id, str):
        raise ValueError("run_id must be a non-empty string.")
    deadline_at = deadline_instant(as_of_date, deadline_ist)
    return DeadlineEvaluation(
        run_id=run_id,
        as_of_date=as_of_date,
        deadline_at=deadline_at,
        now=_aware(now, "now"),
        is_complete=is_complete,
        breached=is_past_deadline(now, deadline_at),
    )


# -- recurring-failure escalation ----------------------------------------


def normalize_error(message: str) -> str:
    """Fold run-specific detail (ids, timestamps, counts) out of an error
    message so the same underlying cause compares equal night over night —
    "the same failure recurring across nights" (`spec §R-28.b`) means the
    same *cause*, not two failures that merely both exist.
    """

    text = _UUID_RE.sub("<id>", message)
    text = _TIMESTAMP_RE.sub("<timestamp>", text)
    text = _NUMBER_RE.sub("<n>", text)
    return " ".join(text.strip().lower().split())


def is_recurring_failure(
    consecutive_failures: Sequence[str],
    *,
    threshold: int = DEFAULT_RECURRING_FAILURE_THRESHOLD,
) -> bool:
    """`consecutive_failures` is the most recent run's failure message
    first, back through immediately preceding runs of the *same* job with
    no intervening success — the caller stops collecting at the first
    success or at `threshold`, whichever comes first. Returns whether the
    leading `threshold` of them share one normalized cause.
    """

    if threshold < 2:
        raise ValueError("threshold must be at least 2: one failure alone cannot recur.")
    if len(consecutive_failures) < threshold:
        return False
    window = [normalize_error(message) for message in consecutive_failures[:threshold]]
    baseline = window[0]
    return bool(baseline) and all(message == baseline for message in window[1:])


def _parse_hhmm(value: str) -> time:
    if not isinstance(value, str):
        raise TypeError("deadline_ist must be a string in HH:MM format.")
    try:
        hours_text, minutes_text = value.split(":", 1)
        hours, minutes = int(hours_text), int(minutes_text)
    except ValueError as error:
        raise ValueError(f"deadline_ist {value!r} is not in HH:MM format.") from error
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"deadline_ist {value!r} is not a valid 24-hour time.")
    return time(hour=hours, minute=minutes)


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    return value


__all__ = [
    "DEFAULT_RECURRING_FAILURE_THRESHOLD",
    "IST",
    "DeadlineEvaluation",
    "IsolationReport",
    "IsolationTracker",
    "ItemFailure",
    "PipelineRunStatus",
    "deadline_instant",
    "evaluate_deadline",
    "is_past_deadline",
    "is_recurring_failure",
    "normalize_error",
]
