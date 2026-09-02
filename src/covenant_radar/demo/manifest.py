"""The decision-complete 120-borrower showcase matrix.

This module is deliberately pure.  It is the single source of truth for the
coverage contract used by the builder, verifier and tests; no database write
path is hidden in the manifest.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final

SHOWCASE_BORROWER_COUNT: Final[int] = 120

INDUSTRY_CODES: Final[tuple[str, ...]] = (
    "A01",
    "B08",
    "C10",
    "C13",
    "C20",
    "C21",
    "C24",
    "C25",
    "C29",
    "D35",
    "E36",
    "F41",
    "G46",
    "G47",
    "H49",
    "H50",
    "H51",
    "I55",
    "J61",
    "J62",
    "K64",
    "L68",
    "M70",
    "N77",
)
RISK_BANDS: Final[tuple[str, ...]] = ("watch", "amber", "act")
SMA_BANDS: Final[tuple[str, ...]] = ("none", "SMA-0", "SMA-1", "SMA-2", "beyond")
SMA_DAYS_PAST_DUE: Final[dict[str, int]] = {
    "none": 0,
    "SMA-0": 15,
    "SMA-1": 45,
    "SMA-2": 75,
    "beyond": 105,
}
CASE_STATES: Final[tuple[str, ...]] = (
    "open",
    "in_progress",
    "monitoring",
    "escalated",
    "closed",
)
COVENANT_VERDICTS: Final[tuple[str, ...]] = (
    "pass",
    "warning",
    "breach",
    "breach_cure_open",
    "stale",
    "not_computable",
)
EVIDENCE_STATES: Final[tuple[str, ...]] = (
    "transient",
    "sustained",
    "superseded",
    "disputed",
)
FORECAST_STORIES: Final[tuple[str, ...]] = (
    "cross_within_30",
    "cross_30_60",
    "cross_60_90",
    "no_crossing",
    "suppressed",
)
NOTIFICATION_STORIES: Final[tuple[str, ...]] = (
    "band_change",
    "morning_queue",
    "sla_breach",
    "certificate_due",
    "case_comment_mention",
    "case_assignee_fallback",
)


@dataclass(frozen=True, slots=True)
class Scenario:
    """One stable borrower assignment in the showcase matrix."""

    ordinal: int
    borrower_reference: str
    industry_code: str
    risk_band: str
    sma_band: str
    days_past_due: int
    covenant_verdict: str
    evidence_state: str
    case_state: str
    forecast_story: str
    notification_story: str


def scenario_manifest() -> tuple[Scenario, ...]:
    """Return all scenarios in stable borrower-reference order."""

    rows: list[Scenario] = []
    for offset in range(SHOWCASE_BORROWER_COUNT):
        ordinal = offset + 1
        sma = SMA_BANDS[offset % len(SMA_BANDS)]
        rows.append(
            Scenario(
                ordinal=ordinal,
                borrower_reference=f"B-{ordinal:06d}",
                industry_code=INDUSTRY_CODES[offset % len(INDUSTRY_CODES)],
                risk_band=RISK_BANDS[(offset // len(SMA_BANDS)) % len(RISK_BANDS)],
                sma_band=sma,
                days_past_due=SMA_DAYS_PAST_DUE[sma],
                covenant_verdict=COVENANT_VERDICTS[offset % len(COVENANT_VERDICTS)],
                evidence_state=EVIDENCE_STATES[offset % len(EVIDENCE_STATES)],
                case_state=CASE_STATES[offset % len(CASE_STATES)],
                forecast_story=FORECAST_STORIES[offset % len(FORECAST_STORIES)],
                notification_story=NOTIFICATION_STORIES[offset % len(NOTIFICATION_STORIES)],
            )
        )
    _validate_manifest(tuple(rows))
    return tuple(rows)


def _validate_manifest(rows: tuple[Scenario, ...]) -> None:
    if len(rows) != SHOWCASE_BORROWER_COUNT:
        raise RuntimeError("The showcase manifest must contain exactly 120 borrowers.")
    if len({row.borrower_reference for row in rows}) != len(rows):
        raise RuntimeError("The showcase manifest contains duplicate borrower references.")
    if Counter(row.risk_band for row in rows) != Counter({band: 40 for band in RISK_BANDS}):
        raise RuntimeError("The showcase risk-band distribution is not 40/40/40.")
    if Counter(row.sma_band for row in rows) != Counter({band: 24 for band in SMA_BANDS}):
        raise RuntimeError("The showcase SMA distribution is not 24 per band.")
    matrix = Counter((row.risk_band, row.sma_band) for row in rows)
    if any(matrix[(risk, sma)] != 8 for risk in RISK_BANDS for sma in SMA_BANDS):
        raise RuntimeError("Every risk/SMA cell must contain exactly eight borrowers.")
    if Counter(row.industry_code for row in rows) != Counter(
        {code: 5 for code in INDUSTRY_CODES}
    ):
        raise RuntimeError("Every showcase industry must contain exactly five borrowers.")
    for code in INDUSTRY_CODES:
        represented = {row.risk_band for row in rows if row.industry_code == code}
        if len(represented) < 2:
            raise RuntimeError(f"Showcase industry {code} has insufficient risk variety.")


__all__ = [
    "CASE_STATES",
    "COVENANT_VERDICTS",
    "EVIDENCE_STATES",
    "FORECAST_STORIES",
    "INDUSTRY_CODES",
    "NOTIFICATION_STORIES",
    "RISK_BANDS",
    "SHOWCASE_BORROWER_COUNT",
    "SMA_BANDS",
    "SMA_DAYS_PAST_DUE",
    "Scenario",
    "scenario_manifest",
]
