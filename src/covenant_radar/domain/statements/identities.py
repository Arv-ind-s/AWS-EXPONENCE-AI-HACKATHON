"""The balance-sheet and profit-and-loss identities `Chart.normalise`
checks on every period (`plan.md §5.3`, `T-024`).

An identity is two groups of line codes that must sum to the same total.
Checking it is how a normalisation catches a statement that does not add
up — a torn balance sheet, a mis-keyed P&L line — before any ratio is
computed from it. An identity that fails beyond tolerance marks the whole
period incomplete rather than letting the mismatch pass through silently;
a ratio computed from an inconsistent statement is worse than one flagged
not-computable.

This module has no I/O and no dependency on `chart.py`; `chart.py` calls
into it with the line mapping it has already resolved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final


@dataclass(frozen=True, slots=True)
class Identity:
    """One statement identity: the `left` lines must sum to the `right` lines."""

    name: str
    left: tuple[str, ...]
    right: tuple[str, ...]

    @property
    def required_lines(self) -> frozenset[str]:
        """Every line code this identity needs to be evaluated at all."""
        return frozenset(self.left) | frozenset(self.right)

    @property
    def expression(self) -> str:
        """A human-readable rendering, e.g. ``total_assets = total_liabilities +
        tangible_net_worth``."""
        return f"{' + '.join(self.left)} = {' + '.join(self.right)}"


BALANCE_SHEET_IDENTITY: Final[Identity] = Identity(
    name="balance_sheet_identity",
    left=("total_assets",),
    right=("total_liabilities", "tangible_net_worth"),
)

PROFIT_AND_LOSS_IDENTITY: Final[Identity] = Identity(
    name="profit_and_loss_identity",
    left=("revenue",),
    right=(
        "cost_of_goods_sold",
        "operating_expenses",
        "depreciation",
        "finance_cost",
        "tax_expense",
        "profit_after_tax",
    ),
)

STATEMENT_IDENTITIES: Final[tuple[Identity, ...]] = (
    BALANCE_SHEET_IDENTITY,
    PROFIT_AND_LOSS_IDENTITY,
)


@dataclass(frozen=True, slots=True)
class IdentityCheck:
    """The outcome of testing one `Identity` against a resolved line mapping.

    `within_tolerance` is `None`, not `False`, when the period does not
    carry every line the identity needs — that is a gap to fill, not a
    failed identity, and must not be reported as one.
    """

    name: str
    expression: str
    left_total: Decimal | None
    right_total: Decimal | None
    difference: Decimal | None
    within_tolerance: bool | None

    @property
    def failed(self) -> bool:
        """Whether this identity was evaluated and found outside tolerance."""
        return self.within_tolerance is False


def check_identity(
    identity: Identity,
    lines: Mapping[str, Decimal],
    *,
    tolerance: Decimal,
) -> IdentityCheck:
    """Evaluate one `Identity` against a resolved `{code: value}` mapping."""
    missing = identity.required_lines - lines.keys()
    if missing:
        return IdentityCheck(
            name=identity.name,
            expression=identity.expression,
            left_total=None,
            right_total=None,
            difference=None,
            within_tolerance=None,
        )
    left_total = sum((lines[code] for code in identity.left), start=Decimal(0))
    right_total = sum((lines[code] for code in identity.right), start=Decimal(0))
    difference = left_total - right_total
    return IdentityCheck(
        name=identity.name,
        expression=identity.expression,
        left_total=left_total,
        right_total=right_total,
        difference=difference,
        within_tolerance=abs(difference) <= tolerance,
    )


def check_all(lines: Mapping[str, Decimal], *, tolerance: Decimal) -> tuple[IdentityCheck, ...]:
    """Evaluate every `STATEMENT_IDENTITIES` entry against a resolved line mapping."""
    return tuple(
        check_identity(identity, lines, tolerance=tolerance) for identity in STATEMENT_IDENTITIES
    )


__all__ = [
    "BALANCE_SHEET_IDENTITY",
    "Identity",
    "IdentityCheck",
    "PROFIT_AND_LOSS_IDENTITY",
    "STATEMENT_IDENTITIES",
    "check_all",
    "check_identity",
]
