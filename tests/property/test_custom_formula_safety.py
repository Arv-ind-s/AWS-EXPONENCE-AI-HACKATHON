"""Property coverage for `T-029`'s security boundary — `parse_custom_formula`
never executes code, for any input Hypothesis can generate (`plan.md §6`'s
`C-31`, `spec §R-07.d`).

`eval` and `exec` — the two primitives that actually run code — are
replaced, for the body of each generated example, with a stand-in that
fails the test the instant either is called, proving by construction,
rather than by inspecting output, that no code path in
`parse_custom_formula` or `Formula.evaluate` ever reaches them. A formula
built from `__import__(...)` is still covered: it is a `Call` node, so the
AST validator refuses it before evaluation ever runs, the same as any
other call — there is no need to (and, since the standard library itself
performs ordinary lazy imports while walking a tree, no way to safely)
guard `__import__` or `compile` themselves without also breaking
`ast.walk`'s and `ast.parse`'s own legitimate use of them.

Generated text mixes known-hostile fragments (`__import__`, attribute
chains that reach a base class's subclasses, `eval`/`exec`/`compile`
calls, comprehensions, lambdas) with allowed line names, arbitrary
Unicode text and arbitrary integers, joined by arithmetic operators and
whitespace — a search space wide enough to include both "looks almost
valid" and "obviously hostile" formulas.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from contextlib import ExitStack
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from covenant_radar.domain.ratios.custom import Formula, FormulaRefused, parse_custom_formula
from covenant_radar.domain.ratios.definitions import FormulaOutcome

pytestmark = pytest.mark.property

_ALLOWED_LINES = frozenset(
    {"total_debt", "tangible_net_worth", "ebitda", "finance_cost", "x", "y", "z"}
)

#: Fragments that would do real damage, or reach something that could, if
#: this module ever handed generated text to `eval`/`exec` — a `Call`, an
#: `Attribute`, a comprehension, a lambda, or an import, each in a shape a
#: careless implementation could plausibly slip past.
_HOSTILE_FRAGMENTS: tuple[str, ...] = (
    "__import__('os').system('id')",
    "().__class__.__bases__[0].__subclasses__()",
    "open('/etc/passwd').read()",
    'eval(\'__import__("os").system("id")\')',
    "exec('import os')",
    "globals()",
    "locals()",
    "getattr(total_debt, '__class__')",
    "[__import__('os') for _ in range(1)]",
    "(lambda: __import__('os').system('id'))()",
    "total_debt.__class__.__bases__",
    "compile('1', '<s>', 'eval')",
    "vars()",
    "(1).__class__",
    "%%x",
    "$(rm -rf /)",
    "`id`",
)


@st.composite
def _hostile_text(draw: st.DrawFn) -> str:
    fragment = st.one_of(
        st.sampled_from(_HOSTILE_FRAGMENTS),
        st.sampled_from(sorted(_ALLOWED_LINES)),
        st.text(max_size=30),
        st.integers(min_value=-10_000, max_value=10_000).map(str),
    )
    parts = draw(st.lists(fragment, min_size=1, max_size=6))
    joiner = draw(st.sampled_from([" + ", " - ", " * ", " / ", " ", "", "; "]))
    return joiner.join(parts)


def _forbidden(name: str) -> Callable[..., Any]:
    def _raise(*args: object, **kwargs: object) -> Any:
        raise AssertionError(
            f"builtins.{name} was invoked while parsing or evaluating a custom formula "
            f"(args={args!r}) — the security boundary in domain/ratios/custom.py was crossed."
        )

    return _raise


@given(text=_hostile_text())
@settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_no_generated_input_ever_executes_code(text: str) -> None:
    with ExitStack() as guards:
        for name in ("eval", "exec"):
            guards.enter_context(patch.object(builtins, name, _forbidden(name)))

        try:
            formula = parse_custom_formula(text, _ALLOWED_LINES)
        except FormulaRefused:
            return
        except RecursionError:
            pytest.fail(
                f"parse_custom_formula raised RecursionError instead of refusing {text!r} "
                "cleanly before evaluation."
            )

        assert isinstance(formula, Formula)
        assert formula.required_lines <= _ALLOWED_LINES

        lines = {name: Decimal("7") for name in formula.required_lines}
        outcome = formula.evaluate(lines)

        assert isinstance(outcome, FormulaOutcome)
        assert outcome.computable in (True, False)
