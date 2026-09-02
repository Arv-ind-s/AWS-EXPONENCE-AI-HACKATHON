"""`maker_checker_request`: the one workflow table every maker-checker
operation in the product uses (`T-018` builds the service on top of it).

The distinct-actor rule — a maker cannot check their own request — is
enforced here as a database `CHECK` constraint, not only in application
code, so it cannot be bypassed by a code path that forgets to ask
(`plan.md §5.1`). The constraint is written so a still-pending request,
whose `checker_id` is `NULL`, never trips it: SQL's three-valued logic
treats `maker_id <> NULL` as unknown rather than false, so the row is
accepted right up until a checker — a *different* one — is recorded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, AwareDateTime, PortableJSON

_SUBJECT_TYPE_MAX_LENGTH = 50
_OPERATION_MAX_LENGTH = 50
_STATE_MAX_LENGTH = 20
_REASON_MAX_LENGTH = 2000

_STATES = ("pending", "approved", "rejected", "expired")


class MakerCheckerRequest(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A pending or decided second-pair-of-eyes request against some other
    row, named generically by `subject_type` and `subject_id` so covenant
    registration, threshold change, catalogue change and model promotion
    all share this one mechanism instead of each inventing their own.
    """

    __tablename__ = "maker_checker_request"
    __table_args__ = (
        CheckConstraint("maker_id <> checker_id", name="distinct_actor"),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in _STATES) + ")",
            name="state_valid",
        ),
        Index("ix_maker_checker_request_state_subject_type", "state", "subject_type"),
    )

    subject_type: Mapped[str] = mapped_column(String(_SUBJECT_TYPE_MAX_LENGTH), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    operation: Mapped[str] = mapped_column(String(_OPERATION_MAX_LENGTH), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    maker_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    checker_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(_STATE_MAX_LENGTH), nullable=False, default="pending"
    )
    decided_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(_REASON_MAX_LENGTH), nullable=True)
