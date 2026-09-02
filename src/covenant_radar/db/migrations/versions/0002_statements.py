"""statement import: import_mapping, import_batch, quarantine_row,
financial_period, field_provenance, statement_line_value

`T-025`'s six new tables from `plan.md §5.3`. `statement_line_definition` —
the seventh table that section names — is not created here: `T-024`
(`domain/statements/chart.py`) already sources the normalised chart from
the packaged `db/seed/data/statement_lines.json` file at process start, not
from a database table.

Created in foreign-key dependency order: `import_mapping` first (nothing
depends on it existing), then `import_batch` (references `import_mapping`),
then `field_provenance` and `financial_period` (both reference
`import_batch`; `financial_period` also self-references for
`superseded_by_id`), then `quarantine_row` (references `import_batch`) and
`statement_line_value` (references `financial_period` and
`field_provenance`). `downgrade()` drops in the exact reverse order.

Revision ID: 0002_statements
Revises: 0001_initial
Create Date: 2026-08-31 21:19:01.444662
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import covenant_radar.db.models._decimal
import covenant_radar.db.types

# revision identifiers, used by Alembic.
revision: str = "0002_statements"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _dialect_name() -> str:
    bind = op.get_bind()
    return bind.dialect.name


def _json_checks(table: str, *columns: str) -> list[sa.CheckConstraint]:
    """A SQLite-only `json_valid(...)` guard for each of `columns` on
    `table` — see `0001_initial.py`'s module docstring for why this lives
    in the migration rather than on the `PortableJSON` type itself."""
    if _dialect_name() != "sqlite":
        return []
    return [
        sa.CheckConstraint(f'json_valid("{column}")', name=op.f(f"ck_{table}_{column}_json_valid"))
        for column in columns
    ]


def upgrade() -> None:
    op.create_table(
        "import_mapping",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", covenant_radar.db.types.PortableJSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint(
            "source_type IN ('csv', 'xlsx', 'json', 'api')",
            name=op.f("ck_import_mapping_source_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["app_user.id"],
            name=op.f("fk_import_mapping_created_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["app_user.id"],
            name=op.f("fk_import_mapping_updated_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_mapping")),
        sa.UniqueConstraint("name", "version", name="uq_import_mapping_name_version"),
        *_json_checks("import_mapping", "spec"),
    )

    op.create_table(
        "import_batch",
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_reference", sa.String(length=500), nullable=True),
        sa.Column("mapping_id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "started_at", covenant_radar.db.types.AwareDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "finished_at", covenant_radar.db.types.AwareDateTime(timezone=True), nullable=True
        ),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("report", covenant_radar.db.types.PortableJSON(), nullable=False),
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
        sa.CheckConstraint(
            "source_type IN ('csv', 'xlsx', 'json', 'api')",
            name=op.f("ck_import_batch_source_type_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('completed', 'failed')", name=op.f("ck_import_batch_state_valid")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["app_user.id"],
            name=op.f("fk_import_batch_created_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_id"],
            ["import_mapping.id"],
            name=op.f("fk_import_batch_mapping_id_import_mapping"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["app_user.id"],
            name=op.f("fk_import_batch_updated_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batch")),
        sa.UniqueConstraint("content_hash", name=op.f("uq_import_batch_content_hash")),
        *_json_checks("import_batch", "report"),
    )

    op.create_table(
        "field_provenance",
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_reference", sa.String(length=500), nullable=True),
        sa.Column("row_reference", sa.String(length=50), nullable=True),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column(
            "ingested_at", covenant_radar.db.types.AwareDateTime(timezone=True), nullable=False
        ),
        sa.Column("batch_id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column("transform_note", sa.String(length=1000), nullable=True),
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
        sa.CheckConstraint(
            "source_type IN ('csv', 'xlsx', 'json', 'api')",
            name=op.f("ck_field_provenance_source_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["import_batch.id"],
            name=op.f("fk_field_provenance_batch_id_import_batch"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["app_user.id"],
            name=op.f("fk_field_provenance_created_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["app_user.id"],
            name=op.f("fk_field_provenance_updated_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_provenance")),
    )

    op.create_table(
        "financial_period",
        sa.Column("borrower_id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column("fy_label", sa.String(length=20), nullable=False),
        sa.Column("period_type", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column("is_audited", sa.Boolean(), nullable=False),
        sa.Column("superseded_by_id", covenant_radar.db.types.GUID(length=36), nullable=True),
        sa.Column("source_batch_id", covenant_radar.db.types.GUID(length=36), nullable=True),
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
        sa.CheckConstraint(
            "period_type IN ('quarterly', 'half_yearly', 'annual')",
            name=op.f("ck_financial_period_period_type_valid"),
        ),
        sa.CheckConstraint(
            "period_end > period_start", name=op.f("ck_financial_period_period_end_after_start")
        ),
        sa.ForeignKeyConstraint(
            ["borrower_id"],
            ["borrower.id"],
            name=op.f("fk_financial_period_borrower_id_borrower"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["app_user.id"],
            name=op.f("fk_financial_period_created_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["import_batch.id"],
            name=op.f("fk_financial_period_source_batch_id_import_batch"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["financial_period.id"],
            name=op.f("fk_financial_period_superseded_by_id_financial_period"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["app_user.id"],
            name=op.f("fk_financial_period_updated_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_period")),
        sa.UniqueConstraint(
            "borrower_id", "fy_label", "version", name="uq_financial_period_borrower_fy_version"
        ),
    )

    op.create_table(
        "quarantine_row",
        sa.Column("batch_id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw", covenant_radar.db.types.PortableJSON(), nullable=True),
        sa.Column("rule_failed", sa.String(length=100), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column(
            "resolved_at", covenant_radar.db.types.AwareDateTime(timezone=True), nullable=True
        ),
        sa.Column("resolved_by_id", covenant_radar.db.types.GUID(length=36), nullable=True),
        sa.Column("resolution", sa.String(length=1000), nullable=True),
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
            ["batch_id"],
            ["import_batch.id"],
            name=op.f("fk_quarantine_row_batch_id_import_batch"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["app_user.id"],
            name=op.f("fk_quarantine_row_created_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"],
            ["app_user.id"],
            name=op.f("fk_quarantine_row_resolved_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["app_user.id"],
            name=op.f("fk_quarantine_row_updated_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quarantine_row")),
        *_json_checks("quarantine_row", "raw"),
    )
    with op.batch_alter_table("quarantine_row", schema=None) as batch_op:
        batch_op.create_index(
            "ix_quarantine_row_batch_id_resolved_at", ["batch_id", "resolved_at"], unique=False
        )

    op.create_table(
        "statement_line_value",
        sa.Column("period_id", covenant_radar.db.types.GUID(length=36), nullable=False),
        sa.Column("line_code", sa.String(length=100), nullable=False),
        sa.Column("value", covenant_radar.db.models._decimal.RatioValue(), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provenance_id", covenant_radar.db.types.GUID(length=36), nullable=False),
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
            name=op.f("fk_statement_line_value_created_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["financial_period.id"],
            name=op.f("fk_statement_line_value_period_id_financial_period"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            ["field_provenance.id"],
            name=op.f("fk_statement_line_value_provenance_id_field_provenance"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["app_user.id"],
            name=op.f("fk_statement_line_value_updated_by_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_line_value")),
        sa.UniqueConstraint("period_id", "line_code", name="uq_statement_line_value_period_line"),
    )


def downgrade() -> None:
    op.drop_table("statement_line_value")
    with op.batch_alter_table("quarantine_row", schema=None) as batch_op:
        batch_op.drop_index("ix_quarantine_row_batch_id_resolved_at")
    op.drop_table("quarantine_row")
    op.drop_table("financial_period")
    op.drop_table("field_provenance")
    op.drop_table("import_batch")
    op.drop_table("import_mapping")
