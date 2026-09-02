"""`organisation`: one row per deployment (`plan.md §5.1`).

The fiscal-year start month drives every quarter calculation in the
product (`FY27Q2` and its kin), so it is validated at the database level
rather than trusted to whoever seeds the row.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models.identity import UserAttributedColumns

_NAME_MAX_LENGTH = 200
_SHORT_CODE_MAX_LENGTH = 20
_REGULATORY_ID_MAX_LENGTH = 50


class Organisation(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """The single deployment-level organisation record."""

    __tablename__ = "organisation"
    __table_args__ = (
        CheckConstraint(
            "fiscal_year_start_month BETWEEN 1 AND 12",
            name="fiscal_year_start_month_range",
        ),
    )

    name: Mapped[str] = mapped_column(String(_NAME_MAX_LENGTH), nullable=False)
    short_code: Mapped[str] = mapped_column(
        String(_SHORT_CODE_MAX_LENGTH), nullable=False, unique=True
    )
    regulatory_id: Mapped[str | None] = mapped_column(
        String(_REGULATORY_ID_MAX_LENGTH), nullable=True
    )
    fiscal_year_start_month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
