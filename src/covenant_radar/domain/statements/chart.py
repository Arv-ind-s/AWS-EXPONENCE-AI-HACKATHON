"""The normalised statement chart of accounts (`plan.md §5.3`, `T-024`).

Every ratio the library computes reads lines from this chart, never a
customer's own column names, so a ratio's correctness never depends on how
one bank happened to label its extract. `Chart.load` parses the seeded
taxonomy (`db/seed/data/statement_lines.json`) into `StatementLineDefinition`
rows; `Chart.normalise` turns one period's raw values, keyed by chart code,
into a validated `{code: Decimal}` mapping in ₹ crore — the exact shape
`plan.md §6`'s `C-30 compute_ratio` consumes as `lines`.

**Sign convention is the trap.** Indian extracts variously present
liabilities positive, expenses negative and both, sometimes within the same
document. The convention is declared once, per line, in the chart itself
(`sign_convention`), and applied here, at normalisation — never inferred
from the sign of an incoming number. A value that violates its line's
convention is flagged for review rather than sign-flipped: guessing a sign
is how a ratio becomes wrong quietly, and a flagged line is excluded from
the resolved mapping rather than trusted.

This module performs no database or network I/O beyond reading its own
packaged JSON file; it has no dependency on any ORM model.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Final

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.statements.identities import IdentityCheck, check_all

DEFAULT_CHART_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "db" / "seed" / "data" / "statement_lines.json"
)

STATEMENTS: Final[frozenset[str]] = frozenset(
    {"profit_and_loss", "balance_sheet", "cash_flow", "ownership"}
)
SIGN_CONVENTIONS: Final[frozenset[str]] = frozenset({"positive", "signed", "percentage"})

#: Conventions under which a negative raw value is never trusted as-is.
_NEGATIVE_FORBIDDEN_CONVENTIONS: Final[frozenset[str]] = frozenset({"positive", "percentage"})

#: How a raw extract may state its amounts, and the factor to ₹ crore.
_UNIT_FACTORS: Final[Mapping[str, Decimal]] = {
    "actual": Decimal(1) / Decimal(10_000_000),
    "thousand": Decimal(1) / Decimal(10_000),
    "lakh": Decimal(1) / Decimal(100),
    "crore": Decimal(1),
}

DEFAULT_IDENTITY_TOLERANCE: Final[Decimal] = Decimal("0.01")

_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_CODE_LENGTH: Final[int] = 100
_EXPECTED_ROW_KEYS: Final[frozenset[str]] = frozenset(
    {"code", "name", "statement", "sign_convention", "is_derived", "derivation"}
)
_FLAG_NEGATIVE_ON_FORBIDDEN_SIGN: Final[str] = "negative_value_on_forbidden_sign_line"


class ChartError(ValidationError):
    """The statement chart source, or a value normalised against it, is invalid."""


@dataclass(frozen=True, slots=True)
class DerivationTerm:
    """One signed line reference inside a derived line's formula."""

    code: str
    sign: int


@dataclass(frozen=True, slots=True)
class StatementLineDefinition:
    """One line in the normalised chart (`plan.md §5.3`)."""

    code: str
    name: str
    statement: str
    sign_convention: str
    is_derived: bool
    derivation: tuple[DerivationTerm, ...] | None


@dataclass(frozen=True, slots=True)
class LineDiscrepancy:
    """A derived line supplied directly whose value disagrees with its
    computed derivation beyond tolerance. The supplied value still wins;
    this is only ever a report, never a silent reconciliation."""

    code: str
    supplied: Decimal
    derived: Decimal
    difference: Decimal


@dataclass(frozen=True, slots=True)
class LineFlag:
    """A raw value rejected rather than trusted, and why."""

    code: str
    reason: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class NormalisationResult:
    """The outcome of normalising one period's raw statement extract.

    `lines` is exactly the shape `C-30`'s `compute_ratio` reads: every
    value in ₹ crore, keyed by chart code, with an absent line simply
    missing from the mapping — never present as zero.
    """

    lines: Mapping[str, Decimal]
    discrepancies: tuple[LineDiscrepancy, ...]
    flags: tuple[LineFlag, ...]
    identity_checks: tuple[IdentityCheck, ...]
    is_complete: bool

    @property
    def failing_identities(self) -> tuple[IdentityCheck, ...]:
        """Every identity that was evaluated and found outside tolerance."""
        return tuple(check for check in self.identity_checks if check.failed)


