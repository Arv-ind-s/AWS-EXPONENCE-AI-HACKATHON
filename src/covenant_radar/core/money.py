"""Exact decimal money tied to its unit; floating point is refused outright.

A `float` cannot represent most rupee-and-paise amounts exactly, and the
rounding error compounds silently through every ratio and forecast built on
top of it — that makes floating-point money a defect rather than a style
preference. `Money` only accepts `Decimal`, refuses to combine amounts
recorded in different units, and quantises only for display, never in
storage or in the middle of a calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_DISPLAY_QUANTUM = Decimal("0.01")
_RUPEE_SYMBOL = "₹"
_DEFAULT_UNIT = "INR"


def _is_scalar(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, Decimal | int)


@dataclass(frozen=True, slots=True)
class Money:
    """An amount of money in a recorded unit (a currency, or a denomination
    such as "lakh" or "crore" that a source document quoted amounts in)."""

    amount: Decimal
    unit: str = _DEFAULT_UNIT

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(
                "Money requires a Decimal amount, not "
                f"{type(self.amount).__name__} ({self.amount!r}); "
                "construct it as Money(Decimal('...'), unit)."
            )
        if not self.amount.is_finite():
            raise ValueError(f"Money requires a finite amount, got {self.amount!r}.")
        if not self.unit:
            raise ValueError("Money requires a non-empty unit.")

    def _same_unit(self, other: Money) -> None:
        if self.unit != other.unit:
            raise ValueError(
                f"Cannot combine money recorded in different units: "
                f"{self.unit!r} and {other.unit!r}."
            )

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._same_unit(other)
        return Money(self.amount + other.amount, self.unit)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._same_unit(other)
        return Money(self.amount - other.amount, self.unit)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.unit)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.unit)

    def __mul__(self, scalar: Decimal | int) -> Money:
        if not _is_scalar(scalar):
            raise TypeError(
                f"Money can only be multiplied by a Decimal or int, not {type(scalar).__name__}."
            )
        return Money(self.amount * Decimal(scalar), self.unit)

    __rmul__ = __mul__

    def __truediv__(self, scalar: Decimal | int) -> Money:
        if not _is_scalar(scalar):
            raise TypeError(
                f"Money can only be divided by a Decimal or int, not {type(scalar).__name__}."
            )
        if scalar == 0:
            raise ZeroDivisionError("Cannot divide Money by zero.")
        return Money(self.amount / Decimal(scalar), self.unit)

    def __lt__(self, other: Money) -> bool:
        self._same_unit(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._same_unit(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._same_unit(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._same_unit(other)
        return self.amount >= other.amount

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    def quantize(self, exponent: Decimal = _DISPLAY_QUANTUM) -> Money:
        """Round to `exponent` for display; never call this before storage
        or before a downstream calculation — quantise only at the edge."""
        return Money(self.amount.quantize(exponent), self.unit)

    def formatted(self) -> str:
        """Render using the Indian digit-grouping convention, e.g. ``₹1,23,456.78``."""
        rendered = _format_indian(self.amount.quantize(_DISPLAY_QUANTUM))
        if self.unit == _DEFAULT_UNIT:
            return f"{_RUPEE_SYMBOL}{rendered}"
        return f"{_RUPEE_SYMBOL}{rendered} {self.unit}"


def _format_indian(amount: Decimal) -> str:
    """Group digits as 1,23,456.78 rather than 123,456.78."""
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):f}".partition(".")

    if len(whole) <= 3:
        grouped = whole
    else:
        last_three = whole[-3:]
        remaining = whole[:-3]
        groups: list[str] = []
        while len(remaining) > 2:
            groups.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            groups.insert(0, remaining)
        grouped = ",".join([*groups, last_three])

    return f"{sign}{grouped}.{fraction}" if fraction else f"{sign}{grouped}"
