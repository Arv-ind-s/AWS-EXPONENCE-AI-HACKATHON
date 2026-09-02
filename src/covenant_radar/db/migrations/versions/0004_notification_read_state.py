"""durable in-app notification read state

Adds the durable read receipt used by the in-app notification centre.
Delivery state and read state are separate concerns: an email or webhook can
be delivered without being read in the browser, and an in-app notification
must remain reconstructable after a process restart.

Revision ID: 0004_notification_read_state
Revises: 0003_saved_queue_views
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import covenant_radar.db.types

revision: str = "0004_notification_read_state"
down_revision: str | None = "0003_saved_queue_views"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_read_state",
        sa.Column("notification_id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column("recipient_id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column("read_at", covenant_radar.db.types.AwareDateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", covenant_radar.db.types.GUID(length=36), nullable=True),
        sa.Column("updated_by_id", covenant_radar.db.types.GUID(length=36), nullable=True),
        sa.Column("id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column(
            "created_at", covenant_radar.db.types.AwareDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", covenant_radar.db.types.AwareDateTime(timezone=True), nullable=False
        ),
        sa.Column("request_id", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["app_user.id"],
            name=op.f("fk_notification_read_state_created_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notification.id"],
            name=op.f("fk_notification_read_state_notification_id_notification"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["app_user.id"],
            name=op.f("fk_notification_read_state_recipient_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["app_user.id"],
            name=op.f("fk_notification_read_state_updated_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_read_state")),
        sa.UniqueConstraint(
            "notification_id", name=op.f("uq_notification_read_state_notification_id")
        ),
    )
    with op.batch_alter_table("notification_read_state", schema=None) as batch_op:
        batch_op.create_index(
            "ix_notification_read_state_recipient_read_at",
            ["recipient_id", "read_at", "notification_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("notification_read_state", schema=None) as batch_op:
        batch_op.drop_index("ix_notification_read_state_recipient_read_at")
    op.drop_table("notification_read_state")