def _forbids_negative(sign_convention: str) -> bool:
    return sign_convention in _NEGATIVE_FORBIDDEN_CONVENTIONS


def _to_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ChartError(
            f"{field} must be a decimal, integer or numeric string, not a boolean.", field=field
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as error:
            raise ChartError(f"{field} is not a valid decimal: {value!r}.", field=field) from error
    elif isinstance(value, float):
        raise ChartError(
            f"{field} was supplied as a float ({value!r}); statement amounts must be a "
            "Decimal, an int, or a numeric string — a float cannot represent money exactly.",
            field=field,
        )
    else:
        raise ChartError(
            f"{field} must be a decimal, integer or numeric string, not {type(value).__name__}.",
            field=field,
        )
    if not result.is_finite():
        raise ChartError(f"{field} must be a finite value, got {result!r}.", field=field)
    return result


def _convert_unit(amount: Decimal, unit: str, *, field: str) -> Decimal:
    factor = _UNIT_FACTORS.get(unit)
    if factor is None:
        raise ChartError(
            f"{field} has unknown unit {unit!r}; expected one of {sorted(_UNIT_FACTORS)}.",
            field=field,
        )
    return amount * factor


_ALLOWED_DERIVATION_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Expression,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Name,
    ast.Load,
)


def _parse_derivation(text: str, *, field: str) -> tuple[DerivationTerm, ...]:
    """Parse a derivation such as ``"ebit + depreciation"`` into signed terms.

    Restricted to line-code names joined by `+`/`-`: no calls, literals,
    attribute access or any other construct is accepted — this is trusted
    seed data, but a chart of accounts is not the place to start trusting
    an expression parser with more than it needs.
    """
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ChartError(
            f"{field} is not a valid derivation expression: {text!r}.", field=field
        ) from error
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_DERIVATION_NODES):
            raise ChartError(
                f"{field} contains a disallowed construct ({type(node).__name__}) in "
                f"{text!r}; only line codes joined by + or - are permitted.",
                field=field,
            )
    terms: list[DerivationTerm] = []
    _collect_derivation_terms(tree.body, sign=1, terms=terms, field=field, text=text)
    codes = [term.code for term in terms]
    if len(set(codes)) != len(codes):
        raise ChartError(f"{field} references a line more than once: {text!r}.", field=field)
    return tuple(terms)


def _collect_derivation_terms(
    node: ast.expr, *, sign: int, terms: list[DerivationTerm], field: str, text: str
) -> None:
    if isinstance(node, ast.Name):
        terms.append(DerivationTerm(code=node.id, sign=sign))
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Sub):
        right_sign = sign if isinstance(node.op, ast.Add) else -sign
        _collect_derivation_terms(node.left, sign=sign, terms=terms, field=field, text=text)
        _collect_derivation_terms(node.right, sign=right_sign, terms=terms, field=field, text=text)
        return
    raise ChartError(f"{field} is not a valid derivation expression: {text!r}.", field=field)


def _parse_row(row: object, *, path: Path, position: int) -> StatementLineDefinition:
    location = f"{path} row {position}"
    if not isinstance(row, dict):
        raise ChartError(f"{location} must be a JSON object.", field="statement_lines")
    keys = set(row)
    if keys != _EXPECTED_ROW_KEYS:
        missing = ", ".join(sorted(_EXPECTED_ROW_KEYS - keys))
        extra = ", ".join(sorted(keys - _EXPECTED_ROW_KEYS))
        details = [f"missing {missing}" if missing else "", f"unknown {extra}" if extra else ""]
        parts = [detail for detail in details if detail]
        raise ChartError(
            f"{location} has invalid fields ({'; '.join(parts)}).", field="statement_lines"
        )

    code = row["code"]
    valid_code = (
        isinstance(code, str) and len(code) <= _MAX_CODE_LENGTH and _CODE_PATTERN.fullmatch(code)
    )
    if not valid_code:
        raise ChartError(f"{location} has an invalid code {code!r}.", field="statement_lines.code")

    name = row["name"]
    if not isinstance(name, str) or not name.strip():
        raise ChartError(
            f"{location}.name must be a non-empty string.", field=f"statement_lines.{code}.name"
        )

    statement = row["statement"]
    if statement not in STATEMENTS:
        raise ChartError(
            f"{location}.statement must be one of {sorted(STATEMENTS)}, got {statement!r}.",
            field=f"statement_lines.{code}.statement",
        )

    sign_convention = row["sign_convention"]
    if sign_convention not in SIGN_CONVENTIONS:
        raise ChartError(
            f"{location}.sign_convention must be one of {sorted(SIGN_CONVENTIONS)}, "
            f"got {sign_convention!r}.",
            field=f"statement_lines.{code}.sign_convention",
        )

    is_derived = row["is_derived"]
    if not isinstance(is_derived, bool):
        raise ChartError(
            f"{location}.is_derived must be a boolean.", field=f"statement_lines.{code}.is_derived"
        )

    derivation_text = row["derivation"]
    derivation: tuple[DerivationTerm, ...] | None
    if is_derived:
        if not isinstance(derivation_text, str) or not derivation_text.strip():
            raise ChartError(
                f"{location}.derivation must be a non-empty string when is_derived is true.",
                field=f"statement_lines.{code}.derivation",
            )
        derivation = _parse_derivation(derivation_text, field=f"statement_lines.{code}.derivation")
    else:
        if derivation_text is not None:
            raise ChartError(
                f"{location}.derivation must be null when is_derived is false.",
                field=f"statement_lines.{code}.derivation",
            )
        derivation = None

    return StatementLineDefinition(
        code=code,
        name=name,
        statement=statement,
        sign_convention=sign_convention,
        is_derived=is_derived,
        derivation=derivation,
    )


