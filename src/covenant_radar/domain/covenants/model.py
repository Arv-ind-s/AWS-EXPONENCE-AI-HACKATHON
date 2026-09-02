"""The covenant version's terms as a frozen value object — `plan.md §5.5`,
`spec §R-05.a`/`R-05.b` (`T-031`).

`register`/`amend` (`services/registry.py`) build one of these from caller
input and validate it completely before any ORM row exists or any statement
reaches the database: a covenant naming an unknown ratio, an out-of-set
direction, frequency or unit, or an internally inconsistent effective range
is refused with nothing written, which is what `R-05.b`'s "unknown-definition
error" requires.

`CovenantVersionTerms` is frozen for the same reason `db/models/covenant.py`'s
trigger freezes a *persisted* `covenant_version`: a reference to "the terms
that were validated" must never be able to drift from what was actually
checked. The trigger protects the row once it exists; this object protects
the value in the moment before it does.

A proposed version identifies its definition one of two ways: `definition_ref`
names a formula the ratio library (`domain/ratios/library.py`) already
implements, or `custom_formula` is bank-authored text outside the library.
`T-029` (`domain/ratios/custom.py`, `C-31`'s `parse_custom_formula`) is
deferred for this build, so the check applied here is deliberately narrower
than that task's full restricted-grammar parser with statement-line name
resolution: it proves a formula is syntactically safe to store — literals,
names, the four arithmetic operators and parentheses, referencing at least
one name — not yet that every name in it is a real statement line, which
needs the chart of accounts this module has no access to.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import ClassVar, Final
from uuid import UUID

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.ratios.library import LIBRARY

DIRECTIONS: Final[frozenset[str]] = frozenset({"min", "max"})
FREQUENCIES: Final[frozenset[str]] = frozenset(
    {"monthly", "quarterly", "half_yearly", "annual", "on_event"}
)

#: The unit vocabulary a covenant's threshold may be expressed in. Derived
#: from the ratio library — the one place a unit is ever defined as data —
#: rather than hand-maintained a second time here, so the two can never
#: drift apart.
UNITS: Final[frozenset[str]] = frozenset(definition.unit for definition in LIBRARY.values())

_TEST_BASIS_MAX_LENGTH: Final[int] = 20
_CUSTOM_FORMULA_MAX_LENGTH: Final[int] = 500
_CUSTOM_FORMULA_MAX_NODES: Final[int] = 200

#: The restricted node vocabulary a syntactically valid custom formula may
#: use: literals, names, arithmetic operators and the implicit grouping
#: `ast.parse` already resolves for parentheses. Anything else — a call, an
#: attribute, a subscript, a comprehension, a lambda, an import, a walrus —
#: is refused because it is not in this tuple.
_ALLOWED_FORMULA_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.USub,
    ast.UAdd,
)


class UnknownCovenantDefinition(ValidationError):
    """The proposed version names neither a library ratio nor a
    syntactically valid custom formula — `spec §R-05.b`'s "unknown-definition
    error", refusing registration with nothing written."""

    code: ClassVar[str] = "unknown_covenant_definition"

    def __init__(self, message: str) -> None:
        super().__init__(message, field="covenant_version.definition")


def is_valid_custom_formula(text: str) -> bool:
    """A syntax-only safety check — see this module's docstring for why it
    stops short of `T-029`'s full grammar. Refuses anything unparseable,
    anything using a node outside the allow-list, a non-numeric literal, a
    boolean literal (a `bool` is an `int` subtype in Python, so it is
    excluded explicitly), and a formula that names no line at all — a
    constant is not a covenant.

    Public so `domain/intake/verify.py` (`T-095`) can check a stage-1
    proposal's custom formula for validity without duplicating this AST
    grammar a second time."""
    if not text or len(text) > _CUSTOM_FORMULA_MAX_LENGTH:
        return False
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        return False
    has_name = False
    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if node_count > _CUSTOM_FORMULA_MAX_NODES:
            return False
        if not isinstance(node, _ALLOWED_FORMULA_NODES):
            return False
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int | float):
                return False
        elif isinstance(node, ast.Name):
            has_name = True
    return has_name


def _clean_definition_field(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            "covenant_version.definition_ref and custom_formula must be text or null.",
            field="covenant_version.definition",
        )
    cleaned = value.strip()
    return cleaned or None


