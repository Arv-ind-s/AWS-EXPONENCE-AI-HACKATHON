"""Integration coverage for T-113's identity administration surface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict, ValidationError
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    ApiKey,
    AppUser,
    Role,
    UserRole,
    UserSession,
)
from covenant_radar.security.passwords import Argon2Parameters, PasswordPolicy, PasswordService
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.admin_users import AdminUsersService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


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


def _passwords() -> PasswordService:
    return PasswordService(
        Argon2Parameters(time_cost=1, memory_cost=1024, parallelism=1),
        policy=PasswordPolicy(min_length=12),
    )


def _user(username: str, *, active: bool = True) -> AppUser:
    return AppUser(
        id=uuid4(),
        username=username,
        email=f"{username}@example.com",
        full_name=username.title(),
        password_hash=None,
        auth_source="local",
        external_subject=None,
        is_active=active,
        mfa_secret_enc=None,
        failed_attempts=0,
        locked_until=None,
        password_changed_at=None,
        must_change_password=False,
        locale="en",
        theme="light",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-{uuid4().hex[:20]}",
        version=1,
    )


@pytest.fixture
def fixture():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    audit = _Audit()
    admin_role = Role(
        id=uuid4(),
        code="administrator",
        name="Administrator",
        is_system=True,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-admin-role",
        version=1,
    )
    analyst_role = Role(
        id=uuid4(),
        code="credit",
        name="Credit",
        is_system=True,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-credit-role",
        version=1,
    )
    admin = _user("admin")
    checker = _user("checker")
    target = _user("target")
    session.add_all([admin_role, analyst_role, admin, checker, target])
    session.flush()
    for user in (admin, checker):
        session.add(
            UserRole(
                id=uuid4(),
                user_id=user.id,
                role_id=admin_role.id,
                granted_by_id=admin.id,
                granted_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                created_by_id=admin.id,
                updated_by_id=admin.id,
                request_id="rq-role-grant",
            )
        )
    session.add(
        UserRole(
            id=uuid4(),
            user_id=target.id,
            role_id=analyst_role.id,
            granted_by_id=admin.id,
            granted_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            created_by_id=admin.id,
            updated_by_id=admin.id,
            request_id="rq-target-role",
        )
    )
    session.flush()
    service = AdminUsersService(
        session,
        audit=audit,
        passwords=_passwords(),
        clock=FixedClock(_NOW),
        request_id="rq-t113-test",
    )
    yield session, service, audit, admin, checker, target, admin_role, analyst_role
    session.close()
    engine.dispose()


def _principal(user: AppUser) -> Principal:
    return Principal.user(user.id, (Permission.MANAGE_USERS,))


def _session(user: AppUser) -> UserSession:
    return UserSession(
        id=uuid4(),
        user_id=user.id,
        token_hash=uuid4().hex,
        issued_at=_NOW,
        last_seen_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        absolute_expires_at=_NOW + timedelta(hours=12),
        revoked_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-session",
    )


def test_deactivation_revokes_sessions_and_keys(fixture) -> None:
    session, service, _audit, admin, _checker, target, _admin_role, _analyst_role = fixture
    user_session = _session(target)
    api_key = ApiKey(
        id=uuid4(),
        name="target-integration",
        key_hash=uuid4().hex,
        prefix="crk_test",
        scopes=["VIEW_QUEUE"],
        portfolio_scope=None,
        rate_limit_per_min=10,
        expires_at=None,
        last_used_at=None,
        revoked_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        created_by_id=target.id,
        updated_by_id=target.id,
        request_id="rq-key",
        version=1,
    )
    session.add_all([user_session, api_key])
    session.flush()

    service.deactivate_user(_principal(admin), target.id, reason="Leaver access removal")

    assert session.get(UserSession, user_session.id).revoked_at is not None
    assert session.get(ApiKey, api_key.id).revoked_at is not None
    assert not session.get(AppUser, target.id).is_active


def test_role_change_revokes_sessions(fixture) -> None:
    session, service, _audit, admin, _checker, target, admin_role, _analyst_role = fixture
    user_session = _session(target)
    session.add(user_session)
    session.flush()

    service.assign_roles(
        _principal(admin),
        target.id,
        ("administrator",),
        reason="Move to platform administration",
    )

    assert session.get(UserSession, user_session.id).revoked_at is not None
    assert service.get_user(_principal(admin), target.id).role_codes == ("administrator",)
    assert (
        session.scalar(
            select(UserRole).where(
                UserRole.user_id == target.id,
                UserRole.role_id == admin_role.id,
            )
        )
        is not None
    )


def test_last_administrator_protected(fixture) -> None:
    session_db, service, _audit, admin, checker, _target, admin_role, _analyst_role = fixture
    checker_role = session_db.scalar(
        select(UserRole).where(
            UserRole.user_id == checker.id,
            UserRole.role_id == admin_role.id,
        )
    )
    assert checker_role is not None
    session_db.delete(checker_role)
    session_db.flush()

    with pytest.raises(Conflict, match="last active administrator"):
        service.deactivate_user(_principal(admin), admin.id, reason="Attempted lockout")


def test_every_change_audited_with_before_and_after(fixture) -> None:
    _session, service, audit, admin, _checker, target, _admin_role, _analyst_role = fixture

    service.deactivate_user(_principal(admin), target.id, reason="Account closed")

    event = next(item for item in audit.events if item[0] == "admin_user_deactivated")
    assert event[2]["before"] is not None
    assert event[2]["after"] is not None
    assert event[2]["before"] != event[2]["after"]


def test_sso_to_local_requires_a_new_password(fixture) -> None:
    _session, service, _audit, admin, _checker, target, _admin_role, _analyst_role = fixture

    service.configure_sso_mapping(
        _principal(admin),
        target.id,
        auth_source="oidc",
        external_subject="bank|target",
        reason="Move identity to bank SSO",
    )

    with pytest.raises(ValidationError, match="new password"):
        service.configure_sso_mapping(
            _principal(admin),
            target.id,
            auth_source="local",
            external_subject=None,
            reason="Restore local sign-in",
        )

    restored = service.configure_sso_mapping(
        _principal(admin),
        target.id,
        auth_source="local",
        external_subject=None,
        password="A-secure-password-7",
        reason="Restore local sign-in",
    )
    assert restored.auth_source == "local"
    assert restored.must_change_password


def test_admin_screen_renders_with_the_production_template_environment(fixture) -> None:
    _session, service, _audit, admin, _checker, target, _admin_role, _analyst_role = fixture
    app = create_app()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/admin/users/{target.id}",
            "raw_path": f"/admin/users/{target.id}".encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    selected = service.get_user(_principal(admin), target.id)
    rendered = app.state.template_env.get_template(
        "screens/admin/users/index.html"
    ).render(
        request=request,
        principal=_principal(admin),
        users=service.list_users(_principal(admin)),
        roles=service.list_roles(_principal(admin)),
        portfolios=service.list_portfolios(_principal(admin)),
        pending=service.pending_role_assignments(_principal(admin)),
        selected=selected,
        sessions=service.list_sessions(_principal(admin), target.id),
        current_session_id=None,
        form={},
        error="",
        locale="en",
        theme="light",
        text_direction="ltr",
        csrf_token="",
    )

    assert "Users &amp; access" in rendered
    assert target.full_name in rendered
    assert "Save portfolio reach" in rendered
