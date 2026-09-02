"""Audit, trace and configuration tables: `plan.md §5.9`'s `audit_event`,
`trace_row`, `threshold_snapshot` and `config_version`.

**`audit_event` is append-only.** `C-60` (`audit/record.py`, a later task)
is the only write path into it, and no `UPDATE` or `DELETE` statement
against it exists anywhere in the source — `tests/unit/test_model_domain.py`
scans the tree and proves it. This module supplies the *database*-level
half of that guarantee: a trigger that refuses to insert a row whose
`prev_hash` does not match the previous row's `hash`, so the chain cannot
be started wrong even by a caller that bypasses `C-60` entirely. Granting
the application's database role no `UPDATE`/`DELETE` privilege on this
table is a deployment/migration concern (`T-010` and its downstream
infrastructure tasks), not a column this model declares.

`request_id` is `plan.md §5.9`'s own listed field for both `audit_event`
and `trace_row`; neither redeclares it because `StandardColumns` already
supplies it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import DDL, BigInteger, CheckConstraint, ForeignKey, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models._decimal import FractionValue
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, AwareDateTime, PortableJSON

_ACTOR_LABEL_MAX_LENGTH = 200
_EVENT_TYPE_MAX_LENGTH = 100
_SUBJECT_TYPE_MAX_LENGTH = 50
_HASH_MAX_LENGTH = 128
_STAGE_MAX_LENGTH = 50
_DECIDER_MAX_LENGTH = 20
_RULE_OR_PROMPT_VERSION_MAX_LENGTH = 50
_SOURCE_MAX_LENGTH = 50
_CHECKSUM_MAX_LENGTH = 128

_DECIDERS: Final[tuple[str, ...]] = ("code", "model", "statistical")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class AuditEvent(Base, UserAttributedColumns, StandardColumns):
    """One immutable link in the append-only audit chain (`plan.md
    §5.9`). No `version` column — an audit row is never edited.

    `hash = H(sequence ‖ occurred_at ‖ actor ‖ type ‖ subject ‖
    canonical(payload) ‖ prev_hash)`, computed by `C-60` before insert;
    this table only declares the column and the chain-integrity trigger
    below, never the hash function itself.
    """

    __tablename__ = "audit_event"

    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(_ACTOR_LABEL_MAX_LENGTH), nullable=True)
    event_type: Mapped[str] = mapped_column(String(_EVENT_TYPE_MAX_LENGTH), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(_SUBJECT_TYPE_MAX_LENGTH), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    threshold_snapshot_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("threshold_snapshot.id", ondelete="RESTRICT"), nullable=True
    )
    prev_hash: Mapped[str | None] = mapped_column(String(_HASH_MAX_LENGTH), nullable=True)
    hash: Mapped[str] = mapped_column(String(_HASH_MAX_LENGTH), nullable=False, unique=True)


# The chain-integrity trigger: BEFORE INSERT, the row being written must
# either be the very first one (no existing row, `prev_hash` NULL) or its
# `prev_hash` must equal the current latest row's `hash`, and its
# `sequence` must be strictly greater — refusing, at the database level,
# the one thing that would let the chain be started or continued wrong.
#
# Each dialect's DDL is a *sequence* of single-statement `DDL` objects,
# never one multi-statement string: SQLite's own DBAPI driver refuses to
# execute more than one statement per call, so a portable trigger
# installer has to assume every driver might.
#
# Public (not underscore-prefixed): `T-010`'s initial migration imports
# these verbatim rather than hand-copying the trigger SQL a second time,
# so the migration and the ORM event listener below can never drift apart.
POSTGRESQL_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE FUNCTION audit_event_chain_check() RETURNS trigger AS $$
    DECLARE
        latest_hash text;
        latest_sequence bigint;
    BEGIN
        SELECT hash, sequence INTO latest_hash, latest_sequence
        FROM audit_event ORDER BY sequence DESC LIMIT 1;

        IF latest_hash IS NULL THEN
            IF NEW.prev_hash IS NOT NULL THEN
                RAISE EXCEPTION
                    'audit_event: first row must have a NULL prev_hash';
            END IF;
        ELSE
            IF NEW.prev_hash IS DISTINCT FROM latest_hash THEN
                RAISE EXCEPTION
                    'audit_event: prev_hash does not match the previous row''s hash';
            END IF;
            IF NEW.sequence <= latest_sequence THEN
                RAISE EXCEPTION
                    'audit_event: sequence must be strictly greater than the previous row''s';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_audit_event_chain_check
    BEFORE INSERT ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_chain_check();
    """,
)