def _validate_definition(
    definition_ref: str | None, custom_formula: str | None
) -> tuple[str | None, str | None]:
    ref = _clean_definition_field(definition_ref)
    formula = _clean_definition_field(custom_formula)
    if ref is not None and formula is not None:
        raise UnknownCovenantDefinition(
            "A covenant version must name exactly one definition: a library ratio "
            "(definition_ref) or a custom formula, not both."
        )
    if ref is not None:
        if ref not in LIBRARY:
            raise UnknownCovenantDefinition(
                f"{ref!r} names no ratio in the library and no custom formula was given."
            )
        return ref, None
    if formula is not None:
        if not is_valid_custom_formula(formula):
            raise UnknownCovenantDefinition(
                "The custom formula is not a valid definition: only literals, "
                "statement-line names, the four arithmetic operators and "
                "parentheses are permitted, and it must reference at least one name."
            )
        return None, formula
    raise UnknownCovenantDefinition(
        "A covenant version must name a library ratio (definition_ref) or a "
        "custom formula; neither was given."
    )


def _validate_membership(field_name: str, value: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValidationError(
            f"covenant_version.{field_name} must be one of: {options}.",
            field=f"covenant_version.{field_name}",
        )
    return value


def _validate_test_basis(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("covenant_version.test_basis is required.", field="test_basis")
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError("covenant_version.test_basis is required.", field="test_basis")
    if len(cleaned) > _TEST_BASIS_MAX_LENGTH:
        raise ValidationError(
            f"covenant_version.test_basis must be at most {_TEST_BASIS_MAX_LENGTH} characters.",
            field="test_basis",
        )
    return cleaned


def _validate_threshold(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValidationError("covenant_version.threshold must be a Decimal.", field="threshold")
    return value


def _validate_effective_range(effective_from: date, effective_to: date | None) -> None:
    if not isinstance(effective_from, date):
        raise ValidationError(
            "covenant_version.effective_from must be a date.", field="effective_from"
        )
    if effective_to is not None:
        if not isinstance(effective_to, date):
            raise ValidationError(
                "covenant_version.effective_to must be a date or null.", field="effective_to"
            )
        if effective_to <= effective_from:
            raise ValidationError(
                "covenant_version.effective_to must be after effective_from.",
                field="effective_to",
            )


def _validate_warning_headroom(value: Decimal | None) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or value < Decimal("0"):
        raise ValidationError(
            "covenant_version.warning_headroom_pct must be a non-negative Decimal or null.",
            field="warning_headroom_pct",
        )


def _validate_non_negative_days(field_name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(
            f"covenant_version.{field_name} must be a non-negative integer or null.",
            field=field_name,
        )


def _validate_source_span(source_document_id: UUID | None, source_span_id: UUID | None) -> None:
    if source_span_id is not None and source_document_id is None:
        raise ValidationError(
            "covenant_version.source_span_id requires source_document_id.",
            field="source_span_id",
        )


@dataclass(frozen=True, slots=True)
class CovenantVersionTerms:
    """One proposed or persisted covenant version's terms — `plan.md
    §5.5`'s `covenant_version` domain columns, validated as a unit and
    independent of any ORM row.

    Exactly one of `definition_ref`/`custom_formula` is retained after
    validation: whichever was supplied is normalised (blank strings become
    `None`), and the pair is checked together, not each in isolation, since
    "outside the library and not a valid custom formula" (`R-05.b`) is a
    property of the pair.
    """

    definition_ref: str | None
    custom_formula: str | None
    threshold: Decimal
    direction: str
    unit: str
    frequency: str
    test_basis: str
    effective_from: date
    effective_to: date | None = None
    warning_headroom_pct: Decimal | None = None
    cure_days: int | None = None
    grace_days: int | None = None
    source_document_id: UUID | None = None
    source_span_id: UUID | None = None

    def __post_init__(self) -> None:
        ref, formula = _validate_definition(self.definition_ref, self.custom_formula)
        object.__setattr__(self, "definition_ref", ref)
        object.__setattr__(self, "custom_formula", formula)
        object.__setattr__(
            self, "direction", _validate_membership("direction", self.direction, DIRECTIONS)
        )
        object.__setattr__(
            self, "frequency", _validate_membership("frequency", self.frequency, FREQUENCIES)
        )
        object.__setattr__(self, "unit", _validate_membership("unit", self.unit, UNITS))
        object.__setattr__(self, "test_basis", _validate_test_basis(self.test_basis))
        object.__setattr__(self, "threshold", _validate_threshold(self.threshold))
        _validate_effective_range(self.effective_from, self.effective_to)
        _validate_warning_headroom(self.warning_headroom_pct)
        _validate_non_negative_days("cure_days", self.cure_days)
        _validate_non_negative_days("grace_days", self.grace_days)
        _validate_source_span(self.source_document_id, self.source_span_id)


__all__ = [
    "DIRECTIONS",
    "FREQUENCIES",
    "UNITS",
    "CovenantVersionTerms",
    "UnknownCovenantDefinition",
    "is_valid_custom_formula",
]
