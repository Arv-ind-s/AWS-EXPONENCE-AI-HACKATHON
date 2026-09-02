"""Unit coverage for T-100's product-owned stage-7 shape checks."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from covenant_radar.ai.shapes import CatalogueAction, check_stage7_shapes
from covenant_radar.domain.memo import MemoRecord, MemoRecords, RecordReference
from covenant_radar.services.memo import MemoAssemblyService

pytestmark = pytest.mark.unit


def _record(record_type: str, record_id: str, **values: object) -> MemoRecord:
    return MemoRecord(RecordReference(record_type, record_id), values)


def _slots():
    records = MemoRecords(
        situation=_record("triage", "triage-1", situation="Projected pressure requires review."),
        covenant_position=_record(
            "forecast",
            "forecast-1",
            ratio_name="Debt service coverage",
            value=Decimal("1.25"),
            threshold=Decimal("1.10"),
            headroom=Decimal("0.15"),
            probability=Decimal("0.42"),
            confidence=Decimal("0.88"),
            crossing_date=date(2026, 10, 15),
        ),
        drivers=(_record("driver", "driver-1", name="Cash-flow pressure"),),
        evidence=(_record("evidence", "evidence-1", citation="EV-001", count=3),),
        recommendations=(
            _record(
                "intervention",
                "intervention-1",
                code="CREDIT-REDUCE",
                role_tag="credit",
                text="Review and reduce funded exposure.",
            ),
        ),
    )
    return MemoAssemblyService().assemble(records)


def _catalogue():
    return (
        CatalogueAction(
            id="CREDIT-REDUCE",
            role_tag="credit",
            text="Review and reduce funded exposure.",
        ),
    )


def _draft(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "headline": "Debt service coverage is projected to reach the action point on 2026-10-15.",
        "summary": (
            "The recorded value is 1.25 against a threshold of 1.10, with headroom of 0.15. "
            "The projected breach probability is 0.42 at confidence 0.88."
        ),
        "drivers": ["Cash-flow pressure"],
        "actions": [{"id": "CREDIT-REDUCE", "role_tag": "credit"}],
        "recommended_next_step": "Review and reduce funded exposure.",
        "disclaimer": "human credit review is required before action",
    }
    result.update(changes)
    return result


def test_clean_draft_passes_all_four() -> None:
    report = check_stage7_shapes(json.dumps(_draft()), _slots(), _catalogue())

    assert report.all_passed is True
    assert report.failed_checks == ()
    assert len(report.checks) == 4


def test_fabricated_figure_fails() -> None:
    report = check_stage7_shapes(
        _draft(summary="The recorded value is 1.25 and the fabricated value is 9.99."),
        _slots(),
        _catalogue(),
    )

    assert report.grounding.passed is False
    assert "9.99" in report.grounding.detail or "9.99" in " ".join(report.grounding.failures)


def test_reformatted_slot_value_fails() -> None:
    report = check_stage7_shapes(
        _draft(
            headline=("Debt service coverage is projected to reach the action point on 15/10/2026.")
        ),
        _slots(),
        _catalogue(),
    )

    assert report.grounding.passed is False
    assert "15" in " ".join(report.grounding.failures)


def test_action_outside_catalogue_fails() -> None:
    report = check_stage7_shapes(
        _draft(actions=[{"id": "UNKNOWN-ACTION", "role_tag": "credit"}]),
        _slots(),
        _catalogue(),
    )

    assert report.actions.passed is False
    assert "UNKNOWN-ACTION" in " ".join(report.actions.failures)


def test_wrong_role_tag_fails() -> None:
    report = check_stage7_shapes(
        _draft(actions=[{"id": "CREDIT-REDUCE", "role_tag": "risk"}]),
        _slots(),
        _catalogue(),
    )

    assert report.actions.passed is False
    assert "role_tag" in " ".join(report.actions.failures)


def test_length_above_t6_fails() -> None:
    report = check_stage7_shapes(
        _draft(summary=" ".join("word" for _ in range(1_205))), _slots(), _catalogue()
    )

    assert report.length.passed is False
    assert "1200" in " ".join(report.length.failures)


def test_directive_language_fails() -> None:
    report = check_stage7_shapes(
        _draft(summary="Approve the waiver immediately."), _slots(), _catalogue()
    )

    assert report.grounding.passed is False
    assert report.directive_failures
