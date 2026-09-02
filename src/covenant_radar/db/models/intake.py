"""The intake proposal table: `plan.md §8`'s `T-096`, `spec §R-06`/`C-05`/
`C-06`.

`CovenantProposal` persists one clause candidate's stage-1 outcome —
`domain/intake/proposal.py`'s `StageOneProposal`, normalised, plus
`domain/intake/verify.py`/`ai/shapes.py`'s six-check-and-injection-scan
verdict — so a reviewer's correction, abandonment or confirmation acts on a
durable row rather than a value that only ever existed inside one request.

Every proposal column mirrors a `StageOneProposal` field one-for-one and is
nullable exactly where that dataclass allows `None` (an unparseable reply
carries none of them). ``checks`` stores the six-check report as structured
JSON — one object per `domain.intake.verify.VerificationCheckName`, in that
enum's own order — so `services/intake.py` can rebuild a faithful
`Stage1VerificationOutcome` from a persisted row without re-running
verification, the same way `covenant_test.inputs` (`db/models/covenant.py`)
lets a reviewer see what a computed value was built from without recomputing
it.

``status`` is a three-state lifecycle — ``open`` (awaiting correction,
abandonment or submission), ``confirmed`` (a live/draft covenant version was
created from it) or ``abandoned`` (retained as evidence, never deleted) —
never a fourth state that could let a caller believe a covenant was created
from a row that failed verification: that refusal is enforced entirely in
`services/intake.py`, this module only stores the verdict it refuses on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models._decimal import RatioValue
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, PortableJSON

_DEFINITION_REF_MAX_LENGTH = 20
_DIRECTION_MAX_LENGTH = 4
_UNIT_MAX_LENGTH = 20
_CURRENCY_MAX_LENGTH = 3
_FREQUENCY_MAX_LENGTH = 20
_STATUS_MAX_LENGTH = 20
_CONTENT_HASH_MAX_LENGTH = 64

_STATUSES: Final[tuple[str, ...]] = ("open", "confirmed", "abandoned")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class CovenantProposal(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One clause candidate's stage-1 proposal, its six-check-plus-injection
    verdict, and its confirm/correct/abandon lifecycle.

    ``document_id``/``source_span_id`` are null for a hand-pasted
    ``clause_text`` proposal carrying no uploaded document. ``covenant_id``/
    ``covenant_version_id`` are set only once ``status`` becomes
    ``confirmed`` — `services/intake.py` is the only writer of either, and
    only from inside the same transaction that creates the covenant version
    they point at.
    """

    __tablename__ = "covenant_proposal"
    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_in_list(_STATUSES)})", name="status_valid"),
        Index("ix_covenant_proposal_document_id", "document_id"),
        Index("ix_covenant_proposal_facility_id_content_hash", "facility_id", "content_hash"),
    )

    facility_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("document.id", ondelete="RESTRICT"), nullable=True
    )
    source_span_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("document_span.id", ondelete="RESTRICT"), nullable=True
    )
    covenant_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("covenant.id", ondelete="RESTRICT"), nullable=True
    )
    covenant_version_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("covenant_version.id", ondelete="RESTRICT"), nullable=True
    )

    # The candidate clause, as sent to the model — retained verbatim for
    # duplicate detection and for a reviewer to compare against the source.
    clause_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(_CONTENT_HASH_MAX_LENGTH), nullable=False)
    raw_reply: Mapped[str] = mapped_column(Text, nullable=False)

    # `StageOneProposal.parseable`/`parse_error`.
    parseable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `StageOneProposal`'s normalised fields, one-for-one.
    definition_ref: Mapped[str | None] = mapped_column(
        String(_DEFINITION_REF_MAX_LENGTH), nullable=True
    )
    custom_formula: Mapped[str | None] = mapped_column(Text, nullable=True)
    threshold: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    threshold_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unit: Mapped[str | None] = mapped_column(String(_UNIT_MAX_LENGTH), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(_CURRENCY_MAX_LENGTH), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(_DIRECTION_MAX_LENGTH), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(_FREQUENCY_MAX_LENGTH), nullable=True)
    frequency_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    exceptions: Mapped[list[str]] = mapped_column(PortableJSON, nullable=False, default=list)
    cure_period_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `Stage1VerificationOutcome`: the six-check report plus the injection
    # scan, stored so it can be rebuilt without re-verifying.
    checks: Mapped[list[dict[str, object]]] = mapped_column(PortableJSON, nullable=False)
    all_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    injection_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    security_event: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    refusal_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(_STATUS_MAX_LENGTH), nullable=False, default="open")
    abandon_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["CovenantProposal"]
