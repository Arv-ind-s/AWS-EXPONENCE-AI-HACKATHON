"""Portable fixed-point decimal types for `plan.md §5.4`-`§5.9`'s tables
(`T-009`), scoped to `db/models/` the same way `facility.py`'s private
`_PercentageValue` is.

`db/types.py` (`T-006`) is not this task's file to change, and the types it
already exports are each named for one specific meaning — `MoneyAmount` for
currency, `AwareDateTime` for an instant. The columns this task adds need a
handful of *other* fixed-point shapes (a covenant ratio's value, a
probability, a percentage), so this module repeats `MoneyAmount`'s
SQLite-as-text technique at each precision T-009's columns need, rather
than repurpose a type whose name says "money" or invent one shared
type wide enough to fit every meaning and precise about none of them.
Every field-encrypted/money-shaped column in this task's tables still uses
`db/types.py`'s own `MoneyAmount` directly — this module is only for the
shapes that type does not cover.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.engine import Dialect
from sqlalchemy.types import Numeric, Text, TypeDecorator, TypeEngine


class _PortableNumeric(TypeDecorator[Decimal]):
    """A fixed-point decimal, numeric on PostgreSQL and fixed-point text on
    SQLite so SQLite's float type-affinity never corrupts the value on
    write. Refuses a non-`Decimal` bind value for the same reason."""

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int) -> None:
        super().__init__()
        self._precision = precision
        self._scale = scale
        self._quantum = Decimal("1").scaleb(-scale)

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(Numeric(self._precision, self._scale, asdecimal=True))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Decimal | str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(
                f"{type(self).__name__} requires a Decimal, not "
                f"{type(value).__name__} ({value!r})."
            )
        quantized = value.quantize(self._quantum)
        return format(quantized, "f") if dialect.name == "sqlite" else quantized

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class RatioValue(_PortableNumeric):
    """A covenant ratio, threshold or other computed value — a wide,
    unitless decimal (`covenant_version.threshold`, `covenant_test.value`,
    `forecast_path.projected_value`, ...)."""

    cache_ok = True

    def __init__(self) -> None:
        super().__init__(precision=24, scale=8)


class PercentageValue(_PortableNumeric):
    """A percentage, stored as the number and not the fraction —
    `87.5000` means 87.5%, exactly `facility._PercentageValue`'s
    convention, repeated here because that type is private to its own
    module (`headroom_pct`, `materiality_pct`, ...)."""

    cache_ok = True

    def __init__(self) -> None:
        super().__init__(precision=7, scale=4)


class FractionValue(_PortableNumeric):
    """A value on `[0, 1]` — a probability, a confidence, a driver's
    share (`forecast.probability`, `forecast_driver.share`, ...)."""

    cache_ok = True

    def __init__(self) -> None:
        super().__init__(precision=5, scale=4)
