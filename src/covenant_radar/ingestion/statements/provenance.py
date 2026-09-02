"""Provenance trace assembly for stored statement values (`T-026`).

`services/statements.py` is the only caller that resolves a stored
`StatementLineValue` back through its `FieldProvenance`, `ImportBatch` and
`ImportMapping` rows; this module only shapes the result and formats the
`transform_note` text a restatement or a quarantine correction writes, the
same DB-free split every other module in this package keeps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

_ROW_REFERENCE_MAX_LENGTH: Final[int] = 50
_TRANSFORM_NOTE_MAX_LENGTH: Final[int] = 1000


@dataclass(frozen=True, slots=True)
class ProvenanceTrace:
    """Where one stored statement value came from: source, row and mapping
    version (`plan.md §5.3`'s `field_provenance`, `T-026`'s "Every case")."""

    source_type: str
    source_reference: str | None
    row_reference: str | None
    mapping_name: str
    mapping_version: int
    batch_id: UUID
    ingested_at: datetime
    transform_note: str | None


def restatement_transform_note(*, spec_summary: str, reason: str) -> str:
    """The transform note a restated field's new provenance carries.

    Names this as a restatement and carries the steward's reason, so a
    later provenance query never has to guess why a value changed.
    """
    note = f"restatement ({reason}); {spec_summary}"
    return note[:_TRANSFORM_NOTE_MAX_LENGTH]


def correction_row_reference(row_number: int) -> str:
    """The row reference for a corrected-and-reloaded quarantine row."""
    return f"row_{row_number}_corrected"[:_ROW_REFERENCE_MAX_LENGTH]


def correction_transform_note(
    *,
    spec_summary: str,
    quarantine_row_id: UUID,
    original_rule_failed: str,
    reason: str,
) -> str:
    """The transform note naming both the original quarantine row and the
    correction that resolved it (`T-026`'s "loaded with provenance naming
    both the original and the correction")."""
    note = (
        f"corrects quarantine_row {quarantine_row_id} (rule={original_rule_failed}); "
        f"reason={reason}; {spec_summary}"
    )
    return note[:_TRANSFORM_NOTE_MAX_LENGTH]


__all__ = [
    "ProvenanceTrace",
    "correction_row_reference",
    "correction_transform_note",
    "restatement_transform_note",
]
