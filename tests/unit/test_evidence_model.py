from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_radar.db.repositories.evidence import EvidenceRepository
from covenant_radar.domain.signals.evidence import (
    CERTIFICATE_OVERDUE_TYPE,
    EvidenceFacts,
    SignalEventFacts,
    derive_evidence,
)

pytestmark = pytest.mark.unit

_BORROWER = uuid4()
_FACILITY = uuid4()


def _event(
    event_date: date,
    *,
    event_type: str = "payment_delay",
    event_id: object | None = None,
    facility_id: object | None = _FACILITY,
    family: str = "payment",
) -> SignalEventFacts:
    return SignalEventFacts(
        borrower_id=_BORROWER,
        facility_id=facility_id,
        event_date=event_date,
        family=family,
        event_type=event_type,
        magnitude=Decimal("1"),
        payload={"is_adverse": True},
        event_id=event_id or uuid4(),
    )


def test_identity_groups_events_into_one_item() -> None:
    result = derive_evidence(
        [_event(date(2026, 8, 1), event_id="e-1"), _event(date(2026, 8, 4), event_id="e-2")],
        as_of=date(2026, 8, 4),
    )

    assert len(result) == 1
    assert result[0].first_seen == date(2026, 8, 1)
    assert result[0].last_seen == date(2026, 8, 4)
    assert result[0].source_event_ids == ("e-1", "e-2")


def test_same_day_events_count_once() -> None:
    result = derive_evidence(
        [_event(date(2026, 8, 1), event_id="e-1"), _event(date(2026, 8, 1), event_id="e-2")],
        as_of=date(2026, 8, 1),
        event_window_days=30,
    )

    assert len(result) == 1
    assert result[0].event_count_window == 1
    assert result[0].persistence_days == 1
    assert result[0].source_event_ids == ("e-1", "e-2")


def test_new_type_creates_transient_item() -> None:
    result = derive_evidence(
        [_event(date(2026, 8, 1), event_type="facility_utilisation", event_id="e-1")],
        as_of=date(2026, 8, 1),
    )

    assert result[0].state == "transient"
    assert result[0].counts_toward_pressure is False
    assert result[0].transition is not None
    assert result[0].transition.from_state is None
    assert result[0].transition.to_state == "transient"


def test_repository_has_no_delete() -> None:
    assert not hasattr(EvidenceRepository, "delete")
    assert not hasattr(EvidenceRepository, "delete_all")


def test_deactivated_facility_item_retained() -> None:
    original = derive_evidence(
        [_event(date(2026, 7, 1), event_id="e-1")],
        as_of=date(2026, 7, 1),
    )[0]
    retained = derive_evidence(
        [],
        [EvidenceFacts.from_item(original)],
        as_of=date(2026, 8, 1),
    )

    assert len(retained) == 1
    assert retained[0].facility_id == _FACILITY
    assert retained[0].source_event_ids == ("e-1",)
    assert retained[0].state == "transient"


def test_certificate_overdue_derives_like_any_family() -> None:
    result = derive_evidence(
        [
            _event(
                date(2026, 8, 1),
                event_type=CERTIFICATE_OVERDUE_TYPE,
                event_id="certificate-1",
                facility_id=None,
                family="payment",
            )
        ],
        as_of=date(2026, 8, 1),
    )

    assert len(result) == 1
    assert result[0].evidence_type == CERTIFICATE_OVERDUE_TYPE
    assert result[0].state == "transient"
