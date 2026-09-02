"""Unit tests for the covenant-version value object (`T-031`).

Pure domain-object tests: no session, no engine, no repository — just
`CovenantVersionTerms` validating its own construction, the same way
`domain/ratios`'s own unit tests exercise `RatioDefinition` and
`FormulaOutcome` in isolation.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.covenants.model import (
    CovenantVersionTerms,
    UnknownCovenantDefinition,
)


def _terms(**overrides: object) -> CovenantVersionTerms:
    values: dict[str, object] = {
        "definition_ref": "leverage_ratio",
        "custom_formula": None,
        "threshold": Decimal("2.5"),
        "direction": "max",
        "unit": "x",
        "frequency": "quarterly",
        "test_basis": "standalone",
        "effective_from": date(2026, 1, 1),
    }
    values.update(overrides)
    return CovenantVersionTerms(**values)  # type: ignore[arg-type]


def test_version_is_frozen_value_object() -> None:
    terms = _terms()

    # A validated `CovenantVersionTerms` cannot be mutated afterwards.
    with pytest.raises(FrozenInstanceError):
        terms.threshold = Decimal("3.0")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        terms.direction = "min"  # type: ignore[misc]

    # Two independently built objects with identical fields are equal value
    # objects, not identity-compared references.
    same = _terms()
    assert terms == same
    assert hash(terms) == hash(same)

    changed = _terms(threshold=Decimal("3.0"))
    assert terms != changed


def test_allowed_sets_enforced() -> None:
    # direction, frequency and unit are each a closed set; a value outside
    # it is refused, naming the field and the set.
    with pytest.raises(ValidationError, match="direction"):
        _terms(direction="up")
    with pytest.raises(ValidationError, match="frequency"):
        _terms(frequency="weekly")
    with pytest.raises(ValidationError, match="unit"):
        _terms(unit="bushels")

    # A definition_ref outside the ratio library, with no custom formula
    # given either, is refused with the unknown-definition error
    # (`spec §R-05.b`) — never a generic ValidationError.
    with pytest.raises(UnknownCovenantDefinition):
        _terms(definition_ref="not_a_library_ratio", custom_formula=None)

    # Neither a definition_ref nor a custom formula: also unknown.
    with pytest.raises(UnknownCovenantDefinition):
        _terms(definition_ref=None, custom_formula=None)

    # Both at once is ambiguous, not a valid single definition.
    with pytest.raises(UnknownCovenantDefinition):
        _terms(definition_ref="leverage_ratio", custom_formula="total_debt / tangible_net_worth")

    # A custom formula containing a disallowed construct (a call) is not a
    # valid custom formula, so it is refused the same way as an unknown
    # library code.
    with pytest.raises(UnknownCovenantDefinition):
        _terms(definition_ref=None, custom_formula="__import__('os').system('echo hi')")

    # A custom formula that names no line at all is not a covenant.
    with pytest.raises(UnknownCovenantDefinition):
        _terms(definition_ref=None, custom_formula="42")

    # A syntactically valid custom formula outside the library is accepted.
    valid = _terms(definition_ref=None, custom_formula="total_debt / tangible_net_worth")
    assert valid.definition_ref is None
    assert valid.custom_formula == "total_debt / tangible_net_worth"
