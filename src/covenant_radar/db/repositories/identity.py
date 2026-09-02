"""SQLAlchemy adapters for local authentication and role resolution.

The security services deliberately use persistence-neutral records.  This
module is the production bridge to ``app_user`` and ``user_session`` and is
kept small so browser authentication remains in the same request transaction
as its audit event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.db.models.identity import (
    AppUser,
    Permission,
    Role,
    RolePermission,
    UserRole,
    UserSession,
)
from covenant_radar.db.session import is_database_session
from covenant_radar.security.sessions import SessionRecord
from covenant_radar.services.auth import UserRecord


class SqlAlchemyIdentityStore:
    """Current-transaction identity, session, and role-permission adapter."""

    def __init__(self, session: Session) -> None:
        if not is_database_session(session):
            raise TypeError("SqlAlchemyIdentityStore requires a SQLAlchemy Session.")
        self.session = session

    def find_by_username(self, username: str) -> UserRecord | None:
        row = self.session.execute(
            select(AppUser).where(AppUser.username == username).limit(1)
        ).scalar_one_or_none()
        return _user_record(row) if row is not None else None

    def get(self, user_id: UUID) -> UserRecord | None:
        row = self.session.get(AppUser, user_id)
        return _user_record(row) if row is not None else None

    def create(self, record: SessionRecord) -> None:
        self.session.add(
            UserSession(
                id=record.id,
                user_id=record.user_id,
                token_hash=record.token_hash,
                issued_at=record.issued_at,
                last_seen_at=record.last_seen_at,
                expires_at=record.expires_at,
                absolute_expires_at=record.absolute_expires_at,
                ip_hash=record.ip_hash,
                user_agent_hash=record.user_agent_hash,
                revoked_at=record.revoked_at,
                created_at=record.issued_at,
                updated_at=record.last_seen_at,
                created_by_id=record.user_id,
                updated_by_id=record.user_id,
                request_id=_request_id(),
            )
        )

    def get_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        row = self.session.execute(
            select(UserSession).where(UserSession.token_hash == token_hash).limit(1)
        ).scalar_one_or_none()
        return _session_record(row) if row is not None else None

    def save_session(self, record: SessionRecord) -> None:
        row = self.session.get(UserSession, record.id)
        if row is None or row.token_hash != record.token_hash:
            raise KeyError("Cannot update a session that does not exist.")
        row.last_seen_at = record.last_seen_at
        row.expires_at = record.expires_at
        row.absolute_expires_at = record.absolute_expires_at
        row.ip_hash = record.ip_hash
        row.user_agent_hash = record.user_agent_hash
        row.revoked_at = record.revoked_at
        row.updated_at = record.last_seen_at
        row.updated_by_id = record.user_id
        row.request_id = _request_id()

    # ``SessionStore`` calls this method name.  It intentionally delegates
    # rather than overloading the user-store ``save`` method above.
    def save(self, value: UserRecord | SessionRecord) -> None:
        if isinstance(value, UserRecord):
            self._save_user(value)
            return
        if isinstance(value, SessionRecord):
            self.save_session(value)
            return
        raise TypeError("Identity store can persist only UserRecord or SessionRecord values.")

    def _save_user(self, user: UserRecord) -> None:
        row = self.session.get(AppUser, user.id)
        if row is None:
            raise KeyError("Cannot update a user that does not exist.")
        row.password_hash = user.password_hash
        row.is_active = user.is_active
        row.failed_attempts = user.failed_attempts
        row.locked_until = user.locked_until
        row.password_changed_at = user.password_changed_at
        row.must_change_password = user.must_change_password
        row.mfa_secret_enc = user.mfa_secret_enc
        row.updated_at = _updated_at(user)
        row.updated_by_id = user.id
        row.request_id = _request_id()

    def revoke_all(self, user_id: UUID, revoked_at: datetime) -> int:
        result = self.session.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(
                revoked_at=revoked_at,
                updated_at=revoked_at,
                updated_by_id=user_id,
                request_id=_request_id(),
            )
        )
        return int(result.rowcount or 0)

    def permissions_for_user(self, user_id: UUID) -> tuple[str, ...]:
        statement = (
            select(Permission.code)
            .select_from(UserRole)
            .join(AppUser, AppUser.id == UserRole.user_id)
            .join(RolePermission, RolePermission.role_id == UserRole.role_id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(UserRole.user_id == user_id, AppUser.is_active.is_(True))
            .order_by(Permission.code)
        )
        return tuple(self.session.execute(statement).scalars().all())

    def permissions_by_role(self) -> dict[str, tuple[str, ...]]:
        statement = (
            select(Role.code, Permission.code)
            .select_from(Role)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(Permission, Permission.id == RolePermission.permission_id)
            .order_by(Role.code, Permission.code)
        )
        assignments: dict[str, list[str]] = {}
        for role_code, permission_code in self.session.execute(statement):
            assignments.setdefault(role_code, [])
            if permission_code is not None:
                assignments[role_code].append(permission_code)
        return {role: tuple(codes) for role, codes in assignments.items()}


def _user_record(row: AppUser) -> UserRecord:
    return UserRecord(
        id=row.id,
        username=row.username,
        password_hash=row.password_hash,
        is_active=row.is_active,
        failed_attempts=row.failed_attempts,
        locked_until=row.locked_until,
        password_changed_at=row.password_changed_at,
        must_change_password=row.must_change_password,
        mfa_secret_enc=row.mfa_secret_enc,
    )


def _session_record(row: UserSession) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        issued_at=row.issued_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        absolute_expires_at=row.absolute_expires_at,
        ip_hash=row.ip_hash,
        user_agent_hash=row.user_agent_hash,
        revoked_at=row.revoked_at,
    )


def _updated_at(user: UserRecord) -> datetime:
    return user.password_changed_at or user.locked_until or datetime.now(UTC)


def _request_id() -> str:
    return get_request_id() or new_request_id()


__all__ = ["SqlAlchemyIdentityStore"]
