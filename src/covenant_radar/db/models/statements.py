"""Statement import tables: `plan.md §5.3`'s `import_mapping`, `import_batch`,
`quarantine_row`, `financial_period`, `statement_line_value` and
`field_provenance` (`T-025`).

`statement_line_definition` — the seventh table `plan.md §5.3` names — is
deliberately not modelled here: `T-024` (`domain/statements/chart.py`)
already sources the normalised chart of accounts from the packaged
`db/seed/data/statement_lines.json` file at process start
(`Chart.load()`/`default_chart()`), not from a database table, and nothing
downstream expects a DB-backed copy of it.

`FinancialPeriod.version` doubles as both `VersionedColumns`' usual
optimistic-concurrency counter and `plan.md`'s restatement ordinal: a
restatement (`T-026`) creates a *new* row at `version + 1` rather than
mutating this one, exactly as `Facility.supersede` (`db/models/facility.py`)
already creates a new row instead of overwriting a limit change in place.
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models._decimal import RatioValue
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, AwareDateTime, PortableJSON

_NAME_MAX_LENGTH = 200
_SOURCE_TYPE_MAX_LENGTH = 20
_SOURCE_REFERENCE_MAX_LENGTH = 500
_HASH_MAX_LENGTH = 128
_BATCH_STATE_MAX_LENGTH = 20
_ROW_REFERENCE_MAX_LENGTH = 50
_RULE_MAX_LENGTH = 100
_MESSAGE_MAX_LENGTH = 1000
_RESOLUTION_MAX_LENGTH = 1000
_FY_LABEL_MAX_LENGTH = 20
_PERIOD_TYPE_MAX_LENGTH = 20
_LINE_CODE_MAX_LENGTH = 100
_UNIT_MAX_LENGTH = 20
_CURRENCY_MAX_LENGTH = 3
_TRANSFORM_NOTE_MAX_LENGTH = 1000

#: Shared by `ImportMapping.source_type` and `ImportBatch.source_type` — a
#: mapping is written for exactly one of these, and a batch always carries
#: the source type of the mapping it was ingested through.
_SOURCE_TYPES: Final[tuple[str, ...]] = ("csv", "xlsx", "json", "api")
_BATCH_STATES: Final[tuple[str, ...]] = ("completed", "failed")
_PERIOD_TYPES: Final[tuple[str, ...]] = ("quarterly", "half_yearly", "annual")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class ImportMapping(Base, UserAttributedColumns, StandardColumns):
    """A versioned column/unit/currency/sign/borrower-key mapping for one
    source (`plan.md §5.3`). A mapping change is a new version — this row is
    never edited in place once a batch has been imported through it, so it
    carries no optimistic-concurrency `version` column of its own."""

    __tablename__ = "import_mapping"
    __table_args__ = (
        CheckConstraint(
            f"source_type IN ({_sql_in_list(_SOURCE_TYPES)})", name="source_type_valid"
        ),
        UniqueConstraint("name", "version", name="uq_import_mapping_name_version"),
    )

    name: Mapped[str] = mapped_column(String(_NAME_MAX_LENGTH), nullable=False)
    source_type: Mapped[str] = mapped_column(String(_SOURCE_TYPE_MAX_LENGTH), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ImportBatch(Base, UserAttributedColumns, StandardColumns):
    """One import run (`plan.md §5.3`). `content_hash` is unique, so
    re-importing the same bytes is a duplicate the service reports rather
    than an error it raises — idempotence for free, exactly the shape
    `signal_event.content_hash` (`db/models/signal.py`) already uses.
    Ingested, not user-edited, so it carries no `version` column."""

    __tablename__ = "import_batch"
    __table_args__ = (
        CheckConstraint(
            f"source_type IN ({_sql_in_list(_SOURCE_TYPES)})", name="source_type_valid"
        ),
        CheckConstraint(f"state IN ({_sql_in_list(_BATCH_STATES)})", name="state_valid"),
    )

    source_type: Mapped[str] = mapped_column(String(_SOURCE_TYPE_MAX_LENGTH), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(
        String(_SOURCE_REFERENCE_MAX_LENGTH), nullable=True
    )
    mapping_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("import_mapping.id", ondelete="RESTRICT"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(_HASH_MAX_LENGTH), nullable=False, unique=True)
    started_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    quarantined_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(_BATCH_STATE_MAX_LENGTH), nullable=False)
    report: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)


class QuarantineRow(Base, UserAttributedColumns, StandardColumns):
    """One row held out of a batch, with the failing rule and message
    (`plan.md §5.3`) — nothing an import refuses is ever silently dropped.
    `batch_id` cascades: a quarantine row has no meaning outside the batch
    that produced it. Ingested, not user-edited, so it carries no
    `version` column of its own; `resolved_at`/`resolved_by_id`/
    `resolution` are the resolution workflow `T-026` will populate."""

    __tablename__ = "quarantine_row"
    __table_args__ = (Index("ix_quarantine_row_batch_id_resolved_at", "batch_id", "resolved_at"),)

    batch_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    rule_failed: Mapped[str] = mapped_column(String(_RULE_MAX_LENGTH), nullable=False)
    message: Mapped[str] = mapped_column(String(_MESSAGE_MAX_LENGTH), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    resolved_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    resolution: Mapped[str | None] = mapped_column(String(_RESOLUTION_MAX_LENGTH), nullable=True)


class FinancialPeriod(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One borrower's financial period (`plan.md §5.3`). A restatement
    (`T-026`) creates a new row at `version + 1` and chains it via
    `superseded_by_id`; this row's own columns are never rewritten once
    `T-026` exists to supersede it."""

    __tablename__ = "financial_period"
    __table_args__ = (
        CheckConstraint(
            f"period_type IN ({_sql_in_list(_PERIOD_TYPES)})", name="period_type_valid"
        ),
        CheckConstraint("period_end > period_start", name="period_end_after_start"),
        UniqueConstraint(
            "borrower_id", "fy_label", "version", name="uq_financial_period_borrower_fy_version"
        ),
    )

    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False
    )
    fy_label: Mapped[str] = mapped_column(String(_FY_LABEL_MAX_LENGTH), nullable=False)
    period_type: Mapped[str] = mapped_column(String(_PERIOD_TYPE_MAX_LENGTH), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_audited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("financial_period.id", ondelete="RESTRICT"), nullable=True
    )
    source_batch_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("import_batch.id", ondelete="RESTRICT"), nullable=True
    )


