"""Unit coverage for append-only evidence contradiction resolution."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from covenant_radar.domain.signals.evidence import EvidenceFacts, SignalEventFacts
from covenant_radar.domain.signals.supersession import resolve_supersession

pytestmark = pytest.mark.unit

_BORROWER = uuid4()
_FACILITY = uuid4()


def _item(
    evidence_type: str,
    *,
    item_id: UUID | None = None,
    first_seen: date = date(2026, 8, 1),
    last_seen: date | None = None,
    state: str = "sustained",
    source_id: str = "prior",
) -> EvidenceFacts:
    return EvidenceFacts(
        id=item_id or uuid4(),
        borrower_id=_BORROWER,
        facility_id=_FACILITY,
        family="payment",
        evidence_type=evidence_type,
        first_seen=first_seen,
        last_seen=last_seen or first_seen,
        persistence_days=14,
        event_count_window=3,
        materiality_pct=Decimal("10"),
        state=state,
        counts_toward_pressure=True,
        source_event_ids=(source_id,),
    )


def _event(event_date: date, event_type: str, event_id: str) -> SignalEventFacts:
    return SignalEventFacts(
        borrower_id=_BORROWER,
        facility_id=_FACILITY,
        event_date=event_date,
        family="payment",
        event_type=event_type,
        magnitude=Decimal("0"),
        payload={"is_adverse": event_type == "payment_delay"},
        event_id=event_id,
    )


def test_contradiction_links_both_sides() -> None:
    prior = _item("payment_delay")
    batch = resolve_supersession(
        [prior], [_event(date(2026, 8, 2), "payment_received", "received-1")]
    )

    old = next(item for item in batch if item.id == prior.id)
    new = next(item for item in batch if item.evidence_type == "payment_received")
    assert old.state == "superseded"
    assert old.superseded_by_id == new.id
    assert new.supersedes_id == old.id
    assert len(batch.revisions) == 1
    assert batch.revisions[0].transition.from_state == "sustained"
    assert batch.revisions[0].transition.to_state == "superseded"


def test_out_of_order_resolved_by_event_date() -> None:
    prior = _item("payment_delay", last_seen=date(2026, 8, 10))
    batch = resolve_supersession(
        [prior], [_event(date(2026, 8, 5), "payment_received", "late-received")]
    )

    delay = next(item for item in batch if item.id == prior.id)
    received = next(item for item in batch if item.evidence_type == "payment_received")
    assert delay.state == "sustained"
    assert received.state == "superseded"
    assert delay.supersedes_id == received.id
    assert received.superseded_by_id == delay.id
    assert batch.revisions[0].occurred_on == date(2026, 8, 10)


def test_chain_terminates_and_is_bidirectional() -> None:
    first = _item("payment_delay")
    second_batch = resolve_supersession(
        [first], [_event(date(2026, 8, 2), "payment_received", "received-1")]
    )
    third_batch = resolve_supersession(
        second_batch.items, [_event(date(2026, 8, 3), "payment_delay", "delay-2")]
    )

    by_type = {(item.evidence_type, item.source_event_ids): item for item in third_batch}
    first_row = next(item for item in third_batch if item.id == first.id)
    second_row = next(item for item in third_batch if item.evidence_type == "payment_received")
    third_row = next(item for item in third_batch if "delay-2" in item.source_event_ids)
    assert first_row.superseded_by_id == second_row.id
    assert second_row.supersedes_id == first_row.id
    assert second_row.superseded_by_id == third_row.id
    assert third_row.supersedes_id == second_row.id
    assert third_row.superseded_by_id is None
    assert len({item.id for item in by_type.values()}) == 3


def test_repeated_contradiction_creates_new_item() -> None:
    first = _item("payment_delay")
    received_batch = resolve_supersession(
        [first], [_event(date(2026, 8, 2), "payment_received", "received-1")]
    )
    delay_batch = resolve_supersession(
        received_batch.items, [_event(date(2026, 8, 3), "payment_delay", "delay-2")]
    )

    delay_items = [item for item in delay_batch if item.evidence_type == "payment_delay"]
    assert len(delay_items) == 2
    assert first.id in {item.id for item in delay_items}
    new_delay = next(item for item in delay_items if item.id != first.id)
    assert new_delay.id != first.id
    assert new_delay.supersedes_id is not None
    assert new_delay.supersedes_id != first.id
    assert next(item for item in delay_items if item.id == first.id).state == "superseded"
