"""Unit coverage for the T-109 case state machine and SLA rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.cases.lifecycle import (
    CaseState,
    permitted_transitions,
    transition,
)
from covenant_radar.domain.cases.sla import SlaThresholds, due_at, is_overdue, sla_hours

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_T11 = {"T11": {"act_sla_hours": 24, "amber_sla_hours": 72, "watch_sla_hours": 168}}


def test_permitted_transitions_only() -> None:
    assert transition(CaseState.OPEN, CaseState.IN_PROGRESS) is CaseState.IN_PROGRESS
    assert transition("in_progress", "monitoring") == "monitoring"
    assert set(permitted_transitions("escalated")) == {"in_progress", "monitoring", "closed"}

    with pytest.raises(ValidationError, match="Permitted transitions"):
        transition(CaseState.CLOSED, CaseState.OPEN)


def test_sla_from_band() -> None:
    configured = SlaThresholds.from_store(_T11)

    assert sla_hours("act", configured) == 24
    assert sla_hours("amber", _T11) == 72
    assert sla_hours("watch", _T11) == 168
    assert due_at(_NOW, "act", configured) == _NOW + timedelta(hours=24)
    assert is_overdue(_NOW + timedelta(hours=24), _NOW + timedelta(hours=24)) is True
    assert is_overdue(_NOW + timedelta(hours=24), _NOW + timedelta(hours=23, minutes=59)) is False


def test_closure_requires_reason() -> None:
    with pytest.raises(ValidationError, match="closure reason is required"):
        transition("open", "closed")

    assert transition("open", "closed", closure_reason="Resolved with evidence") is CaseState.CLOSED
