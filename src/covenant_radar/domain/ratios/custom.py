"""`parse_custom_formula` — `plan.md §6`'s `C-31` (`T-029`).

A bank may test a covenant against a figure the built-in library
(`domain/ratios/library.py`) does not name. The formula it types is text,
typed by a human, and text a caller controls is exactly the shape of an
arbitrary-code-execution surface if it is ever handed to Python's own
`eval` — so this module never runs one. `parse_custom_formula` parses the
text with the standard library's own parser, walks the resulting tree
against a closed allow-list of node types (literals, statement-line names,
`+ - * /`, unary sign, and the grouping `ast.parse` already resolves for
parentheses), and refuses anything else — a call, an attribute, a
subscript, a comprehension, a lambda, an import, a walrus assignment, or a
name outside the caller's own `allowed_lines` — **before** any evaluation
is attempted, naming the disallowed construct and where in the text it sits.

Once a formula has passed every check, its evaluator is a hand-written,
non-recursive-parse tree-walking interpreter that recognises exactly the
same closed node set the validator already proved the tree contains
exclusively — never `eval`, never `compile`. Even a defect in the
validator above could not turn evaluation into code execution, because the
interpreter below has no branch that would run anything but `Decimal`
arithmetic; an unrecognised node type is a defect in this module, not a
door out of it, so it raises rather than falling through to some default
behaviour.

A custom formula that cannot produce a value at evaluation time — division
by zero is the only such case the arithmetic itself can produce — reports
`reasons.NotComputableReason.FORMULA_NOT_COMPUTABLE`, the member
`reasons.py` reserves for exactly this module, never a raised exception:
`C-31`'s evaluator, like every formula in the built-in library, never
raises for a data condition.

This is a security boundary, not a convenience feature (`spec §R-07.d`).
"""

from __future__ import annotations

import ast
import difflib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar, Final

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.ratios.definitions import FormulaOutcome
from covenant_radar.domain.ratios.reasons import NotComputableReason

__all__ = [
    "MAX_FORMULA_DEPTH",
    "MAX_FORMULA_LENGTH",
    "Formula",
    "FormulaRefused",
    "parse_custom_formula",
]

#: A formula longer than this is refused before it is even parsed — long
#: enough for any real covenant expression, short enough to make a parser
#: resource-exhaustion attempt pointless.
MAX_FORMULA_LENGTH: Final[int] = 500

#: The deepest an expression tree may nest — literals and single names sit
#: at depth 1; each additional operator adds one level. A left-associative
#: chain of a hundred terms nests just as deep as a hundred parentheses, so
#: this one limit catches both a pathologically long formula's shape and a
#: deliberately over-nested one, and it is checked with an explicit stack
#: rather than recursion so the check itself can never overflow.
MAX_FORMULA_DEPTH: Final[int] = 20

_MAX_NEAR_MATCHES: Final[int] = 3
_NEAR_MATCH_CUTOFF: Final[float] = 0.6

