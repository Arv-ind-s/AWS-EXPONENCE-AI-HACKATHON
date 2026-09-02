"""saved queue views

Adds the durable per-user filter sets used by the portfolio queue.  The model
was introduced after the initial schema; without this revision a clean
deployment has a model/migration mismatch and queue view loading fails.

Revision ID: 0003_saved_queue_views
Revises: 0002_statements
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import covenant_radar.db.types

revision: str = "0003_saved_queue_views"
down_revision: str | None = "0002_statements"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_queue_view",
        sa.Column("owner_id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("filter_json", sa.Text(), nullable=False),
        sa.Column("is_shared", sa.Boolean(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
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
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["app_user.id"],
            name=op.f("fk_saved_queue_view_created_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["app_user.id"],
            name=op.f("fk_saved_queue_view_owner_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["app_user.id"],
            name=op.f("fk_saved_queue_view_updated_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_queue_view")),
    )
    with op.batch_alter_table("saved_queue_view", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_saved_queue_view_owner_id"), ["owner_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_saved_queue_view_owner_is_shared"),
            ["owner_id", "is_shared"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("saved_queue_view", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_saved_queue_view_owner_is_shared"))
        batch_op.drop_index(batch_op.f("ix_saved_queue_view_owner_id"))
    op.drop_table("saved_queue_view")
