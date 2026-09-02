"""`industry_reference`: the seeded industry taxonomy `borrower.industry_code`
resolves against (`plan.md §5.2`).

`T-011` loads and versions the actual rows (a newer `taxonomy_version`
supersedes rather than overwrites, retiring what it replaces); this task
declares only the table's shape.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns
from covenant_radar.db.models.identity import UserAttributedColumns

_CODE_MAX_LENGTH = 20
_NAME_MAX_LENGTH = 200
_TAXONOMY_VERSION_MAX_LENGTH = 20


class IndustryReference(Base, UserAttributedColumns, StandardColumns):
    """One node in the industry classification hierarchy.

    Seeded (`T-011`), never created through the application — the same
    reasoning that leaves `identity.Permission` without an optimistic-
    concurrency `version` column applies here too.
    """

    __tablename__ = "industry_reference"

    code: Mapped[str] = mapped_column(String(_CODE_MAX_LENGTH), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(_NAME_MAX_LENGTH), nullable=False)
    parent_code: Mapped[str | None] = mapped_column(
        String(_CODE_MAX_LENGTH),
        ForeignKey("industry_reference.code", ondelete="RESTRICT"),
        nullable=True,
    )
    taxonomy_version: Mapped[str] = mapped_column(
        String(_TAXONOMY_VERSION_MAX_LENGTH), nullable=False
    )
