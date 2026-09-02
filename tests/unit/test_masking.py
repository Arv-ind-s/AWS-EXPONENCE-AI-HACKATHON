"""Unit coverage for the T-090 outbound masking boundary."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from covenant_radar.ai.masking import (
    MASKING_MARKER,
    FieldNotWhitelisted,
    FieldTypeNotAllowed,
    build_outbound,
)

pytestmark = pytest.mark.unit


def _fields() -> dict[str, object]:
    return {
        "ratio_name": "DSCR",
        "value": Decimal("1.25"),
        "threshold": Decimal("1.20"),
        "headroom": Decimal("4.1667"),
        "evidence_type": "financial",
        "evidence_count": 2,
        "materiality": Decimal("0.10"),
        "probability": Decimal("0.40"),
        "confidence": Decimal("0.90"),
        "crossing_date": date(2026, 8, 31),
        "driver_names": ["May"],
        "intervention_text": "Review the facility with the relationship manager.",
        "clause_text": "May reported the covenant under PAN ABCDE1234F.",
    }


def test_unknown_key_raises_and_sends_nothing() -> None:
    fields = _fields()
    fields["borrower_name"] = "Acme Pvt Ltd"

    with pytest.raises(FieldNotWhitelisted, match="borrower_name"):
        build_outbound(fields)


def test_nested_leaves_checked() -> None:
    prompt = build_outbound({"evidence": {"type": "financial", "count": 2}})

    assert '"evidence.count":2' in prompt.content
    assert '"evidence.type":"financial"' in prompt.content

    with pytest.raises(FieldNotWhitelisted, match="account_holder"):
        build_outbound({"evidence": {"type": "financial", "account_holder": "Leaked"}})


def test_names_and_identifiers_masked() -> None:
    prompt = build_outbound(
        {
            "driver_names": ["May"],
            "clause_text": "May signed the undertaking under PAN ABCDE1234F.",
        }
    )

    assert "May" not in prompt.content
    assert "ABCDE1234F" not in prompt.content
    assert "ROLE_DRIVER_1" in prompt.content
    assert "OPAQUE_ID_1" in prompt.content
    assert prompt.token_map["May"] == "ROLE_DRIVER_1"
    assert prompt.token_map["ABCDE1234F"] == "OPAQUE_ID_1"


def test_secret_value_redacted() -> None:
    prompt = build_outbound(
        {"clause_text": "The provider credential is top-secret-value."},
        secret="top-secret-value",
    )

    assert "top-secret-value" not in prompt.content
    assert "[REDACTED]" in prompt.content


def test_wrong_type_refused() -> None:
    with pytest.raises(FieldTypeNotAllowed, match="value"):
        build_outbound({"value": "1.25"})

    with pytest.raises(FieldTypeNotAllowed, match="evidence_count"):
        build_outbound({"evidence_count": True})


def test_token_map_stays_local() -> None:
    prompt = build_outbound(
        {"driver_names": ["Ravi Kumar"], "clause_text": "Ravi Kumar is a driver."}
    )

    assert prompt.token_map["Ravi Kumar"] == "ROLE_DRIVER_1"
    assert "Ravi Kumar" not in prompt.content
    assert "Ravi Kumar" not in repr(prompt)
    with pytest.raises(TypeError):
        prompt.token_map["new-value"] = "ROLE_DRIVER_2"  # type: ignore[index]
    assert prompt.marker == MASKING_MARKER
