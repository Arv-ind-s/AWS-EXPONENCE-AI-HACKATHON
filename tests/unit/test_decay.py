"""Unit coverage for evidence decay and retention semantics."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_radar.domain.signals.decay import (
    DecayThresholds,
    apply_decay,
    decay_factor,
    score_decay,
)
from covenant_radar.domain.signals.evidence import EvidenceFacts, SignalEventFacts

pytestmark = pytest.mark.unit

_AS_OF = date(2026, 8, 31)
_BORROWER = uuid4()
_FACILITY = uuid4()


class _DecayStore:
    def __init__(self, rate: Decimal = Decimal("0.90")) -> None:
        self.rate = rate
        self.requested: list[str] = []

    def get(self, name: str) -> dict[str, Decimal]:
        self.requested.append(name)
        return {"decay_rate": self.rate, "display_floor": Decimal("0.10")}


def _item(
    last_seen: date,
    *,
    state: str = "transient",
    materiality_pct: Decimal | None = Decimal("10"),
    counts_toward_pressure: bool = True,
    source_event_ids: tuple[str, ...] = ("original",),
) -> EvidenceFacts:
    return EvidenceFacts(
        id=uuid4(),
        borrower_id=_BORROWER,
        facility_id=_FACILITY,
        family="payment",
        evidence_type="payment_delay",
        first_seen=last_seen,
        last_seen=last_seen,
        persistence_days=1,
        event_count_window=1,
        materiality_pct=materiality_pct,
        state=state,
        counts_toward_pressure=counts_toward_pressure,
        source_event_ids=source_event_ids,
    )


def _event(event_date: date, event_id: str = "new-event") -> SignalEventFacts:
    return SignalEventFacts(
        borrower_id=_BORROWER,
        facility_id=_FACILITY,
        event_date=event_date,
        family="payment",
        event_type="payment_delay",
        magnitude=Decimal("1"),
        payload={"is_adverse": True},
        event_id=event_id,
    )


def test_zero_days_factor_one() -> None:
    assert decay_factor(0, Decimal("0.90")) == Decimal("1")


def test_decayed_item_still_returned() -> None:
    item = _item(_AS_OF - timedelta(days=10))
    result = apply_decay(
        [item],
        _AS_OF,
        DecayThresholds(Decimal("0.50"), display_floor=Decimal("0.10")),
    )

    assert len(result) == 1
    assert result[0].decay_factor == Decimal("0.0009765625")
    assert result[0].below_display_floor is True
    assert result[0].visible is True
    assert result[0].included is True
    assert result[0].state == "transient"
    assert result[0].decay_state == "decaying"
    assert result[0].pressure_contribution < Decimal("0.001")


def test_live_run_does_not_decay() -> None:
    item = _item(_AS_OF, state="sustained")

    result = score_decay(item, _AS_OF, DecayThresholds(Decimal("0.25")))

    assert result.decay_factor == Decimal("1")
    assert result.days_since_last_seen == 0
    assert result.pressure_contribution == Decimal("0.1")
    assert result.transition is None


def test_new_event_resets_and_records_transition() -> None:
    item = _item(_AS_OF - timedelta(days=10))
    result = score_decay(
        item,
        _AS_OF,
        DecayThresholds(Decimal("0.50")),
        events=[_event(_AS_OF, "reset-event")],
    )

    assert result.decay_factor == Decimal("1")
    assert result.reset is True
    assert result.last_seen == _AS_OF
    assert "reset" in (result.transition.rule if result.transition else "")
    assert result.transition is not None
    assert result.transition.from_state == "transient"
    assert result.transition.to_state == "transient"
    assert "reset-event" in result.source_event_ids


def test_rate_read_from_configuration() -> None:
    store = _DecayStore(Decimal("0.80"))
    result = score_decay(_item(_AS_OF - timedelta(days=2)), _AS_OF, store)

    assert store.requested == ["T3"]
    assert result.thresholds is not None
    assert result.thresholds.decay_rate == Decimal("0.80")
    assert result.decay_factor == Decimal("0.64")
