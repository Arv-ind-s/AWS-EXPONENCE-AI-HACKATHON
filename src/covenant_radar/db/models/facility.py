"""Facility tables: `plan.md §5.2`'s `facility` and `facility_conduct`.

`facility` is effective-dated: a limit change never overwrites a row, it
inserts a new one and closes the row it replaces (`effective_to` and
`superseded_by_id`). `Facility.supersede` is the one place that happens,
so the invariant it protects — a successor's `effective_from` can never
precede its predecessor's — is enforced once, in Python, ahead of the
insert, the same way `Portfolio.create`/`move_to` guard the materialised
path (`T-007`). `reference` is unique and never reused **per row**: a
limit change gets its own new reference, chained to its predecessor by
`superseded_by_id`, never a rewrite of the old one.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Numeric, TypeDecorator, TypeEngine

from covenant_radar.core.errors import ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, MoneyAmount

_REFERENCE_MAX_LENGTH = 24
_FACILITY_TYPE_MAX_LENGTH = 50
_CURRENCY_MAX_LENGTH = 3
_SECURITY_TYPE_MAX_LENGTH = 100

_PERCENTAGE_PRECISION = 7
_PERCENTAGE_SCALE = 4
_PERCENTAGE_QUANTUM = Decimal("1").scaleb(-_PERCENTAGE_SCALE)


class _PercentageValue(TypeDecorator[Decimal]):
    """A percentage, stored as the number and not the fraction — `87.5000`
    means 87.5%, never `0.8750` (`plan.md §5`'s convention, documented
    here per column as that convention requires).

    `db/types.py` (`T-006`) is not this task's file to change, and its
    `MoneyAmount` type is fixed at `numeric(18,4)` and named for money
    specifically. `utilisation_pct` needs the same cross-engine fixed-point
    treatment — SQLite has no decimal storage class of its own, and a bare
    `Numeric` column would let SQLite's float affinity silently corrupt the
    value on write — so this type repeats `MoneyAmount`'s SQLite-as-text
    technique at the precision a percentage needs, rather than repurpose a
    type whose name and error messages say "money". Refuses a non-`Decimal`
    value for the same reason.
    """

    impl = Numeric(_PERCENTAGE_PRECISION, _PERCENTAGE_SCALE, asdecimal=True)
    cache_ok = True
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(
            Numeric(_PERCENTAGE_PRECISION, _PERCENTAGE_SCALE, asdecimal=True)
        )

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Decimal | str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(
                f"Percentage columns require a Decimal, not {type(value).__name__} ({value!r})."
            )
        quantized = value.quantize(_PERCENTAGE_QUANTUM)
        return format(quantized, "f") if dialect.name == "sqlite" else quantized

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


#: Fields `Facility.supersede` carries over from the predecessor unless the
#: caller overrides them — everything about a facility except its identity
#: and its effective-dating columns.
_COPIED_ON_SUPERSEDE: Final[tuple[str, ...]] = (
    "borrower_id",
    "facility_type",
    "sanctioned_limit",
    "currency",
    "drawing_power",
    "outstanding",
    "security_type",
    "pricing_bps",
    "sanction_date",
    "maturity_date",
)


class Facility(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One effective-dated version of a credit facility."""

    __tablename__ = "facility"
    __table_args__ = (
        Index("ix_facility_borrower_id_effective_from", "borrower_id", "effective_from"),
    )

    reference: Mapped[str] = mapped_column(
        String(_REFERENCE_MAX_LENGTH), nullable=False, unique=True
    )
    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False
    )
    facility_type: Mapped[str] = mapped_column(String(_FACILITY_TYPE_MAX_LENGTH), nullable=False)
    sanctioned_limit: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    currency: Mapped[str] = mapped_column(String(_CURRENCY_MAX_LENGTH), nullable=False)
    drawing_power: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    outstanding: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    security_type: Mapped[str | None] = mapped_column(
        String(_SECURITY_TYPE_MAX_LENGTH), nullable=True
    )
    pricing_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sanction_date: Mapped[date] = mapped_column(Date, nullable=False)
    maturity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("facility.id", ondelete="RESTRICT"), nullable=True
    )

    @classmethod
    def supersede(
        cls,
        predecessor: Facility,
        *,
        reference: str,
        effective_from: date,
        created_at: datetime,
        updated_at: datetime,
        request_id: str,
        created_by_id: UUID | None = None,
        updated_by_id: UUID | None = None,
        **overrides: Any,
    ) -> Facility:
        """Close `predecessor` and return the new row that replaces it.

        `overrides` supplies only the fields that changed — a limit
        increase passes `sanctioned_limit=...` and nothing else; every
        other field in `_COPIED_ON_SUPERSEDE` carries over from
        `predecessor` unchanged. Refuses, before mutating anything, an
        `effective_from` that precedes the predecessor's own, naming both
        rows: a successor can never start before the version it replaces.
        """
        if effective_from < predecessor.effective_from:
            raise ValidationError(
                f"Facility {reference!r}'s effective_from ({effective_from}) precedes "
                f"predecessor {predecessor.reference!r}'s effective_from "
                f"({predecessor.effective_from}).",
                field="facility.effective_from",
            )
        unknown = set(overrides) - set(_COPIED_ON_SUPERSEDE)
        if unknown:
            raise TypeError(f"supersede() got unexpected field(s): {sorted(unknown)}")

        values: dict[str, Any] = {
            field: getattr(predecessor, field) for field in _COPIED_ON_SUPERSEDE
        }
        values.update(overrides)

        successor_id = new_id()
        successor = cls(
            id=successor_id,
            reference=reference,
            effective_from=effective_from,
            effective_to=None,
            superseded_by_id=None,
            created_at=created_at,
            updated_at=updated_at,
            request_id=request_id,
            created_by_id=created_by_id,
            updated_by_id=updated_by_id,
            **values,
        )
        predecessor.effective_to = effective_from
        predecessor.superseded_by_id = successor_id
        return successor


class FacilityConduct(Base, UserAttributedColumns, StandardColumns):
    """One day's account-conduct snapshot for a facility — the SMA input
    (`plan.md §5.2`).

    Ingested, not user-edited, so it carries no optimistic-concurrency
    `version` column; idempotence for re-ingestion comes from the unique
    constraint on `(facility_id, as_of_date)` instead — inserting the same
    day twice is a duplicate, not a conflict to resolve.
    """

    __tablename__ = "facility_conduct"
    __table_args__ = (
        UniqueConstraint("facility_id", "as_of_date", name="uq_facility_conduct_facility_day"),
        Index("ix_facility_conduct_as_of_date", "as_of_date"),
    )

    facility_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("facility.id", ondelete="CASCADE"), nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    outstanding: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    utilisation_pct: Mapped[Decimal | None] = mapped_column(_PercentageValue, nullable=True)
    days_past_due: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    overdue_amount: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    excess_amount: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    source_id: Mapped[UUID | None] = mapped_column(GUID, nullable=True)
