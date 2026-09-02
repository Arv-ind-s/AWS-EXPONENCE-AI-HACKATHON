"""The covenant domain: terms, versioning and immutability rules that hold
independently of persistence (`plan.md §5.5`, `T-031`)."""

from __future__ import annotations

from covenant_radar.domain.covenants.model import (
    DIRECTIONS,
    FREQUENCIES,
    UNITS,
    CovenantVersionTerms,
    UnknownCovenantDefinition,
)

__all__ = [
    "DIRECTIONS",
    "FREQUENCIES",
    "UNITS",
    "CovenantVersionTerms",
    "UnknownCovenantDefinition",
]
