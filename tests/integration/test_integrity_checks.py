"""Integration coverage for `T-150`'s data-integrity checks: the audit
chain, referential integrity, threshold-snapshot references and
document-store consistency.

Each test opens its own file-backed SQLite database (the same recipe
`test_audit_store.py` uses) rather than the shared PostgreSQL fixtures,
since these tests deliberately corrupt already-committed rows the
application itself never writes — SQLite's default of not enforcing
`FOREIGN KEY` constraints is exactly what lets a test simulate the kind of
privileged, out-of-band tamper (a direct database edit, a purge that
outran a constraint) this job exists to catch.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import NotFound
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent, ThresholdSnapshot
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.scheduler.jobs import INTEGRITY_CHECK_JOB_NAME, JobRunContext
from covenant_radar.scheduler.ledger import SUCCEEDED
from covenant_radar.services.integrity import (
    AUDIT_CHAIN_CHECK,
    DOCUMENT_STORE_CHECK,
    REFERENTIAL_INTEGRITY_CHECK,
    SNAPSHOT_REFERENCE_CHECK,
    IntegrityAlert,
    IntegrityService,
    build_integrity_check_job_handler,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
_REQUEST_ID = "rq-integrity-test"


def _session(tmp_path: Path, name: str) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return factory()


def _recorder(session: Session) -> AuditRecorder:
    return AuditRecorder(AuditRepository(session), clock=FixedClock(_NOW), request_id=_REQUEST_ID)


class _FakeDocumentStore:
    """A minimal `DocumentStore` double: only presence matters here, not
    the encrypted-envelope format the real filesystem adapter uses."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_bytes(self, storage_key: str, data: bytes) -> None:
        self._objects[storage_key] = data

    def put(self, content: object, *, content_hash: str | None = None) -> str:
        raise NotImplementedError("not used by these tests")

    def get(self, storage_key: str) -> bytes:
        raise NotImplementedError("not used by these tests")

    def delete(self, storage_key: str) -> None:
        raise NotImplementedError("not used by these tests")

    def stream(self, storage_key: str, *, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
        if storage_key not in self._objects:
            raise NotFound(f"Document storage key {storage_key!r} was not found.")
        data = self._objects[storage_key]

        def _iterate() -> Iterator[bytes]:
            yield data

        return _iterate()


def _document(**overrides: object) -> Document:
    values: dict[str, object] = {
        "id": new_id(),
        "borrower_id": uuid4(),
        "facility_id": None,
        "doc_type": "sanction_letter",
        "filename": "statement.pdf",
        "content_hash": "a" * 64,
        "byte_size": 128,
        "mime_type": "application/pdf",
        "storage_key": "sha256/" + "a" * 64,
        "uploaded_by_id": uuid4(),
        "created_at": _NOW,
        "updated_at": _NOW,
        "request_id": _REQUEST_ID,
        "version": 1,
    }
    values.update(overrides)
    return Document(**values)


def _threshold_snapshot() -> ThresholdSnapshot:
    return ThresholdSnapshot(
        id=new_id(),
        values={"warning_headroom_pct": "0.10"},
        source="test",
        effective_from=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
        version=1,
    )


def test_corrupted_chain_detected_and_alerted(tmp_path: Path) -> None:
    session = _session(tmp_path, "chain.sqlite3")
    recorder = _recorder(session)
    for index in range(4):
        recorder.record("integrity.smoke", ("borrower", new_id()), {"index": index}, actor=None)
    session.commit()

    # A privileged database tamper is deliberately simulated here, the same
    # way `test_audit_store.py::test_concurrent_writes_keep_chain_valid`
    # does: the application never exposes this mutation surface.
    tampered = session.execute(select(AuditEvent).where(AuditEvent.sequence == 2)).scalar_one()
    tampered.payload = {"tampered": True}
    session.commit()

    alerts: list[IntegrityAlert] = []
    report = IntegrityService(session, alert=alerts.append).run()

    chain = next(check for check in report.checks if check.name == AUDIT_CHAIN_CHECK)
    assert chain.status == "failed"
    assert chain.failed == 1
    assert chain.findings[0]["sequence"] == 2
    assert not report.healthy

    assert len(alerts) == 1
    assert alerts[0].check == AUDIT_CHAIN_CHECK
    assert alerts[0].severity == "critical"
    assert alerts[0].findings[0]["sequence"] == 2

    # Re-running without advancing past the break keeps reporting it —
    # remediation, not the passage of time, is what clears the alert.
    second_report = IntegrityService(session).run(previous_metrics=report.as_metrics())
    second_chain = next(c for c in second_report.checks if c.name == AUDIT_CHAIN_CHECK)
    assert second_chain.status == "failed"
    assert second_chain.findings[0]["sequence"] == 2

    session.close()


def test_missing_document_file_reported(tmp_path: Path) -> None:
    session = _session(tmp_path, "documents.sqlite3")
    store = _FakeDocumentStore()
    document = _document()
    session.add(document)
    session.commit()

    # The store never receives the bytes: the record promises a file that
    # is not there, exactly the scenario `spec §N-06.c` names.
    report = IntegrityService(session, document_store=store).run()

    check = next(c for c in report.checks if c.name == DOCUMENT_STORE_CHECK)
    assert check.status == "failed"
    assert check.checked == 1
    assert check.failed == 1
    assert check.findings[0]["document_id"] == str(document.id)
    assert check.findings[0]["storage_key"] == document.storage_key
    assert not report.healthy

    session.close()


def test_purged_snapshot_reference_detected(tmp_path: Path) -> None:
    session = _session(tmp_path, "snapshot.sqlite3")
    snapshot = _threshold_snapshot()
    session.add(snapshot)
    session.flush()

    recorder = _recorder(session)
    recorder.record(
        "integrity.smoke",
        ("borrower", new_id()),
        {"note": "references a snapshot"},
        actor=None,
        threshold_snapshot_id=snapshot.id,
    )
    session.commit()

    # `threshold_snapshot_id` is declared `ondelete="RESTRICT"` (`db/models/
    # audit.py`), but SQLite does not enforce it, which is what lets this
    # test simulate a purge that outran the constraint — a bulk restore, a
    # migration, or a privileged maintenance delete against the raw
    # database — leaving the audit event's reference dangling.
    session.delete(snapshot)
    session.commit()

    report = IntegrityService(session).run()

    check = next(c for c in report.checks if c.name == SNAPSHOT_REFERENCE_CHECK)
    assert check.status == "failed"
    assert check.failed == 1
    assert check.findings[0]["table"] == "audit_event"
    assert check.findings[0]["references"] == "threshold_snapshot.id"
    assert not report.healthy

    # The audit chain itself is untouched — only the snapshot row was
    # purged — so that check must stay clean; a broken snapshot reference
    # is not a broken chain.
    chain = next(c for c in report.checks if c.name == AUDIT_CHAIN_CHECK)
    assert chain.status == "ok"

    # The dangling reference must not also surface under the generic
    # referential-integrity check: it is reported exactly once, under its
    # own name, so an operator is not left guessing which alert to act on.
    referential = next(c for c in report.checks if c.name == REFERENTIAL_INTEGRITY_CHECK)
    assert referential.status == "ok"

    session.close()


def test_incremental_with_watermark(tmp_path: Path) -> None:
    session = _session(tmp_path, "watermark.sqlite3")
    recorder = _recorder(session)
    for index in range(8):
        recorder.record("integrity.smoke", ("borrower", new_id()), {"index": index}, actor=None)
    session.commit()

    service = IntegrityService(session, audit_chain_batch_size=3)
    seen: list[tuple[int, int]] = []
    previous_metrics: dict[str, object] | None = None
    for _ in range(4):
        report = service.run(previous_metrics=previous_metrics)
        chain = next(c for c in report.checks if c.name == AUDIT_CHAIN_CHECK)
        seen.append((chain.checked, chain.watermark["last_verified_sequence"]))
        previous_metrics = report.as_metrics()

    # Every run verifies at most its configured batch size — never the
    # whole 8-row chain in one pass — and the cursor advances 3, 6, then
    # wraps back to 0 once it reaches the tail, so the next run starts a
    # fresh cycle instead of trusting the already-verified range forever.
    assert seen == [(3, 3), (3, 6), (2, 0), (3, 3)]
    assert all(checked <= 3 for checked, _ in seen)

    session.close()


def test_clean_run_recorded(tmp_path: Path) -> None:
    session = _session(tmp_path, "clean.sqlite3")
    store = _FakeDocumentStore()
    recorder = _recorder(session)
    for index in range(3):
        recorder.record("integrity.smoke", ("borrower", new_id()), {"index": index}, actor=None)
    session.commit()

    report = IntegrityService(session, document_store=store).run()

    # Absence of a report is indistinguishable from a check that never
    # ran, so a clean pass must still produce one full, present record —
    # not an empty or omitted result — naming every check that ran.
    assert report.healthy
    metrics = report.as_metrics()
    assert metrics["healthy"] is True
    names = {check["name"] for check in metrics["checks"]}
    assert names == {
        AUDIT_CHAIN_CHECK,
        REFERENTIAL_INTEGRITY_CHECK,
        SNAPSHOT_REFERENCE_CHECK,
        DOCUMENT_STORE_CHECK,
    }
    for check in metrics["checks"]:
        assert check["status"] in ("ok", "not_configured")
        assert check["failed"] == 0

    chain = next(c for c in metrics["checks"] if c["name"] == AUDIT_CHAIN_CHECK)
    assert chain["checked"] == 3

    session.close()


def test_job_handler_resumes_watermark_from_last_successful_run(tmp_path: Path) -> None:
    """`scheduler.jobs.integrity_check_job`'s handler must record every
    run's report as `job_run.metrics` and resume the next run's watermark
    from the last successful one, so a hard restart never forces a full
    audit-chain re-scan."""

    session = _session(tmp_path, "job_handler.sqlite3")

    def session_factory() -> Session:
        return session

    recorder = _recorder(session)
    for index in range(8):
        recorder.record("integrity.smoke", ("borrower", new_id()), {"index": index}, actor=None)
    session.commit()

    handler = build_integrity_check_job_handler(
        session_factory,
        audit_chain_batch_size=3,
        job_name=INTEGRITY_CHECK_JOB_NAME,
    )
    context = JobRunContext(
        run_id="run-1", attempt=1, trigger="manual", request_id=_REQUEST_ID
    )

    first_metrics = handler(context)
    _record_job_run(session, run_id="run-1", metrics=first_metrics)

    second_metrics = handler(
        JobRunContext(run_id="run-2", attempt=1, trigger="manual", request_id=_REQUEST_ID)
    )

    first_chain = next(c for c in first_metrics["checks"] if c["name"] == AUDIT_CHAIN_CHECK)
    second_chain = next(c for c in second_metrics["checks"] if c["name"] == AUDIT_CHAIN_CHECK)
    assert first_chain["watermark"]["last_verified_sequence"] == 3
    assert second_chain["checked"] == 3
    assert second_chain["watermark"]["last_verified_sequence"] == 6

    session.close()


def _record_job_run(session: Session, *, run_id: str, metrics: dict[str, object]) -> None:
    """Persist one `succeeded` `job_run` row directly, the shape
    `scheduler.ledger.JobLedger.succeed` would leave behind, so
    `build_integrity_check_job_handler`'s watermark lookup has a real
    predecessor to resume from."""

    session.add(
        JobRun(
            id=new_id(),
            job_name=INTEGRITY_CHECK_JOB_NAME,
            run_id=run_id,
            trigger="manual",
            started_at=_NOW,
            finished_at=_NOW,
            state=SUCCEEDED,
            attempt=1,
            error=None,
            metrics=metrics,
            created_at=_NOW,
            updated_at=_NOW,
            created_by_id=None,
            updated_by_id=None,
            request_id=_REQUEST_ID,
        )
    )
    session.commit()
