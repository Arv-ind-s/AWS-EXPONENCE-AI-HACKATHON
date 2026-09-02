"""Identity and access tables: `plan.md §5.1`'s `app_user`, `role`,
`permission`, `role_permission`, `user_role`, `user_portfolio_scope`,
`user_session` and `api_key`.

`user_portfolio_scope`'s absence for a user means **no access, never all
access** — there is no "grants everything" flag anywhere in this module,
by design: the only way to read another portfolio's data is a row that
says so, which is exactly what `T-016`'s scope predicate depends on.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.types import GUID, AwareDateTime, PortableJSON

_USERNAME_MAX_LENGTH = 64
_EMAIL_MAX_LENGTH = 254
_FULL_NAME_MAX_LENGTH = 200
_PASSWORD_HASH_MAX_LENGTH = 255
_AUTH_SOURCE_MAX_LENGTH = 10
_EXTERNAL_SUBJECT_MAX_LENGTH = 255
_LOCALE_MAX_LENGTH = 10
_THEME_MAX_LENGTH = 10
_ROLE_CODE_MAX_LENGTH = 50
_ROLE_NAME_MAX_LENGTH = 100
_PERMISSION_CODE_MAX_LENGTH = 100
_PERMISSION_DESCRIPTION_MAX_LENGTH = 500
_HASH_MAX_LENGTH = 128
_API_KEY_NAME_MAX_LENGTH = 100
_API_KEY_PREFIX_MAX_LENGTH = 16

_AUTH_SOURCES = ("local", "oidc", "saml")
_LOCALES = ("en", "hi")
_THEMES = ("light", "dark")


def _sql_in_list(values: tuple[str, ...]) -> str:
    """Render `values` as a SQL ``IN`` list literal, e.g. ``'a', 'b'``."""
    return ", ".join(f"'{value}'" for value in values)


class UserAttributedColumns:
    """Overrides `StandardColumns.created_by_id`/`updated_by_id` with the
    foreign key to `app_user.id` that `db/base.py` deliberately leaves off
    — that module sits below `app_user` and should not know its name.

    Every concrete model in `db/models/` mixes this in ahead of
    `StandardColumns` in its base list so these definitions win: Python's
    normal attribute resolution order means the first class in the MRO to
    define `created_by_id`/`updated_by_id` is the one SQLAlchemy maps.
    """

    created_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    updated_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


class AppUser(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A person who can sign in, locally or through the bank's identity
    provider (`T-013`, `T-014`).

    `password_hash` is `NULL` for an SSO-only account; `mfa_secret_enc` is
    written only once field encryption exists (`T-017`) and is opaque text
    until then. `email` is unique only while `is_active` is true, so a
    deactivated account's address can be reused by a new hire without a
    manual cleanup step.
    """

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(
            f"auth_source IN ({_sql_in_list(_AUTH_SOURCES)})", name="auth_source_valid"
        ),
        CheckConstraint(f"locale IN ({_sql_in_list(_LOCALES)})", name="locale_valid"),
        CheckConstraint(f"theme IN ({_sql_in_list(_THEMES)})", name="theme_valid"),
        CheckConstraint("failed_attempts >= 0", name="failed_attempts_non_negative"),
        Index(
            "uq_app_user_email_active",
            "email",
            unique=True,
            sqlite_where=text("is_active"),
            postgresql_where=text("is_active"),
        ),
    )

    username: Mapped[str] = mapped_column(
        String(_USERNAME_MAX_LENGTH), nullable=False, unique=True
    )
    email: Mapped[str] = mapped_column(String(_EMAIL_MAX_LENGTH), nullable=False)
    full_name: Mapped[str] = mapped_column(String(_FULL_NAME_MAX_LENGTH), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(
        String(_PASSWORD_HASH_MAX_LENGTH), nullable=True
    )
    auth_source: Mapped[str] = mapped_column(
        String(_AUTH_SOURCE_MAX_LENGTH), nullable=False, default="local"
    )
    external_subject: Mapped[str | None] = mapped_column(
        String(_EXTERNAL_SUBJECT_MAX_LENGTH), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locale: Mapped[str] = mapped_column(String(_LOCALE_MAX_LENGTH), nullable=False, default="en")
    theme: Mapped[str] = mapped_column(String(_THEME_MAX_LENGTH), nullable=False, default="light")


class Role(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A named bundle of permissions. The seven system roles from
    `spec §16.1` are seeded (`T-011`) with `is_system=True`; custom roles
    an administrator defines are not.
    """

    __tablename__ = "role"

    code: Mapped[str] = mapped_column(String(_ROLE_CODE_MAX_LENGTH), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(_ROLE_NAME_MAX_LENGTH), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Permission(Base, UserAttributedColumns, StandardColumns):
    """One member of the enumerated permission set `T-015` declares in
    code. Seeded (`T-011`); never created through the application."""

    __tablename__ = "permission"

    code: Mapped[str] = mapped_column(
        String(_PERMISSION_CODE_MAX_LENGTH), nullable=False, unique=True
    )
    description: Mapped[str] = mapped_column(
        String(_PERMISSION_DESCRIPTION_MAX_LENGTH), nullable=False
    )


class RolePermission(Base, UserAttributedColumns, StandardColumns):
    """One permission granted to one role. A pure join with no meaning of
    its own, so both foreign keys cascade with their parent."""

    __tablename__ = "role_permission"
    __table_args__ = (UniqueConstraint("role_id", "permission_id"),)

    role_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("role.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("permission.id", ondelete="CASCADE"), nullable=False
    )


class UserRole(Base, UserAttributedColumns, StandardColumns):
    """One role granted to one user. Revocation deletes the row outright —
    there is no `revoked_at` here, because the grant and revoke events
    themselves are what `audit_event` records; this table only ever
    reflects who currently holds what.
    """

    __tablename__ = "user_role"
    __table_args__ = (UniqueConstraint("user_id", "role_id"),)

    user_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("role.id", ondelete="CASCADE"), nullable=False
    )
    granted_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    granted_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)


class UserPortfolioScope(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """The row-level scope: a user may read a portfolio's data only
    because a row here says so. Absence is the default, and the default
    is **no access** — `T-016`'s scope predicate is built on that being
    true for every user, in every state, with no exception column.
    """

    __tablename__ = "user_portfolio_scope"
    __table_args__ = (UniqueConstraint("user_id", "portfolio_id"),)

    user_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("portfolio.id", ondelete="CASCADE"), nullable=False
    )
    include_descendants: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class UserSession(Base, UserAttributedColumns, StandardColumns):
    """A signed, HttpOnly, SameSite session (`T-013`). `token_hash` is the
    only trace of the token the database ever holds — the cookie's actual
    value is never written here, so a database dump cannot be replayed as
    a live session.

    No `version` column: a session is refreshed on effectively every
    request (`last_seen_at`), and optimistic concurrency on a row that hot
    would just manufacture spurious conflicts instead of preventing real
    ones.
    """

    __tablename__ = "user_session"

    user_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(_HASH_MAX_LENGTH), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(_HASH_MAX_LENGTH), nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(_HASH_MAX_LENGTH), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)


class ApiKey(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A service-to-service credential. `key_hash` is the only trace of
    the secret the database holds; the key itself is shown once, at
    creation, and never again — `prefix` is what the console displays
    afterwards so an administrator can tell keys apart without seeing the
    secret.
    """

    __tablename__ = "api_key"
    __table_args__ = (CheckConstraint("rate_limit_per_min > 0", name="rate_limit_positive"),)

    name: Mapped[str] = mapped_column(String(_API_KEY_NAME_MAX_LENGTH), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(_HASH_MAX_LENGTH), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(_API_KEY_PREFIX_MAX_LENGTH), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(PortableJSON, nullable=False)
    portfolio_scope: Mapped[list[str] | None] = mapped_column(PortableJSON, nullable=True)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