DROP_POSTGRESQL_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    "DROP TRIGGER IF EXISTS trg_audit_event_chain_check ON audit_event;",
    "DROP FUNCTION IF EXISTS audit_event_chain_check();",
)

SQLITE_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TRIGGER trg_audit_event_chain_check_first_row
    BEFORE INSERT ON audit_event
    WHEN (SELECT COUNT(*) FROM audit_event) = 0 AND NEW.prev_hash IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'audit_event: first row must have a NULL prev_hash');
    END;
    """,
    """
    CREATE TRIGGER trg_audit_event_chain_check_prev_hash
    BEFORE INSERT ON audit_event
    WHEN (SELECT COUNT(*) FROM audit_event) > 0
        AND NEW.prev_hash IS NOT (SELECT hash FROM audit_event ORDER BY sequence DESC LIMIT 1)
    BEGIN
        SELECT RAISE(ABORT, 'audit_event: prev_hash does not match the previous row''s hash');
    END;
    """,
    """
    CREATE TRIGGER trg_audit_event_chain_check_sequence
    BEFORE INSERT ON audit_event
    WHEN (SELECT COUNT(*) FROM audit_event) > 0
        AND NEW.sequence <= (SELECT MAX(sequence) FROM audit_event)
    BEGIN
        SELECT RAISE(ABORT,
            'audit_event: sequence must be strictly greater than the previous row''s');
    END;
    """,
)

DROP_SQLITE_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    "DROP TRIGGER IF EXISTS trg_audit_event_chain_check_first_row;",
    "DROP TRIGGER IF EXISTS trg_audit_event_chain_check_prev_hash;",
    "DROP TRIGGER IF EXISTS trg_audit_event_chain_check_sequence;",
)


def _install(table_event: str, dialect: str, statements: tuple[str, ...]) -> None:
    """Register each of `statements` as its own single-statement `DDL`
    event against `AuditEvent.__table__`, in order, for `dialect` only."""
    for statement in statements:
        # SQLAlchemy's `DDL.__init__` carries no type annotations of its
        # own (`sqlalchemy.sql.ddl`), so this call is flagged under
        # `check_untyped_defs` regardless of the argument's own type.
        ddl = DDL(statement)  # type: ignore[no-untyped-call]
        event.listen(AuditEvent.__table__, table_event, ddl.execute_if(dialect=dialect))


_install("after_create", "postgresql", POSTGRESQL_TRIGGER_STATEMENTS)
_install("before_drop", "postgresql", DROP_POSTGRESQL_TRIGGER_STATEMENTS)
_install("after_create", "sqlite", SQLITE_TRIGGER_STATEMENTS)
_install("before_drop", "sqlite", DROP_SQLITE_TRIGGER_STATEMENTS)


class TraceRow(Base, UserAttributedColumns, StandardColumns):
    """One `C-41` `stage_record` call, persisted — the one trace shape
    every stage of the product uses (`plan.md §5.9`). Append-only; no
    `version` column."""

    __tablename__ = "trace_row"
    __table_args__ = (
        CheckConstraint(f"decider IN ({_sql_in_list(_DECIDERS)})", name="decider_valid"),
    )

    subject_type: Mapped[str] = mapped_column(String(_SUBJECT_TYPE_MAX_LENGTH), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    stage: Mapped[str] = mapped_column(String(_STAGE_MAX_LENGTH), nullable=False)
    decider: Mapped[str] = mapped_column(String(_DECIDER_MAX_LENGTH), nullable=False)
    inputs: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    outputs: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    rule_or_prompt_version: Mapped[str | None] = mapped_column(
        String(_RULE_OR_PROMPT_VERSION_MAX_LENGTH), nullable=True
    )
    thresholds_compared: Mapped[list[dict[str, object]]] = mapped_column(
        PortableJSON, nullable=False
    )
    confidence: Mapped[Decimal] = mapped_column(FractionValue(), nullable=False)
    sources: Mapped[list[object] | None] = mapped_column(PortableJSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)


class ThresholdSnapshot(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One frozen set of business thresholds — every record that decided
    anything names the snapshot in force at the time (`plan.md §5.9`)."""

    __tablename__ = "threshold_snapshot"

    values: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    source: Mapped[str] = mapped_column(String(_SOURCE_MAX_LENGTH), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    proposed_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConfigVersion(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One applied configuration change — secrets are never stored, only
    their presence, via `values_redacted` (`plan.md §5.9`)."""

    __tablename__ = "config_version"

    values_redacted: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    applied_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    checksum: Mapped[str] = mapped_column(String(_CHECKSUM_MAX_LENGTH), nullable=False)
