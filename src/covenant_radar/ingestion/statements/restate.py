"""Restatement and quarantine-resolution decision logic (`T-026`).

DB-free, exactly like `normalise.py` and `validate.py`: this module
validates one corrected row against its original mapping and the chart, and
shapes the restatement outcome `services/statements.py` persists. It never
touches the database, never resolves a borrower id, and never queries a
`CovenantTest` row — those stay the service's job, the same split `T-025`
already draws between this package and its one DB-aware caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final
from uuid import UUID

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.statements.chart import Chart, NormalisationResult
from covenant_radar.ingestion.statements.mapping import ImportMappingSpec
from covenant_radar.ingestion.statements.normalise import ResolvedRow, resolve_row
from covenant_radar.ingestion.statements.readers import Cell

_MAX_REASON_LENGTH: Final[int] = 500


class RestatementError(ValidationError):
    """A restatement or quarantine resolution request cannot proceed."""


@dataclass(frozen=True, slots=True)
class ResolvedRestatementRow:
    """One corrected row, validated against its original mapping and chart —
    the same shape `validate.py`'s `PreparedStatementRow` carries for an
    ordinary import, so the service can persist either one identically."""

    resolved: ResolvedRow
    normalisation: NormalisationResult


def resolve_restatement_row(
    raw_row: Mapping[str, Cell],
    spec: ImportMappingSpec,
    chart: Chart,
    *,
    tolerance: Decimal,
) -> ResolvedRestatementRow:
    """Validate one corrected row exactly as `validate.prepare` would for an
    ordinary import row.

    Raises `RowShapeError` or `ChartError` (both `ValidationError`
    subclasses) naming what is still wrong, rather than letting a
    restatement or a quarantine correction persist a period that is itself
    broken — a corrected quarter is never reconciled by guessing.
    """
    resolved = resolve_row(raw_row, spec)
    normalisation = chart.normalise(resolved.lines, spec.unit, tolerance=tolerance)
    return ResolvedRestatementRow(resolved=resolved, normalisation=normalisation)


def validate_reason(reason: object, *, field: str = "reason") -> str:
    """Require a short, printable reason.

    Shared by restatement, quarantine correction and quarantine rejection —
    none of the three may proceed on an empty or control-character-laden
    string, since the reason is itself the audited record of why a stored
    figure changed.
    """
    if not isinstance(reason, str):
        raise RestatementError(f"{field} is required.", field=field)
    clean = reason.strip()
    if not clean:
        raise RestatementError(f"{field} is required.", field=field)
    if len(clean) > _MAX_REASON_LENGTH:
        raise RestatementError(
            f"{field} must be at most {_MAX_REASON_LENGTH} characters.", field=field
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise RestatementError(f"{field} contains an invalid control character.", field=field)
    return clean


@dataclass(frozen=True, slots=True)
class DependentTest:
    """One existing covenant test whose inputs came from the period a
    restatement supersedes — flagged and reported, never itself recomputed
    by this module or by the service that calls it."""

    covenant_test_id: UUID
    covenant_version_id: UUID
    as_of_date: date
    verdict: str


@dataclass(frozen=True, slots=True)
class RestatementResult:
    """The complete outcome of one restatement: the version chain it created
    plus every dependent test flagged for recomputation."""

    borrower_id: UUID
    fy_label: str
    previous_period_id: UUID
    previous_version: int
    new_period_id: UUID
    new_version: int
    reason: str
    flagged_tests: tuple[DependentTest, ...]

    @property
    def has_dependent_tests(self) -> bool:
        return bool(self.flagged_tests)


__all__ = [
    "DependentTest",
    "ResolvedRestatementRow",
    "RestatementError",
    "RestatementResult",
    "resolve_restatement_row",
    "validate_reason",
]
