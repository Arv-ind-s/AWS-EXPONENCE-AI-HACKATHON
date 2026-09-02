"""The ratio domain: pure, exact-decimal computation of `spec §R-07`'s
covenant ratio library (`plan.md §6`, `T-027`/`T-028`)."""

from __future__ import annotations

from covenant_radar.domain.ratios.compute import (
    FacilityFacts,
    RatioResult,
    UnknownDefinition,
    compute_ratio,
)
from covenant_radar.domain.ratios.definitions import (
    FormulaOutcome,
    RatioDefinition,
    RatioEntry,
    RatioFormula,
)
from covenant_radar.domain.ratios.library import ENTRIES, FORMULAS, LIBRARY
from covenant_radar.domain.ratios.reasons import NotComputableReason

__all__ = [
    "ENTRIES",
    "FORMULAS",
    "LIBRARY",
    "FacilityFacts",
    "FormulaOutcome",
    "NotComputableReason",
    "RatioDefinition",
    "RatioEntry",
    "RatioFormula",
    "RatioResult",
    "UnknownDefinition",
    "compute_ratio",
]
