"""Offline integration coverage for T-013 local authentication use cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import ValidationError
from covenant_radar.security.mfa import MfaSettings, TOTPService
from covenant_radar.security.passwords import Argon2Parameters, PasswordPolicy, PasswordService
from covenant_radar.security.sessions import (
    InMemorySessionStore,
    SessionManager,
    SessionSettings,
)
from covenant_radar.services.auth import (
    GENERIC_AUTHENTICATION_MESSAGE,
    AuthenticationSettings,
    AuthService,
    AuthStatus,
    UserRecord,
)
from covenant_radar.web.routes.auth import create_auth_router, sign_in_redirect_url

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_USER_ID = UUID("00000000-0000-7000-8000-000000000001")
_STRONG_PASSWORD = "Correct-Horse-123!"
_NEW_PASSWORD = "New-Correct-Horse-456!"


class _Users:
    def __init__(self, user: UserRecord) -> None:
        self.users = {user.id: user}
        self.by_username = {user.username: user.id}

    def find_by_username(self, username: str) -> UserRecord | None:
        user_id = self.by_username.get(username)
        return self.users.get(user_id) if user_id is not None else None

    def get(self, user_id: UUID) -> UserRecord | None:
        return self.users.get(user_id)

    def save(self, user: UserRecord) -> None:
        self.users[user.id] = user
        self.by_username[user.username] = user.id


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, dict(payload), actor))
        return object()


def _password_service() -> PasswordService:
    return PasswordService(
        parameters=Argon2Parameters(time_cost=1, memory_cost=1024, parallelism=1),
        policy=PasswordPolicy(min_length=12),
    )


def _build(
    *,
    user: UserRecord | None = None,
    clock: FixedClock | None = None,
    session_settings: SessionSettings | None = None,
    auth_settings: AuthenticationSettings | None = None,
    mfa: TOTPService | None = None,
) -> tuple[AuthService, _Users, InMemorySessionStore, _Audit, FixedClock]:
    test_clock = clock or FixedClock(_NOW)
    password_service = _password_service()
    selected_user = user or UserRecord(
        id=_USER_ID,
        username="alice",
        password_hash=password_service.hash(_STRONG_PASSWORD),
    )
    users = _Users(selected_user)
    session_store = InMemorySessionStore()
    audit = _Audit()
    sessions = SessionManager(
        session_store,
        settings=session_settings or SessionSettings(secret=b"a" * 32, secure_cookie=False),
        clock=test_clock,
    )
    service = AuthService(
        users,
        sessions,
        passwords=password_service,
        mfa=mfa,
        settings=auth_settings,
        clock=test_clock,
        audit=audit,
        request_id="rq-auth-local-test",
    )
    return service, users, session_store, audit, test_clock


def test_wrong_password_generic_message() -> None:
    service, _users, _sessions, audit, _clock = _build()

    result = service.sign_in("alice", "wrong-password")

    assert result.status is AuthStatus.FAILED
    assert result.message == GENERIC_AUTHENTICATION_MESSAGE
    assert audit.events[-1][2]["reason"] == "credential_rejected"


def test_locked_account_same_message() -> None:
    service, _users, _sessions, _audit, _clock = _build(
        auth_settings=AuthenticationSettings(lockout_threshold=1)
    )

    first = service.sign_in("alice", "wrong-password")
    second = service.sign_in("alice", _STRONG_PASSWORD)

    assert first.message == GENERIC_AUTHENTICATION_MESSAGE
    assert second.message == GENERIC_AUTHENTICATION_MESSAGE
    assert first.message == second.message


def test_password_change_revokes_other_sessions() -> None:
    service, _users, sessions, _audit, _clock = _build()
    first = service.sign_in("alice", _STRONG_PASSWORD)
    second = service.sign_in("alice", _STRONG_PASSWORD)
    assert first.session is not None and second.session is not None

    changed = service.change_password(first.session.cookie, _NEW_PASSWORD, _NEW_PASSWORD)

    assert changed.authenticated
    assert service.validate_session(second.session.cookie) is None
    assert len([record for record in sessions.records() if record.revoked_at is not None]) == 2


def test_role_change_revokes_sessions() -> None:
    service, _users, _sessions, _audit, _clock = _build()
    first = service.sign_in("alice", _STRONG_PASSWORD)
    second = service.sign_in("alice", _STRONG_PASSWORD)
    assert first.session is not None and second.session is not None

    assert service.revoke_sessions_for_role_change(_USER_ID, actor=UUID(int=2)) == 2
    assert service.validate_session(first.session.cookie) is None
    assert service.validate_session(second.session.cookie) is None


def test_session_idle_and_absolute_expiry() -> None:
    clock = FixedClock(_NOW)
    settings = SessionSettings(
        secret=b"b" * 32,
        idle_timeout=timedelta(seconds=10),
        absolute_timeout=timedelta(seconds=30),
        secure_cookie=False,
    )
    service, _users, _sessions, _audit, _clock = _build(clock=clock, session_settings=settings)
    result = service.sign_in("alice", _STRONG_PASSWORD)
    assert result.session is not None

    clock.advance(timedelta(seconds=5))
    assert service.refresh_session(result.session.cookie) is not None
    clock.advance(timedelta(seconds=9))
    assert service.validate_session(result.session.cookie) is not None
    clock.advance(timedelta(seconds=20))
    assert service.validate_session(result.session.cookie) is None


def test_expired_session_preserves_destination() -> None:
    redirect = sign_in_redirect_url(
        "/cases/C-000123?tab=evidence",
        form_data={"username": "alice", "password": "do-not-preserve"},
    )

    assert "next=%2Fcases%2FC-000123%3Ftab%3Devidence" in redirect
    assert "username=alice" in redirect
    assert "do-not-preserve" not in redirect
    assert "password" not in redirect


def test_mfa_enrolment_forced_when_enabled() -> None:
    clock = FixedClock(_NOW)
    mfa = TOTPService(
        b"c" * 32,
        settings=MfaSettings(enabled=True),
        clock=clock,
    )
    service, users, _sessions, _audit, _clock = _build(
        clock=clock,
        mfa=mfa,
        auth_settings=AuthenticationSettings(mfa_required=True),
    )

    primary = service.sign_in("alice", _STRONG_PASSWORD)
    assert primary.status is AuthStatus.MFA_ENROLLMENT_REQUIRED
    assert primary.challenge_cookie is not None
    enrollment = service.begin_mfa_enrollment(primary.challenge_cookie)
    code = mfa.code_for_secret(enrollment.enrollment.secret, _NOW)

    completed = service.complete_mfa_enrollment(enrollment.challenge_cookie, code)

    assert completed.authenticated
    assert users.get(_USER_ID).mfa_secret_enc is not None


def test_first_sign_in_forced_password_change() -> None:
    password_service = _password_service()
    user = UserRecord(
        id=_USER_ID,
        username="alice",
        password_hash=password_service.hash(_STRONG_PASSWORD),
        must_change_password=True,
    )
    service, _users, _sessions, _audit, _clock = _build(user=user)

    result = service.sign_in("alice", _STRONG_PASSWORD)

    assert result.status is AuthStatus.PASSWORD_CHANGE_REQUIRED
    assert result.challenge_cookie is not None
    with pytest.raises(ValidationError):
        service.change_password(result.challenge_cookie, "weak", "weak")


def test_every_outcome_audited() -> None:
    service, _users, _sessions, audit, _clock = _build(
        auth_settings=AuthenticationSettings(lockout_threshold=2)
    )
    service.sign_in("missing", "wrong-password")
    service.sign_in("alice", "wrong-password")
    success = service.sign_in("alice", _STRONG_PASSWORD)
    assert success.session is not None
    service.sign_out(success.session.cookie)
    service.sign_out("tampered")

    reasons = [payload.get("reason") for _event, _subject, payload, _actor in audit.events]
    assert "credential_rejected" in reasons
    assert "password_verified" in reasons
    assert "authenticated" in reasons
    assert "logout" in reasons
    assert "logout_session_invalid" in reasons


def test_sign_in_route_sets_signed_http_only_cookie() -> None:
    service, _users, _sessions, _audit, _clock = _build()
    app = FastAPI()
    app.include_router(create_auth_router(service))

    response = TestClient(app).post(
        "/sign-in",
        data={"username": "alice", "password": _STRONG_PASSWORD, "next": "/queue"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/queue"
    set_cookie = response.headers["set-cookie"]
    assert "covenant_radar_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=1800" in set_cookie