#: The restricted node vocabulary a syntactically valid formula may use:
#: literals, names, arithmetic binary operators, unary sign, and the
#: implicit grouping `ast.parse` already resolves for parentheses (which
#: never appears as its own node — Python's parser simply nests the
#: enclosed expression one level deeper). Anything else is refused because
#: it is not in this tuple.
_ALLOWED_NODES: Final[tuple[type[ast.AST], ...]] = (
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

#: A human name for every construct a hostile formula is likely to try,
#: used to name it in the refusal (`spec §R-07.d`: the refusal must name
#: the disallowed construct). A node type not listed here still gets a
#: name — its own class name — so no disallowed node is ever refused
#: silently or anonymously.
_CONSTRUCT_NAMES: Final[Mapping[type[ast.AST], str]] = {
    ast.Call: "a function call",
    ast.Attribute: "attribute access",
    ast.Subscript: "a subscript",
    ast.Slice: "a slice",
    ast.ListComp: "a list comprehension",
    ast.SetComp: "a set comprehension",
    ast.DictComp: "a dict comprehension",
    ast.GeneratorExp: "a generator expression",
    ast.Lambda: "a lambda",
    ast.Import: "an import",
    ast.ImportFrom: "an import",
    ast.NamedExpr: "a walrus assignment",
    ast.Compare: "a comparison",
    ast.BoolOp: "a boolean operator",
    ast.IfExp: "a conditional expression",
    ast.Starred: "a starred expression",
    ast.Await: "an await expression",
    ast.Yield: "a yield expression",
    ast.YieldFrom: "a yield expression",
    ast.JoinedStr: "an f-string",
    ast.List: "a list literal",
    ast.Tuple: "a tuple literal",
    ast.Set: "a set literal",
    ast.Dict: "a dict literal",
    ast.FormattedValue: "an f-string expression",
}


class FormulaRefused(ValidationError):
    """`parse_custom_formula` refused `text` before any evaluation was
    attempted — `spec §R-07.d`'s security boundary. `construct` is a short,
    stable label for what was refused (for example ``"a function call"`` or
    ``"unknown_line"``), for a caller that wants to branch on the reason
    rather than parse the message.
    """

    code: ClassVar[str] = "formula_refused"

    def __init__(self, message: str, *, construct: str) -> None:
        super().__init__(message, field="custom_formula")
        self.construct = construct


@dataclass(frozen=True, slots=True)
class Formula:
    """A custom formula that has already passed every safety check —
    `plan.md §6`'s `C-31` return shape.

    `required_lines` names every statement line `evaluate` reads, exactly
    the way `RatioDefinition.required_lines` and `FormulaOutcome.inputs_used`
    do for the built-in library, so a custom formula's not-computable
    reasons and trace rows read identically to a library ratio's.
    """

    text: str
    required_lines: frozenset[str]
    _evaluate: Callable[[Mapping[str, Decimal]], FormulaOutcome] = field(repr=False, compare=False)

    def evaluate(self, lines: Mapping[str, Decimal]) -> FormulaOutcome:
        """Evaluate this formula against one period's `{code: Decimal}`
        line mapping. Never raises: a missing line or a division by zero
        both resolve to a not-computable `FormulaOutcome`, the same
        contract every built-in library formula honours."""
        return self._evaluate(lines)


def _position(node: ast.AST) -> str:
    lineno = getattr(node, "lineno", None)
    col_offset = getattr(node, "col_offset", None)
    if lineno is None or col_offset is None:
        return "an unknown position"
    return f"line {lineno}, column {col_offset + 1}"


def _construct_name(node: ast.AST) -> str:
    return _CONSTRUCT_NAMES.get(type(node), type(node).__name__)


def _refuse_construct(node: ast.AST) -> FormulaRefused:
    name = _construct_name(node)
    return FormulaRefused(
        f"The custom formula uses {name} at {_position(node)}, which is not permitted; "
        "only literals, statement-line names, +, -, *, / and parentheses are allowed.",
        construct=name,
    )


def _refuse_non_numeric_constant(node: ast.Constant) -> FormulaRefused:
    return FormulaRefused(
        f"The custom formula has a non-numeric literal ({node.value!r}) at "
        f"{_position(node)}; only numeric literals are permitted.",
        construct="non_numeric_constant",
    )


def _refuse_unknown_line(node: ast.Name, allowed_lines: frozenset[str]) -> FormulaRefused:
    matches = difflib.get_close_matches(
        node.id, sorted(allowed_lines), n=_MAX_NEAR_MATCHES, cutoff=_NEAR_MATCH_CUTOFF
    )
    position = _position(node)
    if matches:
        suggestion = ", ".join(matches)
        message = (
            f"The custom formula names {node.id!r} at {position}, which is not a known "
            f"statement line; did you mean: {suggestion}?"
        )
    else:
        message = (
            f"The custom formula names {node.id!r} at {position}, which is not a known "
            "statement line, and no similarly named line exists."
        )
    return FormulaRefused(message, construct="unknown_line")


def _refuse_no_line_referenced(text: str) -> FormulaRefused:
    return FormulaRefused(
        f"The custom formula {text!r} references no statement line; a constant is not a covenant.",
        construct="no_line_referenced",
    )


def _refuse_too_long(text: str) -> FormulaRefused:
    return FormulaRefused(
        f"The custom formula is {len(text)} characters long, exceeding the "
        f"{MAX_FORMULA_LENGTH}-character limit.",
        construct="formula_too_long",
    )


def _refuse_too_deep(text: str, depth: int) -> FormulaRefused:
    return FormulaRefused(
        f"The custom formula {text!r} nests {depth} levels deep, exceeding the "
        f"{MAX_FORMULA_DEPTH}-level limit.",
        construct="formula_too_deep",
    )


def _refuse_unparseable(text: str) -> FormulaRefused:
    return FormulaRefused(
        f"The custom formula {text!r} is not a valid expression.",
        construct="syntax_error",
    )


def _parse(text: str) -> ast.Expression:
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as error:
        raise _refuse_unparseable(text) from error
    if not isinstance(tree, ast.Expression):
        raise _refuse_unparseable(text)
    return tree


def _validate_nodes_and_collect_names(
    tree: ast.Expression, allowed_lines: frozenset[str]
) -> frozenset[str]:
    """Walk every node once, refusing the first one that is not on the
    allow-list, not a numeric constant, or names a line outside
    `allowed_lines` — and, for every `ast.Name` that passes, record it as a
    line the formula requires."""
    required: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise _refuse_construct(node)
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise _refuse_non_numeric_constant(node)
            if isinstance(value, float) and not math.isfinite(value):
                raise _refuse_non_numeric_constant(node)
        elif isinstance(node, ast.Name):
            if node.id not in allowed_lines:
                raise _refuse_unknown_line(node, allowed_lines)
            required.add(node.id)
    return frozenset(required)


def _tree_depth(root: ast.AST) -> int:
    """The deepest nesting in `root`, computed with an explicit stack —
    never Python recursion — so a hostile, pathologically nested tree can
    be measured, and refused, without risking a `RecursionError` in the
    measurement itself."""
    max_depth = 0
    stack: list[tuple[ast.AST, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            max_depth = depth
        for child in ast.iter_child_nodes(node):
            stack.append((child, depth + 1))
    return max_depth


def _decimal_constant(value: object) -> Decimal:
    # Reachable only for a `Constant` node `_validate_nodes_and_collect_names`
    # already proved is a non-boolean `int` or `float`.
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise AssertionError(
        f"Unreachable: {value!r} should have been refused as a non-numeric constant."
    )


def _evaluate_node(node: ast.expr, values: Mapping[str, Decimal]) -> Decimal:
    """Evaluate one already-validated expression node against `values`.
    Handles exactly the node types the allow-list admits; anything else is
    a defect in the validator, surfaced as an assertion rather than
    silently falling through to `eval`-like behaviour.
    """
    if isinstance(node, ast.Constant):
        return _decimal_constant(node.value)
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_node(node.operand, values)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise AssertionError(f"Unreachable unary operator: {type(node.op).__name__}.")
    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, values)
        right = _evaluate_node(node.right, values)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError
            return left / right
        raise AssertionError(f"Unreachable binary operator: {type(node.op).__name__}.")
    raise AssertionError(
        f"Unreachable: {type(node).__name__} should have been refused at parse time."
    )


def _build_evaluator(
    tree: ast.Expression, required_lines: frozenset[str]
) -> Callable[[Mapping[str, Decimal]], FormulaOutcome]:
    body = tree.body

    def evaluate(lines: Mapping[str, Decimal]) -> FormulaOutcome:
        missing = sorted(name for name in required_lines if name not in lines)
        if missing:
            return FormulaOutcome(
                value=None,
                computable=False,
                reason=NotComputableReason.MISSING_LINE,
                inputs_used={},
                reason_context={"names": ", ".join(missing)},
            )
        inputs_used = {name: lines[name] for name in required_lines}
        try:
            value = _evaluate_node(body, inputs_used)
        except ArithmeticError:
            return FormulaOutcome(
                value=None,
                computable=False,
                reason=NotComputableReason.FORMULA_NOT_COMPUTABLE,
                inputs_used=inputs_used,
                reason_context={"detail": "division by zero"},
            )
        return FormulaOutcome(value=value, computable=True, reason=None, inputs_used=inputs_used)

    return evaluate


def parse_custom_formula(text: str, allowed_lines: frozenset[str]) -> Formula:
    """Parse and validate `text` as a bank-authored custom formula
    (`plan.md §6`'s `C-31`), refusing it — before any evaluation is
    attempted — the moment it uses anything outside a restricted syntax
    tree: literals, names from `allowed_lines`, the four arithmetic
    operators, unary sign, and parentheses.

    Returns a `Formula` whose `evaluate` reads `values` and never raises;
    the checks below are what let it make that promise.
    """
    if not text or len(text) > MAX_FORMULA_LENGTH:
        raise _refuse_too_long(text)

    tree = _parse(text)
    required_lines = _validate_nodes_and_collect_names(tree, allowed_lines)
    if not required_lines:
        raise _refuse_no_line_referenced(text)

    depth = _tree_depth(tree)
    if depth > MAX_FORMULA_DEPTH:
        raise _refuse_too_deep(text, depth)

    evaluator = _build_evaluator(tree, required_lines)
    return Formula(text=text, required_lines=required_lines, _evaluate=evaluator)
