"""The ratio library: every definition's metadata and pure compute function,
keyed by code (`plan.md §6`'s `C-30`, `T-027`/`T-028`).

Built once, at import time, from `definitions.ENTRIES` (the twenty-two plain
ratios) and `conditions.ENTRIES` (the two condition-type definitions) — the
full twenty-four of `spec §R-07`. A duplicate code is a programming error,
not a data condition, so it fails the import rather than silently keeping
one entry.
"""

from __future__ import annotations

from collections.abc import Mapping

from covenant_radar.domain.ratios.conditions import ENTRIES as _CONDITION_ENTRIES
from covenant_radar.domain.ratios.definitions import (
    ENTRIES as _RATIO_ENTRIES,
)
from covenant_radar.domain.ratios.definitions import (
    RatioDefinition,
    RatioEntry,
    RatioFormula,
)

ENTRIES: tuple[RatioEntry, ...] = _RATIO_ENTRIES + _CONDITION_ENTRIES


def _build_library(
    entries: tuple[RatioEntry, ...],
) -> tuple[dict[str, RatioDefinition], dict[str, RatioFormula]]:
    definitions: dict[str, RatioDefinition] = {}
    formulas: dict[str, RatioFormula] = {}
    for entry in entries:
        code = entry.definition.code
        if code in definitions:
            raise ValueError(f"Duplicate ratio definition code {code!r} in the ratio library.")
        definitions[code] = entry.definition
        formulas[code] = entry.formula
    return definitions, formulas


_DEFINITIONS, _FORMULAS = _build_library(ENTRIES)

LIBRARY: Mapping[str, RatioDefinition] = _DEFINITIONS
FORMULAS: Mapping[str, RatioFormula] = _FORMULAS

__all__ = ["ENTRIES", "FORMULAS", "LIBRARY"]
