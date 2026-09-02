"""Unit tests for the T-012 threshold store and its static policy check."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from covenant_radar.config.thresholds import (
    DEFAULT_THRESHOLD_PATH,
    THRESHOLD_NAMES,
    ThresholdConfigError,
    ThresholdProposalRecord,
    ThresholdSnapshotRecord,
    ThresholdStore,
    scan_threshold_literals,
)
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class _MemoryRepository:
    """Minimal persistence-port fake for focused threshold unit tests."""

    def __init__(self) -> None:
        self.snapshots: list[ThresholdSnapshotRecord] = []
        self.proposals: dict[UUID, ThresholdProposalRecord] = {}

    def get_active_snapshot(self, *, as_of: datetime) -> ThresholdSnapshotRecord | None:
        effective = [snapshot for snapshot in self.snapshots if snapshot.effective_from <= as_of]
        return max(
            effective,
            key=lambda snapshot: (snapshot.effective_from, snapshot.version),
            default=None,
        )

    def create_snapshot(
        self,
        *,
        snapshot_id: UUID,
        values: Mapping[str, object],
        source: str,
        effective_from: datetime,
        proposed_by_id: UUID | None,
        approved_by_id: UUID | None,
        note: str | None,
        actor_id: UUID | None,
        request_id: str,
    ) -> ThresholdSnapshotRecord:
        snapshot = ThresholdSnapshotRecord(
            id=snapshot_id,
            values=dict(values),
            source=source,
            effective_from=effective_from,
            version=len(self.snapshots) + 1,
            proposed_by_id=proposed_by_id,
            approved_by_id=approved_by_id,
            note=note,
        )
        self.snapshots.append(snapshot)
        return snapshot

    def create_pending_proposal(
        self,
        *,
        proposal_id: UUID,
        maker_id: UUID,
        payload: Mapping[str, object],
        created_at: datetime,
        request_id: str,
    ) -> ThresholdProposalRecord:
        proposal = ThresholdProposalRecord(proposal_id, "pending", maker_id, dict(payload))
        self.proposals[proposal_id] = proposal
        return proposal

    def lock_pending_proposal(self, proposal_id: UUID) -> ThresholdProposalRecord | None:
        return self.proposals.get(proposal_id)

    def mark_proposal_approved(
        self, *, proposal_id: UUID, approver_id: UUID, decided_at: datetime, request_id: str
    ) -> None:
        proposal = self.proposals[proposal_id]
        self.proposals[proposal_id] = ThresholdProposalRecord(
            proposal.id, "approved", proposal.maker_id, proposal.payload, proposal.version + 1
        )

    def record_audit(
        self,
        *,
        event_type: str,
        subject_type: str,
        subject_id: UUID,
        payload: Mapping[str, object],
        actor: object,
        occurred_at: datetime,
        request_id: str,
    ) -> object:
        return object()


def test_twelve_thresholds_present_with_spec_values() -> None:
    store = ThresholdStore(path=DEFAULT_THRESHOLD_PATH)

    assert tuple(store.values()) == THRESHOLD_NAMES
    assert store.get("T1") == {"act": Decimal("0.70"), "amber": Decimal("0.40")}
    assert store.get("T2")["confidence_floor"] == Decimal("0.50")
    assert store.get("T3") == {
        "sustained_days": 14,
        "sustained_events": 3,
        "event_window_days": 30,
    }
    assert store.get("T4")["headroom_erosion_pct"] == Decimal("0.05")
    assert store.get("T5")["contribution_share"] == Decimal("0.10")
    assert store.get("T6")["max_output_tokens"] == 1200
    assert store.get("T7") == {
        "calls_per_hour": 200,
        "calls_per_day": 2000,
        "monthly_budget": None,
    }
    assert store.get("T8")["bad_shape_retries"] == 1
    assert store.get("T9")["ocr_confidence_floor"] == Decimal("0.80")
    assert store.get("T10") == {
        "auto_accept": Decimal("0.90"),
        "review_floor": Decimal("0.60"),
    }
    assert store.get("T11") == {
        "act_sla_hours": 24,
        "amber_sla_hours": 72,
        "watch_sla_hours": 168,
    }
    assert store.get("T12") == {"deadline_ist": "07:00"}


def test_unknown_name_lists_valid_names() -> None:
    with pytest.raises(KeyError) as raised:
        ThresholdStore().get("T13")

    message = str(raised.value)
    assert "T13" in message
    assert ", ".join(THRESHOLD_NAMES) in message


def test_malformed_file_keeps_last_good_and_names_line(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.json"
    path.write_text(DEFAULT_THRESHOLD_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    store = ThresholdStore(path=path)
    before = store.values()
    path.write_text('{\n  "T1": {\n', encoding="utf-8")

    with pytest.raises(ThresholdConfigError, match=r"line 3"):
        store.reload()

    assert store.values() == before


def test_amber_above_act_refused(tmp_path: Path) -> None:
    repository = _MemoryRepository()
    maker = new_id()
    store = ThresholdStore(repository, clock=FixedClock(_NOW), request_id="rq-proposal-test")

    with pytest.raises(ThresholdConfigError, match="amber must not exceed act"):
        store.propose({"T1": {"amber": "0.80"}}, maker)

    assert repository.proposals == {}


def test_no_threshold_literal_in_source(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    clean = source_root / "clean.py"
    clean.write_text(
        '"""0.70 is documentation, not executable policy."""\nvalue = 1\n', encoding="utf-8"
    )
    assert scan_threshold_literals(source_root) == ()

    offending = source_root / "offending.py"
    offending.write_text("T1_ACT = 0.70\n", encoding="utf-8")
    findings = scan_threshold_literals(source_root)
    assert len(findings) == 1
    assert "offending.py:1" in findings[0]
