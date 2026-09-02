"""Data-integrity verification: the audit chain, referential integrity,
threshold-snapshot references and document-store consistency (`T-150`,
`spec §N-06.c`).

This is a read-only diagnostic, not a business service: it never writes a
row anywhere.  Its output is a durable :class:`IntegrityReport`, meant to be
returned as a scheduled job's metrics (`scheduler.jobs.integrity_check_job`)
so `job_run` — already the batch ledger every other nightly job is recorded
in — is where "did the check even run" is answered, per-check, forever.

**Why a generic foreign-key scan rather than one hand-written query per
table.** `plan.md §5`'s schema declares every real reference as a SQLAlchemy
`ForeignKey`. Introspecting `Base.metadata` once and building one anti-join
per declared constraint means a new table with a new foreign key is covered
automatically, with no second place to remember to update when this task's
own file list does not include the models it is protecting.

**Why the audit chain and the document store are incremental and the rest
are not.** Both grow without bound and are read in a stable, monotonic
order (`audit_event.sequence`; `document.id`, a UUIDv7 and therefore already
insertion-ordered) — a watermark lets each run pick up exactly where the
last one left off. A referential-integrity anti-join has no such order that
would make a partial scan meaningful: a corruption introduced years ago in
an old row is exactly as urgent as one introduced yesterday, so every run
scans the whole table via one indexed anti-join rather than a chunk of it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from covenant_radar.audit.reconstruct import json_safe
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.errors import NotFound
from covenant_radar.db.base import Base
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.session import SessionFactory, is_database_session
from covenant_radar.ports.document_store import DocumentStore
from covenant_radar.scheduler.jobs import INTEGRITY_CHECK_JOB_NAME, JobHandler, JobRunContext
from covenant_radar.scheduler.ledger import SUCCEEDED

# Importing the models package guarantees every table in `plan.md §5` is
# registered on `Base.metadata` before `_check_foreign_keys` introspects it,
# regardless of what an embedding process happened to import first.
import covenant_radar.db.models as _all_models  # noqa: F401  # side effect: full metadata

_LOGGER = logging.getLogger(__name__)

CheckStatus = Literal["ok", "failed", "not_configured"]

AUDIT_CHAIN_CHECK: Final[str] = "audit_chain"
REFERENTIAL_INTEGRITY_CHECK: Final[str] = "referential_integrity"
SNAPSHOT_REFERENCE_CHECK: Final[str] = "snapshot_reference"
DOCUMENT_STORE_CHECK: Final[str] = "document_store"

_SNAPSHOT_TABLE_NAME: Final[str] = "threshold_snapshot"
_MAX_FINDINGS: Final[int] = 20
_DEFAULT_DOCUMENT_BATCH_SIZE: Final[int] = 5_000
_DEFAULT_AUDIT_CHAIN_BATCH_SIZE: Final[int] = 100_000


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one named check within one integrity run."""

    name: str
    status: CheckStatus
    checked: int
    failed: int
    detail: str
    findings: tuple[Mapping[str, object], ...] = ()
    watermark: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.checked < 0 or self.failed < 0:
            raise ValueError("CheckResult.checked and .failed cannot be negative.")


