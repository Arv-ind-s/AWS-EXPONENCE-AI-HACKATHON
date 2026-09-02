"""Signed covenant headroom calculations.

Headroom is the distance from an observed value to its contractual
threshold, expressed as a percentage of the threshold.  A positive value
means that the covenant still has room, a negative value means that it has
crossed the threshold, and zero is the exact boundary.

The calculation deliberately uses :class:`~decimal.Decimal` throughout.
It is imported by the covenant evaluator and by later forecast stages, so a
single implementation keeps the sign convention identical everywhere.
"""

from __future__ import annotations

from decimal import Decimal

_PERCENT = Decimal("100")
_DIRECTIONS = frozenset({"min", "max"})


def signed_headroom(value: Decimal, threshold: Decimal, direction: str) -> Decimal:
    """Return signed headroom as a percentage of ``threshold``.

    ``min`` covenants are safe when the observed value is above the
    threshold; ``max`` covenants are safe when it is below.  The magnitude
    is divided by the absolute threshold so the sign remains meaningful even
    for a valid contract whose threshold is negative.  A zero threshold has
    no percentage distance and is rejected explicitly rather than allowing a
    division error to escape from a caller.
    """

    _validate_decimal(value, "value")
    _validate_decimal(threshold, "threshold")
    if direction not in _DIRECTIONS:
        raise ValueError("direction must be either 'min' or 'max'.")
    if threshold == 0:
        raise ValueError("headroom is undefined for a zero threshold.")

    distance = value - threshold if direction == "min" else threshold - value
    return (distance / abs(threshold)) * _PERCENT


def _validate_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal.")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite.")


# Descriptive aliases make the same contract discoverable to callers without
# creating another implementation that could drift from ``signed_headroom``.
calculate_headroom = signed_headroom
compute_headroom = signed_headroom
headroom_pct = signed_headroom
headroom = signed_headroom


__all__ = [
    "calculate_headroom",
    "compute_headroom",
    "headroom",
    "headroom_pct",
    "signed_headroom",
]