class FieldProvenance(Base, UserAttributedColumns, StandardColumns):
    """Where one imported row's values came from (`plan.md §5.3`) —
    referenced by every `StatementLineValue` produced from that row.
    Ingested, not user-edited, so it carries no `version` column."""

    __tablename__ = "field_provenance"
    __table_args__ = (
        CheckConstraint(
            f"source_type IN ({_sql_in_list(_SOURCE_TYPES)})", name="source_type_valid"
        ),
    )

    source_type: Mapped[str] = mapped_column(String(_SOURCE_TYPE_MAX_LENGTH), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(
        String(_SOURCE_REFERENCE_MAX_LENGTH), nullable=True
    )
    row_reference: Mapped[str | None] = mapped_column(
        String(_ROW_REFERENCE_MAX_LENGTH), nullable=True
    )
    mapping_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    batch_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("import_batch.id", ondelete="RESTRICT"), nullable=False
    )
    transform_note: Mapped[str | None] = mapped_column(
        String(_TRANSFORM_NOTE_MAX_LENGTH), nullable=True
    )


class StatementLineValue(Base, UserAttributedColumns, StandardColumns):
    """One normalised line value for one financial period (`plan.md
    §5.3`) — exactly the shape `Chart.normalise` produces, persisted.
    `period_id` cascades: a line value has no meaning outside its period.
    Ingested, not user-edited, so it carries no `version` column."""

    __tablename__ = "statement_line_value"
    __table_args__ = (
        UniqueConstraint("period_id", "line_code", name="uq_statement_line_value_period_line"),
    )

    period_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("financial_period.id", ondelete="CASCADE"), nullable=False
    )
    line_code: Mapped[str] = mapped_column(String(_LINE_CODE_MAX_LENGTH), nullable=False)
    value: Mapped[Decimal] = mapped_column(RatioValue(), nullable=False)
    unit: Mapped[str] = mapped_column(String(_UNIT_MAX_LENGTH), nullable=False)
    currency: Mapped[str] = mapped_column(String(_CURRENCY_MAX_LENGTH), nullable=False)
    provenance_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("field_provenance.id", ondelete="RESTRICT"), nullable=False
    )
