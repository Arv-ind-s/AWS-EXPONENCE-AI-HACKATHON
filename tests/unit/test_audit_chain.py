"""Unit coverage for canonical audit hashing and first-break reporting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.audit.chain import canonical_payload, compute_event_hash
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.audit.store import AuditRecord, InMemoryAuditEvent, InMemoryAuditStore
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.repositories.audit import AuditChainError, AuditRepository

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _recorder(store: InMemoryAuditStore) -> AuditRecorder:
    return AuditRecorder(store, clock=FixedClock(_NOW), request_id="rq-audit-unit-0001")


def _append(
    store: InMemoryAuditStore,
    event_type: str,
    payload: dict[str, object],
) -> InMemoryAuditEvent:
    return _recorder(store).record(
        event_type,
        ("borrower", new_id()),
        payload,
        actor=None,
    )


def test_hash_covers_content_and_previous() -> None:
    subject_id = new_id()
    first = compute_event_hash(
        1, _NOW, None, "event.created", "borrower", subject_id, {"value": 1}, None
    )

    changed_content = compute_event_hash(
        1, _NOW, None, "event.created", "borrower", subject_id, {"value": 2}, None
    )
    changed_previous = compute_event_hash(
        2, _NOW, None, "event.created", "borrower", subject_id, {"value": 1}, first
    )
    changed_previous_again = compute_event_hash(
        2, _NOW, None, "event.created", "borrower", subject_id, {"value": 1}, "other"
    )

    assert first != changed_content
    assert changed_previous != changed_previous_again


def test_canonical_serialisation_stable() -> None:
    left = {"b": [2, {"z": "क", "a": True}], "a": "value"}
    right = {"a": "value", "b": [2, {"a": True, "z": "क"}]}

    assert canonical_payload(left) == canonical_payload(right)
    assert canonical_payload(left) == '{"a":"value","b":[2,{"a":true,"z":"क"}]}'


def test_wrong_previous_hash_refused() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = AuditRepository(session)
        recorder = AuditRecorder(repository, clock=FixedClock(_NOW), request_id="rq-audit-unit")
        first = recorder.record("event.created", ("borrower", new_id()), {"value": 1}, actor=None)
        session.commit()

        entry = AuditRecord(
            event_type="event.updated",
            subject_type="borrower",
            subject_id=first.subject_id,
            payload={"value": 2},
            actor_id=None,
            actor_label=None,
            occurred_at=_NOW,
            request_id="rq-audit-unit",
        )
        with pytest.raises(AuditChainError, match="current chain tail"):
            repository.append(entry, previous_hash="not-the-tail")
    engine.dispose()


def test_verification_names_first_break() -> None:
    store = InMemoryAuditStore()
    _append(store, "event.one", {"value": 1})
    second = _append(store, "event.two", {"value": 2})
    _append(store, "event.three", {"value": 3})

    second.payload["value"] = 99
    first_break = store.verify_chain()

    assert first_break is not None
    assert first_break.sequence == second.sequence
    assert first_break.previous_sequence == 1
    assert "sequence 2" in first_break.message
    assert "sequence 1" in first_break.message
