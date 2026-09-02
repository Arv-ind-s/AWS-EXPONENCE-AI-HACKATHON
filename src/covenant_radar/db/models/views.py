"""Saved queue views: `plan.md §5.x`'s user-created filter sets.

A saved view stores a named filter set per user with an option to share
within the user's organisation. Sharing is implemented as a filter applied
within the recipient's scope, not a result set — this prevents scope
leakage while allowing users to share useful filter combinations.

When a portfolio or role is removed from a user's scope, any saved view
the user created that references it silently drops that filter on load
and notifies the user that the view has been narrowed.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID

_VIEW_NAME_MAX_LENGTH = 100
_DESCRIPTION_MAX_LENGTH = 500


class SavedQueueView(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A named filter set owned by one user, optionally shared.

    ``filter_json`` is persisted as a validated JSON string produced by
    SavedView.to_json(), allowing round-trip without schema inference.
    ``is_shared`` gates whether the view appears in the recipient's saved
    view list; a user can always load a shared view by ID if they know it,
    but discovery is restricted.
    """

    __tablename__ = "saved_queue_view"
    __table_args__ = (
        Index("ix_saved_queue_view_owner_id", "owner_id"),
        Index("ix_saved_queue_view_owner_is_shared", "owner_id", "is_shared"),
    )

    owner_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(_VIEW_NAME_MAX_LENGTH), nullable=False)
    filter_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(
        String(_DESCRIPTION_MAX_LENGTH), nullable=True
    )

    @classmethod
    def create(
        cls,
        owner_id: UUID,
        name: str,
        filter_json: str,
        *,
        is_shared: bool = False,
        description: str | None = None,
        created_at: datetime,
        updated_at: datetime,
        request_id: str,
    ) -> SavedQueueView:
        """Factory for creating a saved view with required fields."""
        return cls(
            owner_id=owner_id,
            name=name,
            filter_json=filter_json,
            is_shared=is_shared,
            description=description,
            created_at=created_at,
            updated_at=updated_at,
            request_id=request_id,
        )


__all__ = ["SavedQueueView"]
