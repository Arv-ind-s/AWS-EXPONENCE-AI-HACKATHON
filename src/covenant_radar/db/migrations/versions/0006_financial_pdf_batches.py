"""financial PDF review batches

Revision ID: 0006_financial_pdf_batches
Revises: 0005_account_activity_signal
"""

from alembic import op
import sqlalchemy as sa

from covenant_radar.db.types import AwareDateTime, GUID, PortableJSON

revision = "0006_financial_pdf_batches"
down_revision = "0005_account_activity_signal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_pdf_batch",
        sa.Column("borrower_id", GUID(length=36), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("documents", PortableJSON(), nullable=False),
        sa.Column("candidates", PortableJSON(), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.Column("created_by_id", GUID(length=36), nullable=True),
        sa.Column("updated_by_id", GUID(length=36), nullable=True),
        sa.Column("id", GUID(length=36), nullable=False),
        sa.Column("created_at", AwareDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", AwareDateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["borrower_id"], ["borrower.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_financial_pdf_batch_borrower_state", "financial_pdf_batch", ["borrower_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_financial_pdf_batch_borrower_state", table_name="financial_pdf_batch")
    op.drop_table("financial_pdf_batch")
