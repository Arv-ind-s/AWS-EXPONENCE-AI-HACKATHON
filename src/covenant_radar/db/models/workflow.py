"""Workflow, memo and notification tables: `plan.md §5.8`'s `case`,
`case_event`, `case_comment`, `action_taken`, `memo`, `memo_export`,
`override_record`, `disposition`, `notification` and
`notification_preference`.

`plan.md §5.8` names no `Indexes:` line of its own (unlike every section
before it) — this module adds `index=True` only on the foreign keys a
screen obviously filters by (a case's own history, a user's own
notifications), the same judgement `T-008`'s `RelatedParty.borrower_id` and
`BorrowerContact.borrower_id` already applied where their section's
`Indexes:` line was likewise silent on them.

`memo` is written **only** after the shape check passes (`plan.md §5.8`'s
Notes column) — that rule belongs to the service that builds a `Memo` row
(a later task), not to this table's shape; this module declares the
column, not the gate.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as dt_time
from typing import Final
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, AwareDateTime, PortableJSON

_REFERENCE_MAX_LENGTH = 20
_CASE_STATE_MAX_LENGTH = 20
_BAND_MAX_LENGTH = 20
_CLOSURE_REASON_MAX_LENGTH = 200
_EVENT_TYPE_MAX_LENGTH = 50
_OUTCOME_TEXT_MAX_LENGTH = 2000
_TEMPLATE_VERSION_MAX_LENGTH = 50
_PROMPT_VERSION_MAX_LENGTH = 50
_PROVIDER_MAX_LENGTH = 50
_MODEL_VERSION_MAX_LENGTH = 50
_CHECK_VERDICT_MAX_LENGTH = 50
_EXPORT_FORMAT_MAX_LENGTH = 10
_STORAGE_KEY_MAX_LENGTH = 500
_HASH_MAX_LENGTH = 128
_SUBJECT_TYPE_MAX_LENGTH = 50
_STAGE_MAX_LENGTH = 50
_USER_ACTION_MAX_LENGTH = 50
_REASON_MAX_LENGTH = 2000
_OUTCOME_MAX_LENGTH = 20
_REASON_CODE_MAX_LENGTH = 50
_CHANNEL_MAX_LENGTH = 20
_TEMPLATE_MAX_LENGTH = 100
_NOTIFICATION_STATE_MAX_LENGTH = 20
_LAST_ERROR_MAX_LENGTH = 2000
_DIGEST_FREQUENCY_MAX_LENGTH = 20

_CASE_STATES: Final[tuple[str, ...]] = (
    "open",
    "in_progress",
    "monitoring",
    "escalated",
    "closed",
)
_DISPOSITION_OUTCOMES: Final[tuple[str, ...]] = ("acted", "monitoring", "dismissed")
_EXPORT_FORMATS: Final[tuple[str, ...]] = ("pdf", "docx")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Case(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One borrower's tracked remediation case, opened from a triage
    entry or by hand (`plan.md §5.8`)."""

    __tablename__ = "case"
    __table_args__ = (
        CheckConstraint(f"state IN ({_sql_in_list(_CASE_STATES)})", name="state_valid"),
    )

    reference: Mapped[str] = mapped_column(
        String(_REFERENCE_MAX_LENGTH), nullable=False, unique=True
    )
    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    opened_from_run_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("forecast_run.id", ondelete="RESTRICT"), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(_CASE_STATE_MAX_LENGTH), nullable=False, default="open"
    )
    band_at_open: Mapped[str | None] = mapped_column(String(_BAND_MAX_LENGTH), nullable=True)
    assignee_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    due_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    closure_reason: Mapped[str | None] = mapped_column(
        String(_CLOSURE_REASON_MAX_LENGTH), nullable=True
    )
    closure_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class CaseEvent(Base, UserAttributedColumns, StandardColumns):
    """Append-only history for a `Case` (`plan.md §5.8`). No `version`
    column — the log is never edited, only appended to."""

    __tablename__ = "case_event"

    case_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("case.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(_EVENT_TYPE_MAX_LENGTH), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    payload: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)


class CaseComment(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A person's note on a `Case` (`plan.md §5.8`)."""

    __tablename__ = "case_comment"

    case_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("case.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    mentions: Mapped[list[str] | None] = mapped_column(PortableJSON, nullable=True)


class ActionTaken(Base, UserAttributedColumns, StandardColumns):
    """One intervention or free-text action recorded against a `Case` —
    `G2`'s measurement raw material (`plan.md §5.8`)."""

    __tablename__ = "action_taken"

    case_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("case.id", ondelete="CASCADE"), nullable=False, index=True
    )
    intervention_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("intervention.id", ondelete="RESTRICT"), nullable=True
    )
    free_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    taken_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    outcome: Mapped[str | None] = mapped_column(String(_OUTCOME_TEXT_MAX_LENGTH), nullable=True)


class Memo(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A generated borrower memo, written only after the shape check
    passes (`plan.md §5.8`)."""

    __tablename__ = "memo"

    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("forecast_run.id", ondelete="RESTRICT"), nullable=True
    )
    case_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("case.id", ondelete="RESTRICT"), nullable=True
    )
    template_version: Mapped[str] = mapped_column(
        String(_TEMPLATE_VERSION_MAX_LENGTH), nullable=False
    )
    prompt_version: Mapped[str | None] = mapped_column(
        String(_PROMPT_VERSION_MAX_LENGTH), nullable=True
    )
    provider: Mapped[str | None] = mapped_column(String(_PROVIDER_MAX_LENGTH), nullable=True)
    model_version: Mapped[str | None] = mapped_column(
        String(_MODEL_VERSION_MAX_LENGTH), nullable=True
    )
    slots: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    drafted_text: Mapped[str] = mapped_column(Text, nullable=False)
    actions: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    simulations: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    check_verdict: Mapped[str | None] = mapped_column(
        String(_CHECK_VERDICT_MAX_LENGTH), nullable=True
    )
    generated_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


class MemoExport(Base, UserAttributedColumns, StandardColumns):
    """One exported rendering of a `Memo`, with the integrity hash
    `C-09` returns in its response header (`plan.md §5.8`)."""

    __tablename__ = "memo_export"
    __table_args__ = (
        CheckConstraint(f"format IN ({_sql_in_list(_EXPORT_FORMATS)})", name="format_valid"),
    )

    memo_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("memo.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(_EXPORT_FORMAT_MAX_LENGTH), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(_STORAGE_KEY_MAX_LENGTH), nullable=False)
    integrity_hash: Mapped[str] = mapped_column(String(_HASH_MAX_LENGTH), nullable=False)
    exported_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    exported_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


class OverrideRecord(Base, UserAttributedColumns, StandardColumns):
    """One person overriding what the system showed — the labelled
    dataset `N-12` learns from (`plan.md §5.8`). `subject_type`/
    `subject_id` name the overridden row generically, the same
    polymorphic pattern `MakerCheckerRequest` (`T-007`) already uses."""

    __tablename__ = "override_record"

    subject_type: Mapped[str] = mapped_column(String(_SUBJECT_TYPE_MAX_LENGTH), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    stage: Mapped[str] = mapped_column(String(_STAGE_MAX_LENGTH), nullable=False)
    shown: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    user_action: Mapped[str] = mapped_column(String(_USER_ACTION_MAX_LENGTH), nullable=False)
    user_value: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    reason: Mapped[str] = mapped_column(String(_REASON_MAX_LENGTH), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(
        String(_PROMPT_VERSION_MAX_LENGTH), nullable=True
    )
    model_version: Mapped[str | None] = mapped_column(
        String(_MODEL_VERSION_MAX_LENGTH), nullable=True
    )
    threshold_snapshot_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("threshold_snapshot.id", ondelete="RESTRICT"), nullable=True
    )
    actor_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )


class Disposition(Base, UserAttributedColumns, StandardColumns):
    """One recorded outcome against a subject — acted on, monitored, or
    dismissed (`plan.md §5.8`). Polymorphic, like `OverrideRecord`."""

    __tablename__ = "disposition"
    __table_args__ = (
        CheckConstraint(
            f"outcome IN ({_sql_in_list(_DISPOSITION_OUTCOMES)})", name="outcome_valid"
        ),
    )

    subject_type: Mapped[str] = mapped_column(String(_SUBJECT_TYPE_MAX_LENGTH), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    outcome: Mapped[str] = mapped_column(String(_OUTCOME_MAX_LENGTH), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(
        String(_REASON_CODE_MAX_LENGTH), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )


class Notification(Base, UserAttributedColumns, StandardColumns):
    """One outbound notification, tracked through delivery
    (`plan.md §5.8`)."""

    __tablename__ = "notification"

    recipient_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(_CHANNEL_MAX_LENGTH), nullable=False)
    template: Mapped[str] = mapped_column(String(_TEMPLATE_MAX_LENGTH), nullable=False)
    subject_type: Mapped[str | None] = mapped_column(
        String(_SUBJECT_TYPE_MAX_LENGTH), nullable=True
    )
    subject_id: Mapped[UUID | None] = mapped_column(GUID, nullable=True)
    payload: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    state: Mapped[str] = mapped_column(
        String(_NOTIFICATION_STATE_MAX_LENGTH), nullable=False, default="pending"
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(
        String(_LAST_ERROR_MAX_LENGTH), nullable=True
    )
    dead_lettered_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)


class NotificationReadState(Base, UserAttributedColumns, StandardColumns):
    """One durable browser read receipt for one retained notification.

    Read state is deliberately separate from delivery state.  The receipt's
    primary key is the notification id, which makes a bulk ``INSERT ...
    SELECT`` portable and idempotent while retaining the common standard
    provenance columns.
    """

    __tablename__ = "notification_read_state"
    __table_args__ = (
        UniqueConstraint("notification_id", name="uq_notification_read_state_notification_id"),
        Index(
            "ix_notification_read_state_recipient_read_at",
            "recipient_id",
            "read_at",
            "notification_id",
        ),
    )

    notification_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("notification.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    read_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)


class NotificationPreference(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One user's delivery preference for one notification template
    (`plan.md §5.8`)."""

    __tablename__ = "notification_preference"

    user_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template: Mapped[str] = mapped_column(String(_TEMPLATE_MAX_LENGTH), nullable=False)
    channel: Mapped[str] = mapped_column(String(_CHANNEL_MAX_LENGTH), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    quiet_hours_start: Mapped[dt_time | None] = mapped_column(Time, nullable=True)
    quiet_hours_end: Mapped[dt_time | None] = mapped_column(Time, nullable=True)
    digest_frequency: Mapped[str | None] = mapped_column(
        String(_DIGEST_FREQUENCY_MAX_LENGTH), nullable=True
    )
