"""Covenant tables: `plan.md §5.5`'s `covenant`, `covenant_version`,
`covenant_exception`, `covenant_waiver`, `covenant_test`, `covenant_schedule`
and `ratio_definition`.

**The immutability rule.** Once a `covenant_version` has been tested even
once (`tested_at_least_once`), its terms are frozen: a database trigger
refuses an `UPDATE` that touches any column other than `status` and
`effective_to`. This is the *first* of three enforcement points `spec
§R-05.a` requires — `T-031` supplies the second (no repository method
exists to change a frozen column) and the third (a test that proves both);
this module supplies only the one a stray `UPDATE` statement, run from
anywhere, cannot get past.

The trigger's protected-column set is `covenant_version`'s own domain
columns (`plan.md §5.5`'s "Key fields") minus `status` and `effective_to`.
It deliberately excludes the bookkeeping columns every table carries
(`updated_at`, `updated_by_id`, `request_id`, `version`, from `db/base.py`'s
mixins): those change on every write, including the one legitimate write
this trigger still allows, and are not part of the covenant's terms the
rule protects. `id`, `created_at` and `created_by_id` are immutable by
convention everywhere in this schema and are likewise not this trigger's
concern.

Written once per engine because `plan.md §5`'s "no dialect branching in a
model" rule is about column *types*, not procedural DDL — PL/pgSQL and
SQLite's trigger dialect have no common subset expressive enough for this
rule, so both are written out in full and proven against both engines in
`tests/unit/test_model_domain.py`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models._decimal import PercentageValue, RatioValue
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, AwareDateTime, PortableJSON

_REFERENCE_MAX_LENGTH = 20
_NAME_MAX_LENGTH = 300
_COVENANT_CLASS_MAX_LENGTH = 50
_DEFINITION_REF_MAX_LENGTH = 20
_DIRECTION_MAX_LENGTH = 4
_UNIT_MAX_LENGTH = 20
_FREQUENCY_MAX_LENGTH = 20
_TEST_BASIS_MAX_LENGTH = 20
_STATUS_MAX_LENGTH = 20
_VERDICT_MAX_LENGTH = 20
_SCHEDULE_STATE_MAX_LENGTH = 20
_REASON_MAX_LENGTH = 2000
_SCOPE_MAX_LENGTH = 100
_WAIVER_STATE_MAX_LENGTH = 20
_NOT_COMPUTABLE_REASON_MAX_LENGTH = 100
_CODE_MAX_LENGTH = 20
_TAXONOMY_VERSION_MAX_LENGTH = 20

_DIRECTIONS = ("min", "max")
_FREQUENCIES = ("monthly", "quarterly", "half_yearly", "annual", "on_event")
_STATUSES = ("draft", "pending_approval", "live", "superseded", "retired")
_VERDICTS = ("pass", "warning", "breach", "breach_cure_open", "stale", "not_computable")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Covenant(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A covenant's stable identity across every version it is ever
    amended into (`plan.md §5.5`)."""

    __tablename__ = "covenant"

    reference: Mapped[str] = mapped_column(
        String(_REFERENCE_MAX_LENGTH), nullable=False, unique=True
    )
    facility_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(_NAME_MAX_LENGTH), nullable=False)
    covenant_class: Mapped[str] = mapped_column(String(_COVENANT_CLASS_MAX_LENGTH), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CovenantVersion(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One dated set of terms for a `Covenant`. Frozen, in every column
    but `status` and `effective_to`, the moment `tested_at_least_once`
    turns true — see this module's docstring for the trigger that enforces
    it."""

    __tablename__ = "covenant_version"
    __table_args__ = (
        CheckConstraint(f"direction IN ({_sql_in_list(_DIRECTIONS)})", name="direction_valid"),
        CheckConstraint(f"frequency IN ({_sql_in_list(_FREQUENCIES)})", name="frequency_valid"),
        CheckConstraint(f"status IN ({_sql_in_list(_STATUSES)})", name="status_valid"),
        UniqueConstraint("covenant_id", "version_no", name="uq_covenant_version_covenant_no"),
        # `covenant_version(facility_id, status)` (`plan.md §5.5`'s Indexes)
        # is reached "via the parent join" per that same line — this table
        # carries no `facility_id` of its own (`Covenant` does, and is
        # already indexed on it above) — so the index that actually serves
        # that join-then-filter access path is this one instead.
        Index("ix_covenant_version_covenant_id_status", "covenant_id", "status"),
    )

    covenant_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("covenant.id", ondelete="RESTRICT"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_ref: Mapped[str | None] = mapped_column(
        String(_DEFINITION_REF_MAX_LENGTH),
        ForeignKey("ratio_definition.code", ondelete="RESTRICT"),
        nullable=True,
    )
    custom_formula: Mapped[str | None] = mapped_column(Text, nullable=True)
    threshold: Mapped[Decimal] = mapped_column(RatioValue(), nullable=False)
    direction: Mapped[str] = mapped_column(String(_DIRECTION_MAX_LENGTH), nullable=False)
    unit: Mapped[str] = mapped_column(String(_UNIT_MAX_LENGTH), nullable=False)
    frequency: Mapped[str] = mapped_column(String(_FREQUENCY_MAX_LENGTH), nullable=False)
    test_basis: Mapped[str] = mapped_column(String(_TEST_BASIS_MAX_LENGTH), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    warning_headroom_pct: Mapped[Decimal | None] = mapped_column(PercentageValue(), nullable=True)
    cure_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    grace_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    source_document_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("document.id", ondelete="RESTRICT"), nullable=True
    )
    source_span_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("document_span.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(_STATUS_MAX_LENGTH), nullable=False, default="draft")
    tested_at_least_once: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    registered_by_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


#: `CovenantVersion`'s own domain columns, minus `status` and
#: `effective_to` — the set the immutability trigger refuses to see
#: change once `tested_at_least_once` is true. Built from the model's own
#: mapped columns rather than typed out a second time, so the trigger can
#: never silently drift from the table it protects.
_ALWAYS_ALLOWED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"status", "effective_to", "updated_at", "updated_by_id", "request_id", "version"}
)
_COVENANT_VERSION_PROTECTED_COLUMNS: Final[tuple[str, ...]] = tuple(
    column.name
    for column in CovenantVersion.__table__.columns
    if column.name not in _ALWAYS_ALLOWED_COLUMNS
)

#: Each joined with an embedded newline, so the *rendered SQL* wraps one
#: column per line while every line of *this source file* stays short.
_POSTGRESQL_NEW_ROW = ",\n                ".join(
    f"NEW.{name}" for name in _COVENANT_VERSION_PROTECTED_COLUMNS
)
_POSTGRESQL_OLD_ROW = ",\n                ".join(
    f"OLD.{name}" for name in _COVENANT_VERSION_PROTECTED_COLUMNS
)

#: Two single-statement DDL strings, never one multi-statement string —
#: SQLite's own DBAPI driver refuses more than one statement per `execute`
#: call, so a portable trigger installer has to assume every driver might
#: (see `audit.py`'s `_install`, which this module's `_install` mirrors).
#:
#: Public (not underscore-prefixed): `T-010`'s initial migration imports
#: these verbatim rather than hand-copying the trigger SQL a second time,
#: so the migration and the ORM event listener below can never drift apart.
POSTGRESQL_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    f"""
    CREATE FUNCTION covenant_version_immutable() RETURNS trigger AS $$
    BEGIN
        IF OLD.tested_at_least_once AND (
            (
                {_POSTGRESQL_NEW_ROW}
            ) IS DISTINCT FROM (
                {_POSTGRESQL_OLD_ROW}
            )
        ) THEN
            RAISE EXCEPTION
                'covenant_version % is immutable once tested', OLD.id
                USING HINT = 'Only status and effective_to may change.';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_covenant_version_immutable
    BEFORE UPDATE ON covenant_version
    FOR EACH ROW EXECUTE FUNCTION covenant_version_immutable();
    """,
)

DROP_POSTGRESQL_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    "DROP TRIGGER IF EXISTS trg_covenant_version_immutable ON covenant_version;",
    "DROP FUNCTION IF EXISTS covenant_version_immutable();",
)

_SQLITE_CHANGED_CLAUSE = "\n        OR ".join(
    f"NEW.{name} IS NOT OLD.{name}" for name in _COVENANT_VERSION_PROTECTED_COLUMNS
)

SQLITE_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    f"""
    CREATE TRIGGER trg_covenant_version_immutable
    BEFORE UPDATE ON covenant_version
    FOR EACH ROW
    WHEN OLD.tested_at_least_once AND (
        {_SQLITE_CHANGED_CLAUSE}
    )
    BEGIN
        SELECT RAISE(ABORT, 'covenant_version is immutable once tested');
    END;
    """,
)

DROP_SQLITE_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    "DROP TRIGGER IF EXISTS trg_covenant_version_immutable;",
)


def _install(table_event: str, dialect: str, statements: tuple[str, ...]) -> None:
    """Register each of `statements` as its own single-statement `DDL`
    event against `CovenantVersion.__table__`, in order, for `dialect`
    only."""
    for statement in statements:
        # SQLAlchemy's `DDL.__init__` carries no type annotations of its
        # own (`sqlalchemy.sql.ddl`), so this call is flagged under
        # `check_untyped_defs` regardless of the argument's own type.
        ddl = DDL(statement)  # type: ignore[no-untyped-call]
        event.listen(CovenantVersion.__table__, table_event, ddl.execute_if(dialect=dialect))


_install("after_create", "postgresql", POSTGRESQL_TRIGGER_STATEMENTS)
_install("before_drop", "postgresql", DROP_POSTGRESQL_TRIGGER_STATEMENTS)
_install("after_create", "sqlite", SQLITE_TRIGGER_STATEMENTS)
_install("before_drop", "sqlite", DROP_SQLITE_TRIGGER_STATEMENTS)


class CovenantException(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A dated relaxation of one `CovenantVersion`'s threshold, carried by
    the version rather than requiring a whole new one (`plan.md §5.5`).

    `from_period`/`to_period` name the financial-period label
    (`FY27Q2`-style) the relaxation covers, the same string
    `financial_period.fy_label` will use once statement-domain modelling
    (`plan.md §5.3`) exists; that table is out of this task's scope, so the
    period is carried here as a plain label rather than a foreign key with
    no target yet.
    """

    __tablename__ = "covenant_exception"

    covenant_version_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("covenant_version.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    from_period: Mapped[str] = mapped_column(String(_CODE_MAX_LENGTH), nullable=False)
    to_period: Mapped[str] = mapped_column(String(_CODE_MAX_LENGTH), nullable=False)
    relaxed_threshold: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("document.id", ondelete="RESTRICT"), nullable=True
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


class CovenantWaiver(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A dated waiver against a `Covenant` itself, not any one version
    (`plan.md §5.5`)."""

    __tablename__ = "covenant_waiver"

    covenant_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("covenant.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(_SCOPE_MAX_LENGTH), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("document.id", ondelete="RESTRICT"), nullable=True
    )
    requested_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(_WAIVER_STATE_MAX_LENGTH), nullable=False, default="requested"
    )


class CovenantTest(Base, UserAttributedColumns, StandardColumns):
    """One computed test of one `CovenantVersion` as of one date
    (`plan.md §5.5`). Written once by the engine that computed it, never
    edited by a person, so it carries no `version` column.

    `period_id` names the financial period the test drew its inputs from;
    like `CovenantException.from_period`, `financial_period` (`plan.md
    §5.3`) is out of this task's scope, so it is a bare identifier here,
    the same pattern `db/base.py`'s `StandardColumns.created_by_id`
    documents for a target table that does not exist yet at this layer.
    """

    __tablename__ = "covenant_test"
    __table_args__ = (
        CheckConstraint(f"verdict IN ({_sql_in_list(_VERDICTS)})", name="verdict_valid"),
        Index(
            "ix_covenant_test_covenant_version_id_as_of_date",
            "covenant_version_id",
            "as_of_date",
        ),
        Index("ix_covenant_test_as_of_date_verdict", "as_of_date", "verdict"),
    )

    covenant_version_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("covenant_version.id", ondelete="RESTRICT"), nullable=False
    )
    period_id: Mapped[UUID | None] = mapped_column(GUID, nullable=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    threshold_used: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    headroom_pct: Mapped[Decimal | None] = mapped_column(PercentageValue(), nullable=True)
    verdict: Mapped[str] = mapped_column(String(_VERDICT_MAX_LENGTH), nullable=False)
    exception_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("covenant_exception.id", ondelete="RESTRICT"), nullable=True
    )
    waiver_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("covenant_waiver.id", ondelete="RESTRICT"), nullable=True
    )
    cure_ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    inputs: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    not_computable_reason: Mapped[str | None] = mapped_column(
        String(_NOT_COMPUTABLE_REASON_MAX_LENGTH), nullable=True
    )
    computed_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    job_run_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("job_run.id", ondelete="RESTRICT"), nullable=True
    )


class CovenantSchedule(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """The testing calendar `R-09` and `R-28` both read: one due date per
    `CovenantVersion` occurrence, resolving to the test and certificate
    that ultimately satisfy it (`plan.md §5.5`)."""

    __tablename__ = "covenant_schedule"
    __table_args__ = (Index("ix_covenant_schedule_due_date_state", "due_date", "state"),)

    covenant_version_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("covenant_version.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(
        String(_SCHEDULE_STATE_MAX_LENGTH), nullable=False, default="pending"
    )
    test_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("covenant_test.id", ondelete="RESTRICT"), nullable=True
    )
    certificate_id: Mapped[UUID | None] = mapped_column(
        GUID,
        # `certificate_request.covenant_schedule_id` (`signal.py`) points
        # back at this table, so the pair forms a two-table reference
        # cycle: `use_alter` defers this one FK to an `ALTER TABLE` issued
        # after both tables exist, which is the only way PostgreSQL (and
        # SQL generally) can create either table at all.
        ForeignKey(
            "certificate_request.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_covenant_schedule_certificate_id_certificate_request",
        ),
        nullable=True,
    )


class RatioDefinition(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One of the 24 library ratio definitions, as data — so the plausible
    band `C-30` checks against is configuration, never a literal
    (`plan.md §5.5`). Seeded (`T-011`), amended through the application
    like any other user-editable row."""

    __tablename__ = "ratio_definition"

    code: Mapped[str] = mapped_column(String(_CODE_MAX_LENGTH), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(_NAME_MAX_LENGTH), nullable=False)
    formula_text: Mapped[str] = mapped_column(Text, nullable=False)
    required_lines: Mapped[list[str]] = mapped_column(PortableJSON, nullable=False)
    unit: Mapped[str] = mapped_column(String(_UNIT_MAX_LENGTH), nullable=False)
    plausible_min: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    plausible_max: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    direction_hint: Mapped[str | None] = mapped_column(String(_DIRECTION_MAX_LENGTH), nullable=True)
    taxonomy_version: Mapped[str] = mapped_column(
        String(_TAXONOMY_VERSION_MAX_LENGTH), nullable=False
    )