@dataclass(frozen=True, slots=True)
class IntegrityAlert:
    """One check's failure, shaped for an injected alert hook."""

    check: str
    severity: str
    summary: str
    findings: tuple[Mapping[str, object], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Every check's outcome from one run, always present even when clean —
    absence of a report must never be mistaken for a check that did not
    run."""

    generated_at: datetime
    checks: tuple[CheckResult, ...]

    @property
    def healthy(self) -> bool:
        return all(check.status != "failed" for check in self.checks)

    def as_metrics(self) -> dict[str, object]:
        """A JSON-safe shape suitable for `job_run.metrics`."""

        return {
            "generated_at": self.generated_at.isoformat(),
            "healthy": self.healthy,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "checked": check.checked,
                    "failed": check.failed,
                    "detail": check.detail,
                    "findings": [json_safe(dict(item)) for item in check.findings],
                    "watermark": dict(check.watermark) if check.watermark else None,
                }
                for check in self.checks
            ],
        }


AlertHook = Callable[[IntegrityAlert], object]


class IntegrityService:
    """Run every `T-150` check once against one session."""

    def __init__(
        self,
        session: Session,
        *,
        document_store: DocumentStore | None = None,
        clock: Clock | None = None,
        alert: AlertHook | None = None,
        document_batch_size: int = _DEFAULT_DOCUMENT_BATCH_SIZE,
        audit_chain_batch_size: int = _DEFAULT_AUDIT_CHAIN_BATCH_SIZE,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("IntegrityService requires a SQLAlchemy Session.")
        if document_store is not None and not isinstance(document_store, DocumentStore):
            raise TypeError("IntegrityService document_store must implement DocumentStore.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("IntegrityService clock must expose now().")
        if alert is not None and not callable(alert):
            raise TypeError("IntegrityService alert must be callable.")
        if (
            isinstance(document_batch_size, bool)
            or not isinstance(document_batch_size, int)
            or document_batch_size < 1
        ):
            raise ValueError("document_batch_size must be a positive integer.")
        if (
            isinstance(audit_chain_batch_size, bool)
            or not isinstance(audit_chain_batch_size, int)
            or audit_chain_batch_size < 1
        ):
            raise ValueError("audit_chain_batch_size must be a positive integer.")
        self.session = session
        self.document_store = document_store
        self.clock = clock or SystemClock()
        self.alert = alert
        self.document_batch_size = document_batch_size
        self.audit_chain_batch_size = audit_chain_batch_size

    def run(self, *, previous_metrics: Mapping[str, object] | None = None) -> IntegrityReport:
        """Run every check once and alert on any that failed.

        `previous_metrics` is the prior run's :meth:`IntegrityReport.as_metrics`
        (typically the last successful `job_run` row for this job), used only
        to resume the audit-chain and document-store watermarks.
        """

        if previous_metrics is not None and not isinstance(previous_metrics, Mapping):
            raise TypeError("previous_metrics must be a mapping or None.")
        checks = (
            self._check_audit_chain(previous_metrics),
            self._check_referential_integrity(),
            self._check_snapshot_references(),
            self._check_document_store(previous_metrics),
        )
        for check in checks:
            if check.status == "failed":
                self._raise_alert(check)
        return IntegrityReport(generated_at=self._now(), checks=checks)

    # -- audit chain ------------------------------------------------------

    def _check_audit_chain(self, previous_metrics: Mapping[str, object] | None) -> CheckResult:
        """Verify one bounded, contiguous slice of the chain per run.

        The watermark is a rolling cursor, not a high-water mark that is
        never revisited: once it reaches the current tail it wraps back to
        the start, so every row is eventually re-verified on some future
        run. A hash chain's own tamper-evidence is local to each row's
        stored `hash` — a row altered long after it was written, behind the
        watermark, changes nothing about any row after it and would never
        be found again by a cursor that only ever moves forward. Continuous,
        bounded re-scanning is what keeps that possibility closed while
        still keeping any one run's cost bounded, exactly as this check's
        `spec §N-06.c` "large database" requirement asks.
        """

        repository = AuditRepository(self.session)
        tail = repository.latest()
        if tail is None:
            return CheckResult(
                name=AUDIT_CHAIN_CHECK,
                status="ok",
                checked=0,
                failed=0,
                detail="The audit chain has no events yet.",
            )

        watermark = _previous_watermark(previous_metrics, AUDIT_CHAIN_CHECK, "last_verified_sequence")
        cursor = watermark if isinstance(watermark, int) and 0 <= watermark < tail.sequence else 0
        from_sequence = cursor + 1
        to_sequence = min(tail.sequence, cursor + self.audit_chain_batch_size)

        break_ = repository.verify_chain(from_sequence=from_sequence, to_sequence=to_sequence)
        checked = to_sequence - from_sequence + 1

        if break_ is not None:
            # The cursor is deliberately *not* advanced past a break: the
            # next run must keep reporting it, at the same position, until
            # it is remediated — resuming past a known break would let a
            # transient alerting failure make the corruption invisible.
            return CheckResult(
                name=AUDIT_CHAIN_CHECK,
                status="failed",
                checked=checked,
                failed=1,
                detail=break_.message,
                findings=(
                    {
                        "sequence": break_.sequence,
                        "previous_sequence": break_.previous_sequence,
                        "reason": break_.reason,
                    },
                ),
                watermark={"last_verified_sequence": cursor},
            )

        wrapped = to_sequence >= tail.sequence
        detail = f"Verified sequence {from_sequence} through {to_sequence} of {tail.sequence}."
        if wrapped:
            detail += (
                " The rolling verification cycle reached the current tail and "
                "will restart from the beginning on the next run."
            )
        return CheckResult(
            name=AUDIT_CHAIN_CHECK,
            status="ok",
            checked=checked,
            failed=0,
            detail=detail,
            watermark={"last_verified_sequence": 0 if wrapped else to_sequence},
        )

    # -- referential integrity and snapshot references ---------------------

    def _check_referential_integrity(self) -> CheckResult:
        return self._check_foreign_keys(
            name=REFERENTIAL_INTEGRITY_CHECK,
            include_target=lambda target: target != _SNAPSHOT_TABLE_NAME,
            failure_noun="foreign key",
        )

    def _check_snapshot_references(self) -> CheckResult:
        return self._check_foreign_keys(
            name=SNAPSHOT_REFERENCE_CHECK,
            include_target=lambda target: target == _SNAPSHOT_TABLE_NAME,
            failure_noun="threshold-snapshot reference",
        )

    def _check_foreign_keys(
        self,
        *,
        name: str,
        include_target: Callable[[str], bool],
        failure_noun: str,
    ) -> CheckResult:
        checked_tables: dict[str, int] = {}
        findings: list[dict[str, object]] = []
        total_failed = 0

        for table in Base.metadata.sorted_tables:
            constraints = [
                constraint
                for constraint in table.foreign_key_constraints
                if constraint.elements and include_target(constraint.elements[0].column.table.name)
            ]
            if not constraints:
                continue
            if table.name not in checked_tables:
                checked_tables[table.name] = self.session.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()

            pk_columns = list(table.primary_key.columns)
            for constraint in constraints:
                elements = list(constraint.elements)
                parent_table = elements[0].column.table
                parent_alias = parent_table.alias()
                join_conditions = [
                    parent_alias.c[foreign_key.column.name] == foreign_key.parent
                    for foreign_key in elements
                ]
                not_null_conditions = [foreign_key.parent.is_not(None) for foreign_key in elements]
                missing = ~exists(
                    select(parent_alias.c[elements[0].column.name]).where(and_(*join_conditions))
                )
                where_clause = and_(*not_null_conditions, missing)

                broken_count = self.session.execute(
                    select(func.count()).select_from(table).where(where_clause)
                ).scalar_one()
                if not broken_count:
                    continue
                total_failed += broken_count

                if len(findings) < _MAX_FINDINGS and pk_columns:
                    remaining = _MAX_FINDINGS - len(findings)
                    sample = self.session.execute(
                        select(*pk_columns).select_from(table).where(where_clause).limit(remaining)
                    ).all()
                    column_names = [foreign_key.parent.name for foreign_key in elements]
                    reference = f"{parent_table.name}.{elements[0].column.name}"
                    for row in sample:
                        findings.append(
                            {
                                "table": table.name,
                                "columns": column_names,
                                "references": reference,
                                "row": json_safe(dict(zip((c.name for c in pk_columns), row, strict=True))),
                            }
                        )

        checked = sum(checked_tables.values())
        if total_failed:
            return CheckResult(
                name=name,
                status="failed",
                checked=checked,
                failed=total_failed,
                detail=(
                    f"{total_failed} row(s) have a {failure_noun} pointing at a missing row."
                ),
                findings=tuple(findings),
            )
        return CheckResult(
            name=name,
            status="ok",
            checked=checked,
            failed=0,
            detail=f"Verified {checked} row(s) across {len(checked_tables)} table(s); no missing references.",
        )

    # -- document store -----------------------------------------------------

    def _check_document_store(self, previous_metrics: Mapping[str, object] | None) -> CheckResult:
        if self.document_store is None:
            return CheckResult(
                name=DOCUMENT_STORE_CHECK,
                status="not_configured",
                checked=0,
                failed=0,
                detail="No document store is configured.",
            )

        watermark = _previous_watermark(previous_metrics, DOCUMENT_STORE_CHECK, "last_document_id")
        watermark_id = _parse_uuid(watermark)

        rows = self._document_batch(after=watermark_id)
        wrapped = False
        if not rows and watermark_id is not None:
            # The prior watermark reached the end of the table; wrap around
            # so the scan keeps cycling through every document over time
            # rather than checking new uploads forever and nothing else.
            rows = self._document_batch(after=None)
            wrapped = True

        missing: list[dict[str, object]] = []
        for document_id, storage_key in rows:
            if not self._document_exists(storage_key):
                missing.append({"document_id": str(document_id), "storage_key": storage_key})

        new_watermark = str(rows[-1][0]) if rows else (str(watermark_id) if watermark_id else None)
        if missing:
            detail = f"{len(missing)} of {len(rows)} document(s) checked are missing their stored bytes."
        else:
            detail = f"Verified {len(rows)} document(s) against the document store."
        if wrapped:
            detail += " The scan cycle restarted from the beginning of the document table."

        return CheckResult(
            name=DOCUMENT_STORE_CHECK,
            status="failed" if missing else "ok",
            checked=len(rows),
            failed=len(missing),
            detail=detail,
            findings=tuple(missing[:_MAX_FINDINGS]),
            watermark={"last_document_id": new_watermark} if new_watermark else None,
        )

    def _document_batch(self, *, after: UUID | None) -> list[tuple[UUID, str]]:
        statement = select(Document.id, Document.storage_key).order_by(Document.id)
        if after is not None:
            statement = statement.where(Document.id > after)
        statement = statement.limit(self.document_batch_size)
        return [(row[0], row[1]) for row in self.session.execute(statement).all()]

    def _document_exists(self, storage_key: str) -> bool:
        assert self.document_store is not None
        try:
            iterator = self.document_store.stream(storage_key)
        except NotFound:
            return False
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
        return True

    # -- alerting -------------------------------------------------------

    def _raise_alert(self, check: CheckResult) -> None:
        _LOGGER.critical(
            "Data-integrity check %r failed (%d of %d): %s",
            check.name,
            check.failed,
            check.checked,
            check.detail,
        )
        if self.alert is None:
            return
        alert = IntegrityAlert(
            check=check.name,
            severity="critical",
            summary=check.detail,
            findings=check.findings,
        )
        try:
            self.alert(alert)
        except Exception:
            _LOGGER.warning("Integrity alert hook failed for check %r.", check.name, exc_info=True)

    def _now(self) -> datetime:
        value = self.clock.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock.now() must return a timezone-aware datetime.")
        return value.astimezone(UTC)


def build_integrity_check_job_handler(
    session_factory: SessionFactory,
    *,
    document_store: DocumentStore | None = None,
    clock: Clock | None = None,
    alert: AlertHook | None = None,
    document_batch_size: int = _DEFAULT_DOCUMENT_BATCH_SIZE,
    audit_chain_batch_size: int = _DEFAULT_AUDIT_CHAIN_BATCH_SIZE,
    job_name: str = INTEGRITY_CHECK_JOB_NAME,
) -> JobHandler:
    """Build the `JobHandler` `scheduler.jobs.integrity_check_job` schedules.

    Composition, not business logic: opens a read-only session, resumes the
    prior run's watermarks from the last successful `job_run` row for
    `job_name`, and returns the full report as this run's metrics. A check
    that finds corruption is the job working as intended, not a job
    failure — it always returns rather than raising, so `job_run.metrics`
    durably records the finding (per-check, every run) precisely when it
    matters most. A handler that raises here is reporting its own inability
    to run the checks at all (a database or document-store outage), which
    `scheduler.runner.JobRunner`'s own retry policy is what should react to.
    """

    if not callable(session_factory):
        raise TypeError("build_integrity_check_job_handler requires a callable session_factory.")
    if document_store is not None and not isinstance(document_store, DocumentStore):
        raise TypeError(
            "build_integrity_check_job_handler document_store must implement DocumentStore."
        )

    def handler(context: JobRunContext) -> Mapping[str, object]:
        session = session_factory()
        try:
            previous = _last_successful_metrics(session, job_name)
            service = IntegrityService(
                session,
                document_store=document_store,
                clock=clock,
                alert=alert,
                document_batch_size=document_batch_size,
                audit_chain_batch_size=audit_chain_batch_size,
            )
            report = service.run(previous_metrics=previous)
            return report.as_metrics()
        finally:
            session.close()

    return handler


def _last_successful_metrics(session: Session, job_name: str) -> Mapping[str, object] | None:
    metrics = session.execute(
        select(JobRun.metrics)
        .where(JobRun.job_name == job_name, JobRun.state == SUCCEEDED)
        .order_by(JobRun.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return metrics if isinstance(metrics, Mapping) else None


def _previous_watermark(
    previous_metrics: Mapping[str, object] | None, check_name: str, key: str
) -> object | None:
    if not isinstance(previous_metrics, Mapping):
        return None
    checks = previous_metrics.get("checks")
    if not isinstance(checks, list):
        return None
    for entry in checks:
        if isinstance(entry, Mapping) and entry.get("name") == check_name:
            watermark = entry.get("watermark")
            if isinstance(watermark, Mapping):
                return watermark.get(key)
            return None
    return None


def _parse_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


__all__ = [
    "AUDIT_CHAIN_CHECK",
    "DOCUMENT_STORE_CHECK",
    "REFERENTIAL_INTEGRITY_CHECK",
    "SNAPSHOT_REFERENCE_CHECK",
    "AlertHook",
    "CheckResult",
    "CheckStatus",
    "IntegrityAlert",
    "IntegrityReport",
    "IntegrityService",
    "build_integrity_check_job_handler",
]
