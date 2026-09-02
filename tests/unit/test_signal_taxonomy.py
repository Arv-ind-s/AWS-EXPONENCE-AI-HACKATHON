"""Unit checks for the closed T-042 signal taxonomy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_radar.domain.signals import (
    EVENT_TYPES,
    FAMILIES,
    FAMILY_EVENT_TYPES,
    FAMILY_UNITS,
    REQUIRED_PAYLOAD_FIELDS,
    SignalEvent,
    SignalTaxonomyError,
)

pytestmark = pytest.mark.unit


def test_seven_families_closed() -> None:
    assert FAMILIES == (
        "account_activity",
        "payment",
        "utilisation",
        "treasury",
        "concentration",
        "industry",
        "news",
    )
    assert set(FAMILY_EVENT_TYPES) == set(FAMILIES)
    assert set(FAMILY_EVENT_TYPES.values()) == set(EVENT_TYPES)
    assert len(EVENT_TYPES) == 7

    with pytest.raises(SignalTaxonomyError, match="Unknown signal family"):
        SignalEvent(
            borrower_id=uuid4(),
            facility_id=None,
            event_date=date(2026, 1, 1),
            family="unknown",
            event_type="unknown",
            magnitude=Decimal("1"),
            unit="score",
            payload={"value": 1, "is_adverse": False},
        )


def test_required_payload_fields_per_type() -> None:
    expected_value_fields = {
        "account_activity": "activity_change_pct",
        "payment": "days_past_due",
        "utilisation": "utilisation_pct",
        "treasury": "cash_outflow_ratio",
        "concentration": "top_group_exposure_pct",
        "industry": "industry_stress_score",
        "news": "news_risk_score",
    }
    for family, value_field in expected_value_fields.items():
        assert REQUIRED_PAYLOAD_FIELDS[family] == (value_field, "is_adverse")
        assert FAMILY_UNITS[family]

    with pytest.raises(SignalTaxonomyError, match="missing required field"):
        SignalEvent(
            borrower_id=uuid4(),
            facility_id=None,
            event_date=date(2026, 1, 1),
            family="payment",
            event_type="payment_delay",
            magnitude=Decimal("1"),
            unit="days",
            payload={"is_adverse": False},
        )


def test_content_hash_stable_across_sources() -> None:
    borrower_id = uuid4()
    facility_id = uuid4()
    common = {
        "borrower_id": borrower_id,
        "facility_id": facility_id,
        "event_date": date(2026, 1, 1),
        "family": "treasury",
        "event_type": "treasury_outflow",
        "magnitude": Decimal("0.2500"),
        "unit": "ratio",
        "payload": {"cash_outflow_ratio": Decimal("0.2500"), "is_adverse": True},
    }
    first = SignalEvent(**common, source_id=uuid4())
    second = SignalEvent(**common, source_id=uuid4())

    assert first.hash == second.hash
    assert len(first.hash) == 64