def _topological_derivation_order(
    by_code: Mapping[str, StatementLineDefinition],
) -> tuple[str, ...]:
    """Order every derived line so each appears after every line it reads."""
    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(code: str) -> None:
        if code in visited:
            return
        definition = by_code[code]
        if not definition.is_derived or definition.derivation is None:
            visited.add(code)
            return
        if code in visiting:
            raise ChartError(
                f"Circular derivation detected involving {code!r}.", field="statement_lines"
            )
        visiting.add(code)
        for term in definition.derivation:
            visit(term.code)
        visiting.discard(code)
        visited.add(code)
        order.append(code)

    for code in by_code:
        visit(code)
    return tuple(order)


class Chart:
    """The normalised statement chart of accounts, loaded once and immutable
    thereafter (`plan.md §5.3`)."""

    def __init__(
        self, definitions: Mapping[str, StatementLineDefinition], *, taxonomy_version: str
    ) -> None:
        self._by_code: dict[str, StatementLineDefinition] = dict(definitions)
        for definition in self._by_code.values():
            if definition.derivation is None:
                continue
            for term in definition.derivation:
                if term.code == definition.code:
                    raise ChartError(
                        f"Line {definition.code!r} cannot derive from itself.",
                        field="statement_lines",
                    )
                if term.code not in self._by_code:
                    raise ChartError(
                        f"Line {definition.code!r} derives from unknown line {term.code!r}.",
                        field=f"statement_lines.{definition.code}.derivation",
                    )
        self.taxonomy_version = taxonomy_version
        self._derivation_order = _topological_derivation_order(self._by_code)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CHART_PATH) -> Chart:
        """Read, validate and parse the chart from its packaged JSON file."""
        file_path = Path(path)
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ChartError(
                f"Statement chart cannot be read at {file_path}: {error}.", field="chart.file"
            ) from error
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise ChartError(
                f"Malformed statement chart at {file_path}, line {error.lineno}, "
                f"column {error.colno}: {error.msg}.",
                field="chart.file",
            ) from error
        if not isinstance(raw, dict):
            raise ChartError(
                f"Statement chart {file_path} must contain a JSON object.", field="chart.file"
            )
        if set(raw) != {"taxonomy_version", "statement_lines"}:
            raise ChartError(
                f"Statement chart {file_path} must contain exactly 'taxonomy_version' "
                "and 'statement_lines'.",
                field="chart.file",
            )

        taxonomy_version = raw["taxonomy_version"]
        if not isinstance(taxonomy_version, str) or not taxonomy_version.strip():
            raise ChartError(
                f"{file_path}: taxonomy_version must be a non-empty string.",
                field="taxonomy_version",
            )

        rows = raw["statement_lines"]
        if not isinstance(rows, list) or not rows:
            raise ChartError(
                f"{file_path}: statement_lines must be a non-empty array.", field="statement_lines"
            )

        definitions: dict[str, StatementLineDefinition] = {}
        for position, row in enumerate(rows, start=1):
            definition = _parse_row(row, path=file_path, position=position)
            if definition.code in definitions:
                raise ChartError(
                    f"Duplicate line code {definition.code!r} in {file_path}.",
                    field="statement_lines",
                )
            definitions[definition.code] = definition

        return cls(definitions, taxonomy_version=taxonomy_version)

    def __contains__(self, code: object) -> bool:
        return code in self._by_code

    def __iter__(self) -> Iterator[StatementLineDefinition]:
        return iter(self._by_code.values())

    def __len__(self) -> int:
        return len(self._by_code)

    @property
    def codes(self) -> frozenset[str]:
        """Every line code the chart defines."""
        return frozenset(self._by_code)

    def get(self, code: str) -> StatementLineDefinition:
        """Return one line's definition, or raise `ChartError` naming it."""
        try:
            return self._by_code[code]
        except KeyError as error:
            raise ChartError(f"Unknown statement line {code!r}.", field="code") from error

    def normalise(
        self,
        raw: Mapping[str, object],
        unit: str = "crore",
        *,
        tolerance: Decimal = DEFAULT_IDENTITY_TOLERANCE,
    ) -> NormalisationResult:
        """Normalise one period's raw extract, keyed by chart code, to ₹ crore.

        `unit` names the single denomination the whole extract is stated
        in (``"actual"``, ``"thousand"``, ``"lakh"`` or ``"crore"``,
        default ``"crore"`` — already normalised, no conversion applied).

        A line the chart does not define is refused rather than silently
        ignored: by the time an extract reaches this call, its columns
        have already been mapped onto chart codes upstream, so an unknown
        code here is a caller defect, not a data-quality issue to absorb.
        """
        unknown = raw.keys() - self._by_code.keys()
        if unknown:
            raise ChartError(
                f"Unknown statement line code(s) in raw extract: {', '.join(sorted(unknown))}.",
                field="raw",
            )

        resolved: dict[str, Decimal] = {}
        flags: list[LineFlag] = []

        for code, definition in self._by_code.items():
            if definition.is_derived or code not in raw or raw[code] is None:
                continue
            amount = self._resolve_raw_value(code, raw[code], unit)
            if _forbids_negative(definition.sign_convention) and amount < 0:
                flags.append(
                    LineFlag(code=code, reason=_FLAG_NEGATIVE_ON_FORBIDDEN_SIGN, value=amount)
                )
                continue
            resolved[code] = amount

        discrepancies: list[LineDiscrepancy] = []
        for code in self._derivation_order:
            definition = self._by_code[code]
            derivation = definition.derivation
            if derivation is None:
                # Unreachable: `_topological_derivation_order` only yields
                # codes whose definition carries a derivation.
                continue

            derived_value: Decimal | None = None
            if all(term.code in resolved for term in derivation):
                derived_value = sum(
                    (resolved[term.code] * term.sign for term in derivation), start=Decimal(0)
                )

            supplied_raw = raw.get(code)
            if supplied_raw is None:
                if derived_value is not None:
                    resolved[code] = derived_value
                continue

            supplied = self._resolve_raw_value(code, supplied_raw, unit)
            if _forbids_negative(definition.sign_convention) and supplied < 0:
                flags.append(
                    LineFlag(code=code, reason=_FLAG_NEGATIVE_ON_FORBIDDEN_SIGN, value=supplied)
                )
                continue
            resolved[code] = supplied
            if derived_value is not None and abs(supplied - derived_value) > tolerance:
                discrepancies.append(
                    LineDiscrepancy(
                        code=code,
                        supplied=supplied,
                        derived=derived_value,
                        difference=supplied - derived_value,
                    )
                )

        identity_checks = check_all(resolved, tolerance=tolerance)
        is_complete = not any(check.failed for check in identity_checks)

        return NormalisationResult(
            lines=dict(resolved),
            discrepancies=tuple(discrepancies),
            flags=tuple(flags),
            identity_checks=identity_checks,
            is_complete=is_complete,
        )

    def _resolve_raw_value(self, code: str, value: object, unit: str) -> Decimal:
        amount = _to_decimal(value, field=code)
        return _convert_unit(amount, unit, field=code)


@lru_cache(maxsize=1)
def default_chart() -> Chart:
    """Return the process-cached chart loaded from the packaged taxonomy."""
    return Chart.load()


__all__ = [
    "DEFAULT_CHART_PATH",
    "DEFAULT_IDENTITY_TOLERANCE",
    "SIGN_CONVENTIONS",
    "STATEMENTS",
    "Chart",
    "ChartError",
    "DerivationTerm",
    "LineDiscrepancy",
    "LineFlag",
    "NormalisationResult",
    "StatementLineDefinition",
    "default_chart",
]
