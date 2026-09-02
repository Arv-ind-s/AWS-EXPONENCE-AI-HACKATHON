"""forecast ML provenance

Revision ID: 0007_forecast_ml_provenance
Revises: 0006_financial_pdf_batches
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_forecast_ml_provenance"
down_revision = "0006_financial_pdf_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("forecast") as batch:
        batch.add_column(
            sa.Column("probability_source", sa.String(length=20), nullable=False, server_default="deterministic")
        )
        batch.add_column(sa.Column("fallback_reason", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("forecast") as batch:
        batch.drop_column("fallback_reason")
        batch.drop_column("probability_source")
