"""Signal, evidence and certificate tables: `plan.md §5.6`'s `signal_event`,
`evidence_item`, `evidence_transition` and `certificate_request`.

`signal_event.content_hash` is unique, so re-ingesting the same event is a
duplicate the ingestion layer reports rather than an error it raises —
idempotence for free, the same shape `import_batch.content_hash` (`plan.md
§5.3`) and `document.content_hash` already use.

`evidence_item` rows are never deleted — a superseded item's history is the
point, so `state` moves to `superseded` and `superseded_by_id`/
`supersedes_id` chain the record, exactly as `Facility.supersede` (`T-008`)
chains a limit change instead of overwriting it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models._decimal import FractionValue, PercentageValue, RatioValue
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, AwareDateTime, PortableJSON

_FAMILY_MAX_LENGTH = 20
_EVENT_TYPE_MAX_LENGTH = 50
_UNIT_MAX_LENGTH = 20
_HASH_MAX_LENGTH = 128
_EVIDENCE_TYPE_MAX_LENGTH = 50
_STATE_MAX_LENGTH = 20
_RULE_MAX_LENGTH = 100
_CERTIFICATE_STATE_MAX_LENGTH = 20
_REJECTION_REASON_MAX_LENGTH = 2000

#: Shared by `signal_event.family` and `evidence_item.family` — the same
#: classification named once in `plan.md §5.6`'s prose and used
#: identically by both tables throughout that section.
_FAMILIES: Final[tuple[str, ...]] = (
    "account_activity",
    "payment",
    "utilisation",
    "treasury",
    "concentration",
    "industry",
    "news",
)
_EVIDENCE_STATES: Final[tuple[str, ...]] = ("transient", "sustained", "superseded", "disputed")
_CERTIFICATE_STATES: Final[tuple[str, ...]] = (
    "requested",
    "received",
    "under_review",
    "accepted",
    "rejected",
    "overdue",
)


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class SignalEvent(Base, UserAttributedColumns, StandardColumns):
    """One ingested signal — a payment, a utilisation spike, a news item —
    the raw material `evidence_item` scores into a persisted pattern
    (`plan.md §5.6`). Ingested, not user-edited, so it carries no `version`
    column."""

    __tablename__ = "signal_event"
    __table_args__ = (
        CheckConstraint(f"family IN ({_sql_in_list(_FAMILIES)})", name="family_valid"),
        Index("ix_signal_event_borrower_id_event_date", "borrower_id", "event_date"),
        Index("ix_signal_event_event_date_family", "event_date", "family"),
    )

    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False
    )
    facility_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("facility.id", ondelete="RESTRICT"), nullable=True
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    family: Mapped[str] = mapped_column(String(_FAMILY_MAX_LENGTH), nullable=False)
    event_type: Mapped[str] = mapped_column(String(_EVENT_TYPE_MAX_LENGTH), nullable=False)
    magnitude: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(_UNIT_MAX_LENGTH), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    source_id: Mapped[UUID | None] = mapped_column(GUID, nullable=True)
    content_hash: Mapped[str] = mapped_column(
        String(_HASH_MAX_LENGTH), nullable=False, unique=True
    )
    is_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ingested_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)


class EvidenceItem(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A persisted, scored pattern of `SignalEvent`s (`plan.md §5.6`) — the
    thing R-11's forecast pressure actually reads, never the raw events
    directly."""

    __tablename__ = "evidence_item"
    __table_args__ = (
        CheckConstraint(f"family IN ({_sql_in_list(_FAMILIES)})", name="family_valid"),
        CheckConstraint(f"state IN ({_sql_in_list(_EVIDENCE_STATES)})", name="state_valid"),
        Index("ix_evidence_item_borrower_id_state", "borrower_id", "state"),
        Index("ix_evidence_item_state_last_scored_at", "state", "last_scored_at"),
    )

    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False
    )
    facility_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("facility.id", ondelete="RESTRICT"), nullable=True
    )
    family: Mapped[str] = mapped_column(String(_FAMILY_MAX_LENGTH), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(_EVIDENCE_TYPE_MAX_LENGTH), nullable=False)
    first_seen: Mapped[date] = mapped_column(Date, nullable=False)
    last_seen: Mapped[date] = mapped_column(Date, nullable=False)
    persistence_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_count_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    materiality_pct: Mapped[Decimal | None] = mapped_column(PercentageValue(), nullable=True)
    decay_factor: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)
    state: Mapped[str] = mapped_column(String(_STATE_MAX_LENGTH), nullable=False)
    counts_toward_pressure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("evidence_item.id", ondelete="RESTRICT"), nullable=True
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("evidence_item.id", ondelete="RESTRICT"), nullable=True
    )
    source_event_ids: Mapped[list[str]] = mapped_column(PortableJSON, nullable=False)
    last_scored_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)


class EvidenceTransition(Base, UserAttributedColumns, StandardColumns):
    """One state change in an `EvidenceItem`'s life — the flip `R-11.b`
    asserts is always in the trail (`plan.md §5.6`). Append-only history,
    so it carries no `version` column."""

    __tablename__ = "evidence_transition"

    evidence_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("evidence_item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_state: Mapped[str | None] = mapped_column(String(_STATE_MAX_LENGTH), nullable=True)
    to_state: Mapped[str] = mapped_column(String(_STATE_MAX_LENGTH), nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    rule: Mapped[str] = mapped_column(String(_RULE_MAX_LENGTH), nullable=False)
    threshold_snapshot_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("threshold_snapshot.id", ondelete="RESTRICT"), nullable=True
    )


class CertificateRequest(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One requested compliance certificate against a `covenant_schedule`
    due date (`plan.md §5.6`)."""

    __tablename__ = "certificate_request"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({_sql_in_list(_CERTIFICATE_STATES)})", name="state_valid"
        ),
        Index("ix_certificate_request_state_due_date", "state", "due_date"),
    )

    covenant_schedule_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("covenant_schedule.id", ondelete="RESTRICT"), nullable=False
    )
    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(
        String(_CERTIFICATE_STATE_MAX_LENGTH), nullable=False, default="requested"
    )
    requested_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    document_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("document.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(
        String(_REJECTION_REASON_MAX_LENGTH), nullable=True
    )
