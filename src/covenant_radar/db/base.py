"""The shared declarative base, its constraint-naming convention, and the
columns every table in the application carries.

`plan.md §5`'s conventions, applied once here rather than repeated on each
model: a UUIDv7 primary key, aware-UTC provenance timestamps, the actor and
request that made each write, and (opt in, for entities a person edits
directly) an optimistic-concurrency version column.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from covenant_radar.core.ids import new_id
from covenant_radar.db.types import GUID, AwareDateTime

# Alembic (`T-010`) relies on these names to generate a stable migration
# for every constraint and index instead of a database-assigned one.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_REQUEST_ID_MAX_LENGTH = 40


class Base(DeclarativeBase):
    """The declarative base shared by every ORM model in the application."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class StandardColumns:
    """The columns every table carries, mixed in alongside `Base`.

    `created_by_id` and `updated_by_id` are left here as bare identifiers,
    with no foreign key to `app_user`: that table is defined three layers
    above this one (`T-007`), and a foundational, reusable mixin should not
    depend on one specific downstream table's name. Each concrete model
    that carries these columns adds the foreign key to `app_user.id`
    itself, once that table exists.
    """

    id: Mapped[UUID] = mapped_column(GUID, primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    created_by_id: Mapped[UUID | None] = mapped_column(GUID, nullable=True)
    updated_by_id: Mapped[UUID | None] = mapped_column(GUID, nullable=True)
    request_id: Mapped[str] = mapped_column(String(_REQUEST_ID_MAX_LENGTH), nullable=False)


class VersionedColumns:
    """Optimistic concurrency for entities a person edits directly.

    Not every table carries this — only the user-editable ones
    (`plan.md §3.3`) — so it is a separate mixin rather than part of
    `StandardColumns`.
    """

    version: Mapped[int] = mapped_column(nullable=False, default=1)
