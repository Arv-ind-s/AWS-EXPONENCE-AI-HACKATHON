"""Clause candidate detection (`spec §R-06`, `plan.md §6`'s `T-093`).

Narrowing a sanction letter down to the handful of lines that might be
covenant clauses is deterministic code, not a model call: it keeps
`T-094`'s prompt small, its cost low and its task bounded to one clause at a
time. Everything in this package is pure — no database, no HTTP, no model
provider — so it is exercised entirely by unit tests and reused unchanged by
`services/intake.py`, the one adapter that loads real pages and spans.
"""

from __future__ import annotations

from covenant_radar.domain.intake.candidates import (
    DEFAULT_MATCH_FLOOR,
    DEFAULT_RECALL_FLOOR,
    CandidateLine,
    CandidatePage,
    ClauseCandidate,
    DetectionResult,
    RecallReport,
    detect_candidates,
    measure_recall,
)

__all__ = [
    "DEFAULT_MATCH_FLOOR",
    "DEFAULT_RECALL_FLOOR",
    "CandidateLine",
    "CandidatePage",
    "ClauseCandidate",
    "DetectionResult",
    "RecallReport",
    "detect_candidates",
    "measure_recall",
]
