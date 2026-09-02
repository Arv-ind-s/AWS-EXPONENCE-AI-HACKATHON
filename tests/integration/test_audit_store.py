"""Integration coverage for the database-backed append-only audit store."""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from covenant_radar.audit.chain import PersonalDataRefused
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.audit.store import InMemoryAuditStore
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.repositories.audit import AuditRepository

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _recorder(store: InMemoryAuditStore) -> AuditRecorder:
    return AuditRecorder(store, clock=FixedClock(_NOW), request_id="rq-audit-integration")


def test_personal_value_in_payload_refused() -> None:
    recorder = _recorder(InMemoryAuditStore())

    with pytest.raises(PersonalDataRefused, match="email.*reference"):
        recorder.record(
            "personal.read",
            ("borrower", new_id()),
            {"email": "officer@example.test"},
            actor=None,
        )

    accepted = recorder.record(
        "personal.read",
        ("borrower", new_id()),
        {"email": {"reference": "contact-ref-001"}},
        actor=None,
    )
    assert accepted.payload["email"] == {"reference": "contact-ref-001"}


def test_non_serialisable_raises_naming_field() -> None:
    recorder = _recorder(InMemoryAuditStore())

    with pytest.raises(TypeError, match=r"payload\.amount"):
        recorder.record(
            "financial.read",
            ("borrower", new_id()),
            {"amount": object()},
            actor=None,
        )


def test_concurrent_writes_keep_chain_valid(tmp_path: Path) -> None:
    database_path = tmp_path / "audit.sqlite3"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    writer_count = 12
    barrier = threading.Barrier(writer_count)
    failures: list[BaseException] = []
    failure_lock = threading.Lock()

    def write_one(index: int) -> None:
        session = factory()
        try:
            barrier.wait(timeout=10)
            AuditRecorder(
                AuditRepository(session),
                clock=FixedClock(_NOW),
                request_id=f"rq-audit-{index:04d}",
            ).record(
                "concurrent.write",
                ("borrower", new_id()),
                {"worker": index},
                actor=None,
            )
            session.commit()
        except BaseException as error:  # surfaced below with the worker index
            with failure_lock:
                failures.append(error)
            session.rollback()
        finally:
            session.close()

    workers = [threading.Thread(target=write_one, args=(index,)) for index in range(writer_count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)

    assert failures == []
    with Session(engine) as session:
        repository = AuditRepository(session)
        rows = repository.rows()
        assert [row.sequence for row in rows] == list(range(1, writer_count + 1))
        assert repository.verify_chain() is None

        # A privileged database tamper is deliberately simulated here by
        # changing a persisted row.  The application never exposes this
        # mutation surface; verification must nevertheless identify the
        # affected sequence when the database is inspected.
        tampered = rows[writer_count // 2]
        tampered.payload = {"worker": "tampered"}
        session.commit()
        first_break = repository.verify_chain()
        assert first_break is not None
        assert first_break.sequence == tampered.sequence
    engine.dispose()


def test_no_update_or_delete_in_source() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    forbidden = re.compile(r"(?is)\b(?:update\s+audit_event|delete\s+from\s+audit_event)\b")
    findings: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in forbidden.finditer(source):
            line_number = source.count("\n", 0, match.start()) + 1
            findings.append(f"{path}:{line_number}: {match.group(0)!r}")

    assert findings == [], "audit_event mutation statement found:\n" + "\n".join(findings)


def test_application_role_lacks_grants(tmp_path: Path) -> None:
    # SQLite has no grant catalogue; the adapter's empty result is the
    # portable equivalent of its database-level append-only contract.  A
    # PostgreSQL deployment calls the same method against the separately
    # provisioned application role.
    engine = create_engine(f"sqlite:///{tmp_path / 'grants.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = AuditRepository(session)
        assert repository.mutation_privileges() == frozenset()
        repository.assert_application_role_is_append_only()
    engine.dispose()
