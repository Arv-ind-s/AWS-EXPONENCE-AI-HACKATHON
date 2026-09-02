"""Use cases for the administration console's identity surface.

This module is deliberately the only place where the browser-facing admin
routes can mutate identity data.  The service owns authorization, optimistic
concurrency, the last-administrator invariant, immediate credential
revocation, saved-view narrowing, maker-checker transitions, and audit
emission.  Routes only translate bounded form data into these use cases.

The existing identity schema is intentionally sufficient for this surface:
SSO mapping is represented by ``app_user.auth_source`` and
``app_user.external_subject``; API keys are disabled through their existing
``revoked_at`` column.  No plaintext password, session token, or API key is
ever accepted by the service after the operation returns.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, scoped_session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.identity import (
    ApiKey,
    AppUser,
    Permission,
    Role,
    RolePermission,
    UserPortfolioScope,
    UserRole,
    UserSession,
)
from covenant_radar.db.models.maker_checker import MakerCheckerRequest
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.views import SavedQueueView
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.triage.views import QueueFilters, SavedView
from covenant_radar.security.maker_checker import MakerCheckerState, validate_reason
from covenant_radar.security.passwords import PasswordService
from covenant_radar.security.permissions import Permission as PermissionCode
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize


class AuditWriter(Protocol):
    """The append-only audit boundary used by every admin mutation."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an event in the caller's current transaction."""


class PermissionCache(Protocol):
    """The small invalidation port used by the request principal resolver."""

    def invalidate_user(self, user_id: UUID) -> object:
        """Forget cached permissions for one user."""


ADMIN_PERMISSION = PermissionCode.MANAGE_USERS
ROLE_ASSIGNMENT_OPERATION = "admin.role_assignment"
_ADMINISTRATOR_ROLE_CODES = frozenset({"administrator", "admin"})
_AUTH_SOURCES = frozenset({"local", "oidc", "saml"})
_LOCALES = frozenset({"en", "hi"})
_THEMES = frozenset({"light", "dark"})
_ROLE_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,49}$")
_USERNAME_MAX_LENGTH = 64
_EMAIL_MAX_LENGTH = 254
_FULL_NAME_MAX_LENGTH = 200
_EXTERNAL_SUBJECT_MAX_LENGTH = 255
_MAX_SCOPES = 500


@dataclass(frozen=True, slots=True)
class RoleView:
    """A non-sensitive role row for the console."""

    id: UUID
    code: str
    name: str
    is_system: bool
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortfolioScopeView:
    """A portfolio grant with the hierarchy affordance made explicit."""

    portfolio_id: UUID
    code: str
    name: str
    path: str
    include_descendants: bool


@dataclass(frozen=True, slots=True)
class AdminUserView:
    """The safe user read model used by both the service and the template."""

    id: UUID
    username: str
    email: str
    full_name: str
    auth_source: str
    external_subject: str | None
    is_active: bool
    must_change_password: bool
    locale: str
    theme: str
    version: int
    created_at: datetime
    roles: tuple[RoleView, ...]
    scopes: tuple[PortfolioScopeView, ...]
    active_session_count: int
    active_api_key_count: int

    @property
    def role_codes(self) -> tuple[str, ...]:
        """Return stable role codes for form and audit comparisons."""

        return tuple(role.code for role in self.roles)


@dataclass(frozen=True, slots=True)
class SessionView:
    """A session view that intentionally contains no token or network hash."""

    id: UUID
    user_id: UUID
    issued_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None

    @property
    def is_live(self) -> bool:
        """Whether the row has not been explicitly revoked."""

        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class PendingRoleAssignment:
    """The review-safe projection of a self-escalation request."""

    id: UUID
    user_id: UUID
    maker_id: UUID
    role_codes: tuple[str, ...]
    created_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class RoleAssignmentResult:
    """Result of an immediate or maker-checker role update."""

    user_id: UUID
    applied: bool
    request_id: UUID | None = None
    user: AdminUserView | None = None

    @property
    def pending(self) -> bool:
        """Whether a distinct administrator must still decide the change."""

        return not self.applied


class AdminUsersService:
    """Coordinate users, roles, scopes, sessions, and SSO mappings."""

    def __init__(
        self,
        session: Session | scoped_session[Session],
        *,
        audit: AuditWriter,
        passwords: PasswordService | None = None,
        roles: PermissionCache | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("AdminUsersService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("AdminUsersService requires an append-only audit writer.")
        if roles is not None and not callable(getattr(roles, "invalidate_user", None)):
            raise TypeError("AdminUsersService roles adapter must support invalidate_user().")
        self.session = session
        self.audit = audit
        self.passwords = passwords or PasswordService()
        self.roles = roles
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 40:
            raise ValueError("AdminUsersService request_id must be between 1 and 40 characters.")

    # ---- read models -------------------------------------------------

    def list_users(
        self,
        principal: Principal,
        *,
        include_inactive: bool = True,
    ) -> tuple[AdminUserView, ...]:
        """List users in deterministic order without exposing credentials."""

        self._require_admin(principal)
        if not isinstance(include_inactive, bool):
            raise ValidationError("include_inactive must be boolean.", field="include_inactive")
        statement = select(AppUser).order_by(
            AppUser.is_active.desc(), AppUser.full_name, AppUser.id
        )
        if not include_inactive:
            statement = statement.where(AppUser.is_active.is_(True))
        return tuple(self._user_view(row) for row in self.session.scalars(statement).all())

    def get_user(self, principal: Principal, user_id: UUID | str) -> AdminUserView:
        """Return one user or a non-enumerating not-found domain error."""

        self._require_admin(principal)
        row = self._user(user_id)
        return self._user_view(row)

    def list_roles(self, principal: Principal) -> tuple[RoleView, ...]:
        """Return the role catalogue, including each role's permission codes."""

        self._require_admin(principal)
        role_rows = self.session.scalars(select(Role).order_by(Role.name, Role.code)).all()
        permissions = self._permissions_by_role_id(tuple(row.id for row in role_rows))
        return tuple(
            RoleView(
                id=row.id,
                code=row.code,
                name=row.name,
                is_system=row.is_system,
                permissions=permissions.get(row.id, ()),
            )
            for row in role_rows
        )

    def list_portfolios(self, principal: Principal) -> tuple[Portfolio, ...]:
        """Return the hierarchy used to build scope checkboxes."""

        self._require_admin(principal)
        return tuple(
            self.session.scalars(select(Portfolio).order_by(Portfolio.path, Portfolio.code)).all()
        )

    def list_sessions(
        self,
        principal: Principal,
        user_id: UUID | str,
        *,
        include_revoked: bool = True,
    ) -> tuple[SessionView, ...]:
        """List session metadata, never the bearer token or its digest."""

        self._require_admin(principal)
        parsed_user_id = _uuid(user_id, "user_id")
        if self.session.get(AppUser, parsed_user_id) is None:
            raise NotFound(f"User {parsed_user_id} was not found.")
        statement = select(UserSession).where(UserSession.user_id == parsed_user_id)
        if not include_revoked:
            statement = statement.where(UserSession.revoked_at.is_(None))
        statement = statement.order_by(UserSession.last_seen_at.desc(), UserSession.id)
        return tuple(_session_view(row) for row in self.session.scalars(statement).all())

    def pending_role_assignments(
        self,
        principal: Principal,
    ) -> tuple[PendingRoleAssignment, ...]:
        """Return pending self-escalation requests visible to an admin checker."""

        self._require_admin(principal)
        rows = self.session.scalars(
            select(MakerCheckerRequest)
            .where(
                MakerCheckerRequest.operation == ROLE_ASSIGNMENT_OPERATION,
                MakerCheckerRequest.state == MakerCheckerState.PENDING.value,
            )
            .order_by(MakerCheckerRequest.created_at, MakerCheckerRequest.id)
        ).all()
        return tuple(_pending_request(row) for row in rows)

    # ---- user lifecycle ----------------------------------------------

    def create_user(
        self,
        principal: Principal,
        *,
        username: str,
        email: str,
        full_name: str,
        password: str | None = None,
        role_codes: Sequence[str] = (),
        portfolio_scopes: Sequence[object] | Mapping[object, object] = (),
        auth_source: str = "local",
        external_subject: str | None = None,
        locale: str = "en",
        theme: str = "light",
    ) -> AdminUserView:
        """Create one user with an Argon2id password and explicit scope."""

        self._require_admin(principal)
        clean_username = _username(username)
        clean_email = _text(email, "email", _EMAIL_MAX_LENGTH)
        clean_full_name = _text(full_name, "full_name", _FULL_NAME_MAX_LENGTH)
        clean_source, clean_subject = _sso_mapping(auth_source, external_subject)
        clean_locale = _choice(locale, "locale", _LOCALES)
        clean_theme = _choice(theme, "theme", _THEMES)
        normalized_roles = self._role_ids(role_codes)
        normalized_scopes = self._scope_inputs(portfolio_scopes)
        password_hash: str | None = None
        if clean_source == "local":
            if not isinstance(password, str) or not password:
                raise ValidationError(
                    "A local user requires an initial password.", field="password"
                )
            password_hash = self.passwords.hash(password)
        elif password is not None:
            raise ValidationError(
                "SSO-only users must not carry a local password.", field="password"
            )

        now = self._now()
        row = AppUser(
            id=new_id(),
            username=clean_username,
            email=clean_email,
            full_name=clean_full_name,
            password_hash=password_hash,
            auth_source=clean_source,
            external_subject=clean_subject,
            is_active=True,
            mfa_secret_enc=None,
            failed_attempts=0,
            locked_until=None,
            password_changed_at=None,
            must_change_password=clean_source == "local",
            locale=clean_locale,
            theme=clean_theme,
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=self._write_request_id(),
            version=1,
        )
        self.session.add(row)
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as error:
            raise Conflict("The username or active email is already in use.") from error
        self._add_roles_and_scopes(row, normalized_roles, normalized_scopes, principal.id, now)
        after = self._user_snapshot(row)
        self._audit(
            AuditEventType.ADMIN_USER_CREATED,
            row,
            {"before": None, "after": after},
            principal,
        )
        return self._user_view(row)

    def deactivate_user(
        self,
        principal: Principal,
        user_id: UUID | str,
        *,
        reason: str,
    ) -> AdminUserView:
        """Deactivate a user and revoke all sessions and owned API keys."""

        self._require_admin(principal)
        target = self._user(user_id)
        clean_reason = _reason(reason)
        before = self._user_snapshot(target)
        if not target.is_active:
            raise Conflict(f"User {target.username!r} is already inactive.")
        if self._is_administrator(target.id) and self._active_administrator_count() <= 1:
            raise Conflict("The last active administrator cannot be deactivated.")
        now = self._now()
        target.is_active = False
        self._touch_user(target, principal.id, now)
        revoked_sessions, disabled_keys = self._revoke_credentials(target.id, principal.id, now)
        after = self._user_snapshot(target)
        self._audit(
            AuditEventType.ADMIN_USER_DEACTIVATED,
            target,
            {
                "before": before,
                "after": after,
                "reason": clean_reason,
                "sessions_revoked": revoked_sessions,
                "api_keys_disabled": disabled_keys,
            },
            principal,
        )
        self._invalidate_permissions(target.id)
        return self._user_view(target)

    def reactivate_user(
        self,
        principal: Principal,
        user_id: UUID | str,
        *,
        reason: str,
    ) -> AdminUserView:
        """Reactivate a prior user without silently restoring old sessions."""

        self._require_admin(principal)
        target = self._user(user_id)
        clean_reason = _reason(reason)
        if target.is_active:
            raise Conflict(f"User {target.username!r} is already active.")
        before = self._user_snapshot(target)
        now = self._now()
        target.is_active = True
        self._touch_user(target, principal.id, now)
        after = self._user_snapshot(target)
        self._audit(
            AuditEventType.ADMIN_USER_REACTIVATED,
            target,
            {"before": before, "after": after, "reason": clean_reason},
            principal,
        )
        return self._user_view(target)

    def reset_password(
        self,
        principal: Principal,
        user_id: UUID | str,
        *,
        password: str,
        reason: str,
    ) -> AdminUserView:
        """Replace a local password, force a change, and revoke sessions."""

        self._require_admin(principal)
        target = self._user(user_id)
        clean_reason = _reason(reason)
        if target.auth_source != "local":
            raise Conflict("An SSO-only user does not have a local password to reset.")
        if not isinstance(password, str) or not password:
            raise ValidationError("A replacement password is required.", field="password")
        password_hash = self.passwords.hash(password)
        before = self._user_snapshot(target)
        now = self._now()
        target.password_hash = password_hash
        target.password_changed_at = now
        target.must_change_password = True
        target.failed_attempts = 0
        target.locked_until = None
        self._touch_user(target, principal.id, now)
        revoked_sessions, _disabled_keys = self._revoke_credentials(
            target.id, principal.id, now, disable_keys=False
        )
        after = self._user_snapshot(target)
        self._audit(
            AuditEventType.ADMIN_USER_PASSWORD_RESET,
            target,
            {
                "before": before,
                "after": after,
                "reason": clean_reason,
                "sessions_revoked": revoked_sessions,
            },
            principal,
        )
        return self._user_view(target)

    def configure_sso_mapping(
        self,
        principal: Principal,
        user_id: UUID | str,
        *,
        auth_source: str,
        external_subject: str | None,
        password: str | None = None,
        reason: str,
    ) -> AdminUserView:
        """Set or clear the immutable-provider identity mapping."""

        self._require_admin(principal)
        target = self._user(user_id)
        clean_reason = _reason(reason)
        clean_source, clean_subject = _sso_mapping(auth_source, external_subject)
        if clean_source != "local" and password is not None:
            raise ValidationError(
                "SSO-only users must not carry a local password.", field="password"
            )
        if clean_source == "local" and target.auth_source != "local":
            if not isinstance(password, str) or not password:
                raise ValidationError(
                    "Switching to a local account requires a new password.", field="password"
                )
        if target.auth_source == clean_source and target.external_subject == clean_subject:
            raise Conflict("The SSO mapping is unchanged.")
        before = self._user_snapshot(target)
        now = self._now()
        was_local = target.auth_source == "local"
        target.auth_source = clean_source
        target.external_subject = clean_subject
        if clean_source != "local":
            target.password_hash = None
            target.must_change_password = False
        elif not was_local:
            target.password_hash = self.passwords.hash(cast(str, password))
            target.password_changed_at = now
            target.must_change_password = True
        self._touch_user(target, principal.id, now)
        revoked_sessions, _disabled_keys = self._revoke_credentials(
            target.id, principal.id, now, disable_keys=False
        )
        after = self._user_snapshot(target)
        self._audit(
            AuditEventType.ADMIN_USER_SSO_MAPPING_CHANGED,
            target,
            {
                "before": before,
                "after": after,
                "reason": clean_reason,
                "sessions_revoked": revoked_sessions,
            },
            principal,
        )
        return self._user_view(target)

    # ---- roles and scopes ---------------------------------------------

    def assign_roles(
        self,
        principal: Principal,
        user_id: UUID | str,
        role_codes: Sequence[str],
        *,
        reason: str,
    ) -> RoleAssignmentResult:
        """Replace a user's roles, routing self-escalation to maker-checker."""

        self._require_admin(principal)
        target = self._user(user_id)
        clean_reason = _reason(reason)
        desired_codes = self._normalise_role_codes(role_codes)
        self._role_ids(desired_codes)
        current_codes = self._role_codes(target.id)
        if tuple(sorted(current_codes)) == tuple(sorted(desired_codes)):
            raise Conflict("The requested role assignment is unchanged.")
        before = self._user_snapshot(target)
        added = set(desired_codes) - set(current_codes)
        if target.id == principal.id and not self._actor_holds_roles(principal.id, added):
            pending = self._create_pending_role_assignment(
                target,
                desired_codes,
                before=before,
                reason=clean_reason,
                principal=principal,
            )
            self._audit(
                AuditEventType.ADMIN_ROLE_ASSIGNMENT_PROPOSED,
                pending,
                {
                    "before": before,
                    "after": {"roles": list(desired_codes)},
                    "state": MakerCheckerState.PENDING.value,
                    "request_id": str(pending.id),
                    "reason": clean_reason,
                },
                principal,
            )
            return RoleAssignmentResult(target.id, applied=False, request_id=pending.id)

        now = self._now()
        self._apply_role_codes(target, desired_codes, principal.id, now)
        revoked_sessions, _disabled_keys = self._revoke_credentials(
            target.id, principal.id, now, disable_keys=False
        )
        after = self._user_snapshot(target)
        self._audit(
            AuditEventType.ADMIN_USER_ROLES_CHANGED,
            target,
            {
                "before": before,
                "after": after,
                "reason": clean_reason,
                "sessions_revoked": revoked_sessions,
            },
            principal,
        )
        self._invalidate_permissions(target.id)
        return RoleAssignmentResult(target.id, applied=True, user=self._user_view(target))

    def assign_role(
        self,
        principal: Principal,
        user_id: UUID | str,
        role_code: str,
        *,
        reason: str,
    ) -> RoleAssignmentResult:
        """Convenience form for callers adding/replacing one explicit role."""

        return self.assign_roles(principal, user_id, (role_code,), reason=reason)

    def decide_role_assignment(
        self,
        principal: Principal,
        request_id: UUID | str,
        *,
        approved: bool,
        reason: str | None = None,
    ) -> RoleAssignmentResult:
        """Approve or reject a pending role assignment as a distinct admin."""

        self._require_admin(principal)
        if not isinstance(approved, bool):
            raise ValidationError("approved must be boolean.", field="approved")
        request = self._maker_request(request_id, lock=True)
        if request.state != MakerCheckerState.PENDING.value:
            raise Conflict(f"Role assignment {request.id} is already {request.state}.")
        if request.maker_id == principal.id:
            raise Conflict("A role assignment requires a distinct administrator approver.")
        decision_reason = validate_reason(reason, required=not approved)
        payload = _role_payload(request.payload)
        target = self._user(payload.user_id)
        before = self._user_snapshot(target)
        expected_version = payload.expected_version
        if target.version != expected_version:
            raise Conflict(
                f"User {target.username!r} changed while the role assignment was pending; "
                "submit a new request."
            )
        now = self._now()
        if approved:
            self._role_ids(payload.role_codes)
            current_codes = self._role_codes(target.id)
            if tuple(sorted(current_codes)) != payload.before_role_codes:
                raise Conflict(
                    f"User {target.username!r} roles changed while the request was pending."
                )
            self._apply_role_codes(target, payload.role_codes, principal.id, now)
            revoked_sessions, _disabled_keys = self._revoke_credentials(
                target.id, principal.id, now, disable_keys=False
            )
            after = self._user_snapshot(target)
            event = AuditEventType.ADMIN_ROLE_ASSIGNMENT_APPROVED
            result = RoleAssignmentResult(target.id, applied=True, user=self._user_view(target))
            self._invalidate_permissions(target.id)
        else:
            revoked_sessions = 0
            after = before
            event = AuditEventType.ADMIN_ROLE_ASSIGNMENT_REJECTED
            result = RoleAssignmentResult(target.id, applied=False, request_id=request.id)

        request.checker_id = principal.id
        request.state = (
            MakerCheckerState.APPROVED.value
            if approved
            else MakerCheckerState.REJECTED.value
        )
        request.decided_at = now
        request.reason = decision_reason
        request.updated_at = now
        request.updated_by_id = principal.id
        request.request_id = self._write_request_id()
        request.version += 1
        self.session.flush()
        self._audit(
            event,
            target,
            {
                "before": before,
                "after": after,
                "request_id": str(request.id),
                "maker_id": str(request.maker_id),
                "checker_id": str(principal.id),
                "state": request.state,
                "reason": decision_reason,
                "sessions_revoked": revoked_sessions,
            },
            principal,
        )
        generic_event = (
            AuditEventType.MAKER_CHECKER_APPROVED
            if approved
            else AuditEventType.MAKER_CHECKER_REJECTED
        )
        self._audit(
            generic_event,
            request,
            {
                "request_id": str(request.id),
                "operation": request.operation,
                "subject_type": request.subject_type,
                "subject_id": str(request.subject_id),
                "maker_id": str(request.maker_id),
                "checker_id": str(principal.id),
                "state": request.state,
                "approved": approved,
                "reason": decision_reason,
            },
            principal,
        )
        return result

    def approve_role_assignment(
        self,
        principal: Principal,
        request_id: UUID | str,
    ) -> RoleAssignmentResult:
        """Approve one pending role assignment."""

        return self.decide_role_assignment(principal, request_id, approved=True)

    def reject_role_assignment(
        self,
        principal: Principal,
        request_id: UUID | str,
        *,
        reason: str,
    ) -> RoleAssignmentResult:
        """Reject one pending role assignment with a recorded reason."""

        return self.decide_role_assignment(
            principal,
            request_id,
            approved=False,
            reason=reason,
        )

    def set_portfolio_scope(
        self,
        principal: Principal,
        user_id: UUID | str,
        portfolio_scopes: Sequence[object] | Mapping[object, object],
        *,
        reason: str,
    ) -> AdminUserView:
        """Replace a user's explicit portfolio grants; empty means no access."""

        self._require_admin(principal)
        target = self._user(user_id)
        clean_reason = _reason(reason)
        normalized = self._scope_inputs(portfolio_scopes)
        before = self._user_snapshot(target)
        current = {
            (row.portfolio_id, bool(row.include_descendants))
            for row in self.session.scalars(
                select(UserPortfolioScope).where(UserPortfolioScope.user_id == target.id)
            ).all()
        }
        desired = set(normalized)
        if current == desired:
            raise Conflict("The requested portfolio scope is unchanged.")
        now = self._now()
        self.session.execute(
            delete(UserPortfolioScope).where(UserPortfolioScope.user_id == target.id)
        )
        for portfolio_id, include_descendants in normalized:
            self.session.add(
                UserPortfolioScope(
                    id=new_id(),
                    user_id=target.id,
                    portfolio_id=portfolio_id,
                    include_descendants=include_descendants,
                    created_at=now,
                    updated_at=now,
                    created_by_id=principal.id,
                    updated_by_id=principal.id,
                    request_id=self._write_request_id(),
                    version=1,
                )
            )
        self._touch_user(target, principal.id, now)
        narrowed_views = self._narrow_saved_views(target.id, normalized, principal.id, now)
        after = self._user_snapshot(target)
        self._audit(
            AuditEventType.ADMIN_USER_SCOPE_CHANGED,
            target,
            {
                "before": before,
                "after": after,
                "reason": clean_reason,
                "saved_views_narrowed": [str(value) for value in narrowed_views],
            },
            principal,
        )
        return self._user_view(target)

    def update_scope(
        self,
        principal: Principal,
        user_id: UUID | str,
        portfolio_scopes: Sequence[object] | Mapping[object, object],
        *,
        reason: str,
    ) -> AdminUserView:
        """Compatibility-facing spelling for :meth:`set_portfolio_scope`."""

        return self.set_portfolio_scope(principal, user_id, portfolio_scopes, reason=reason)

    # ---- sessions ----------------------------------------------------

    def revoke_session(
        self,
        principal: Principal,
        user_id: UUID | str,
        session_id: UUID | str,
        *,
        reason: str,
    ) -> SessionView:
        """Revoke one session immediately and audit its before/after state."""

        self._require_admin(principal)
        target_user_id = _uuid(user_id, "user_id")
        parsed_session_id = _uuid(session_id, "session_id")
        clean_reason = _reason(reason)
        row = self.session.scalar(
            select(UserSession).where(
                UserSession.id == parsed_session_id,
                UserSession.user_id == target_user_id,
            )
        )
        if row is None:
            raise NotFound(f"Session {parsed_session_id} was not found for this user.")
        if row.revoked_at is not None:
            raise Conflict("The session is already revoked.")
        before = _session_snapshot(row)
        now = self._now()
        row.revoked_at = now
        row.updated_at = now
        row.updated_by_id = principal.id
        row.request_id = self._write_request_id()
        self.session.flush()
        after = _session_snapshot(row)
        self._audit(
            AuditEventType.ADMIN_USER_SESSION_REVOKED,
            row,
            {"before": before, "after": after, "reason": clean_reason},
            principal,
        )
        return _session_view(row)

    def revoke_user_session(
        self,
        principal: Principal,
        user_id: UUID | str,
        session_id: UUID | str,
        *,
        reason: str,
    ) -> SessionView:
        """Compatibility-facing spelling for :meth:`revoke_session`."""

        return self.revoke_session(principal, user_id, session_id, reason=reason)

    def deactivate(
        self,
        principal: Principal,
        user_id: UUID | str,
        *,
        reason: str,
    ) -> AdminUserView:
        """Compatibility-facing spelling for :meth:`deactivate_user`."""

        return self.deactivate_user(principal, user_id, reason=reason)

    def reset_user_password(
        self,
        principal: Principal,
        user_id: UUID | str,
        *,
        password: str,
        reason: str,
    ) -> AdminUserView:
        """Compatibility-facing spelling for :meth:`reset_password`."""

        return self.reset_password(principal, user_id, password=password, reason=reason)

    # ---- private mutation helpers -----------------------------------

    def _create_pending_role_assignment(
        self,
        target: AppUser,
        desired_codes: tuple[str, ...],
        *,
        before: Mapping[str, object],
        reason: str,
        principal: Principal,
    ) -> MakerCheckerRequest:
        now = self._now()
        request = MakerCheckerRequest(
            id=new_id(),
            subject_type="app_user",
            subject_id=target.id,
            operation=ROLE_ASSIGNMENT_OPERATION,
            payload={
                "user_id": str(target.id),
                "role_codes": list(desired_codes),
                "expected_version": target.version,
                "before": dict(before),
                "reason": reason,
            },
            maker_id=principal.id,
            checker_id=None,
            state=MakerCheckerState.PENDING.value,
            created_at=now,
            updated_at=now,
            decided_at=None,
            reason=reason,
            request_id=self._write_request_id(),
            version=1,
        )
        self.session.add(request)
        self.session.flush()
        return request

    def _apply_role_codes(
        self,
        target: AppUser,
        desired_codes: Sequence[str],
        actor_id: UUID,
        now: datetime,
    ) -> None:
        current_codes = set(self._role_codes(target.id))
        desired = set(desired_codes)
        if target.is_active and self._is_administrator(target.id):
            if (
                not desired.intersection(_ADMINISTRATOR_ROLE_CODES)
                and self._active_administrator_count() <= 1
            ):
                raise Conflict("The last active administrator must retain an administrator role.")
        role_rows = {row.code: row for row in self.session.scalars(select(Role)).all()}
        links = self.session.scalars(select(UserRole).where(UserRole.user_id == target.id)).all()
        for link in links:
            role = next((item for item in role_rows.values() if item.id == link.role_id), None)
            if role is not None and role.code not in desired:
                self.session.delete(link)
        for code in sorted(desired - current_codes):
            role = role_rows[code]
            self.session.add(
                UserRole(
                    id=new_id(),
                    user_id=target.id,
                    role_id=role.id,
                    granted_by_id=actor_id,
                    granted_at=now,
                    created_at=now,
                    updated_at=now,
                    created_by_id=actor_id,
                    updated_by_id=actor_id,
                    request_id=self._write_request_id(),
                )
            )
        self._touch_user(target, actor_id, now)
        self.session.flush()

    def _add_roles_and_scopes(
        self,
        target: AppUser,
        role_ids: Sequence[UUID],
        scopes: Sequence[tuple[UUID, bool]],
        actor_id: UUID,
        now: datetime,
    ) -> None:
        for role_id in role_ids:
            self.session.add(
                UserRole(
                    id=new_id(),
                    user_id=target.id,
                    role_id=role_id,
                    granted_by_id=actor_id,
                    granted_at=now,
                    created_at=now,
                    updated_at=now,
                    created_by_id=actor_id,
                    updated_by_id=actor_id,
                    request_id=self._write_request_id(),
                )
            )
        for portfolio_id, include_descendants in scopes:
            self.session.add(
                UserPortfolioScope(
                    id=new_id(),
                    user_id=target.id,
                    portfolio_id=portfolio_id,
                    include_descendants=include_descendants,
                    created_at=now,
                    updated_at=now,
                    created_by_id=actor_id,
                    updated_by_id=actor_id,
                    request_id=self._write_request_id(),
                    version=1,
                )
            )
        self.session.flush()

    def _revoke_credentials(
        self,
        user_id: UUID,
        actor_id: UUID,
        now: datetime,
        *,
        disable_keys: bool = True,
    ) -> tuple[int, int]:
        sessions = self.session.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(
                revoked_at=now,
                updated_at=now,
                updated_by_id=actor_id,
                request_id=self._write_request_id(),
            )
        )
        key_count = 0
        if disable_keys:
            keys = self.session.execute(
                update(ApiKey)
                .where(ApiKey.created_by_id == user_id, ApiKey.revoked_at.is_(None))
                .values(
                    revoked_at=now,
                    updated_at=now,
                    updated_by_id=actor_id,
                    request_id=self._write_request_id(),
                    version=ApiKey.version + 1,
                )
            )
            key_count = int(keys.rowcount or 0)
        return int(sessions.rowcount or 0), key_count

    def _narrow_saved_views(
        self,
        user_id: UUID,
        scopes: Sequence[tuple[UUID, bool]],
        actor_id: UUID,
        now: datetime,
    ) -> tuple[UUID, ...]:
        """Drop portfolio filters a user can no longer see from owned views."""

        exact_ids = {portfolio_id for portfolio_id, descendants in scopes if not descendants}
        descendant_ids = {portfolio_id for portfolio_id, descendants in scopes if descendants}
        scope_rows = self.session.scalars(select(Portfolio)).all()
        allowed_ids: set[UUID] = set()
        allowed_codes: set[str] = set()
        allowed_paths: set[str] = set()
        for portfolio in scope_rows:
            if portfolio.id in exact_ids or portfolio.id in descendant_ids or any(
                portfolio.path.startswith(parent.path)
                for parent in scope_rows
                if parent.id in descendant_ids
            ):
                allowed_ids.add(portfolio.id)
                allowed_codes.add(portfolio.code)
                allowed_paths.add(portfolio.path.rstrip("/"))

        narrowed: list[UUID] = []
        rows = self.session.scalars(
            select(SavedQueueView).where(SavedQueueView.owner_id == user_id)
        ).all()
        for row in rows:
            try:
                view = SavedView.from_json(row.filter_json)
            except (TypeError, ValueError):
                # A malformed legacy view is left untouched; the queue's own
                # validation remains the authority for malformed persisted data.
                continue
            portfolio_filter = view.filters.portfolio
            if portfolio_filter is None or self._portfolio_filter_allowed(
                portfolio_filter, allowed_ids, allowed_codes, allowed_paths
            ):
                continue
            filtered = QueueFilters(
                band=view.filters.band,
                portfolio=None,
                industry=view.filters.industry,
                assignee=view.filters.assignee,
                sma_band=view.filters.sma_band,
                case_state=view.filters.case_state,
            )
            row.filter_json = SavedView(name=view.name, filters=filtered).to_json()
            row.updated_at = now
            row.updated_by_id = actor_id
            row.request_id = self._write_request_id()
            row.version += 1
            narrowed.append(row.id)
        if narrowed:
            self.session.flush()
        return tuple(narrowed)

    @staticmethod
    def _portfolio_filter_allowed(
        value: UUID | str,
        allowed_ids: set[UUID],
        allowed_codes: set[str],
        allowed_paths: set[str],
    ) -> bool:
        if isinstance(value, UUID):
            return value in allowed_ids
        try:
            if UUID(value) in allowed_ids:
                return True
        except (ValueError, AttributeError):
            pass
        return value in allowed_codes or value.rstrip("/") in allowed_paths

    # ---- query and validation helpers --------------------------------

    def _user(self, value: UUID | str) -> AppUser:
        parsed = _uuid(value, "user_id")
        row = self.session.get(AppUser, parsed)
        if row is None:
            raise NotFound(f"User {parsed} was not found.")
        return row

    def _role_ids(self, values: Sequence[str]) -> tuple[UUID, ...]:
        codes = self._normalise_role_codes(values)
        rows = (
            tuple(self.session.scalars(select(Role).where(Role.code.in_(codes))).all())
            if codes
            else ()
        )
        by_code = {row.code: row.id for row in rows}
        missing = [code for code in codes if code not in by_code]
        if missing:
            raise ValidationError(f"Unknown role {missing[0]!r}.", field="role_codes")
        return tuple(by_code[code] for code in codes)

    @staticmethod
    def _normalise_role_codes(values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, str) or not isinstance(values, Sequence):
            raise ValidationError(
                "role_codes must be a sequence of role codes.", field="role_codes"
            )
        result: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValidationError("Role codes must be text.", field="role_codes")
            code = value.strip()
            if not code or _ROLE_CODE_PATTERN.fullmatch(code) is None:
                raise ValidationError(f"Invalid role code {value!r}.", field="role_codes")
            if code not in result:
                result.append(code)
        return tuple(result)

    def _scope_inputs(
        self,
        values: Sequence[object] | Mapping[object, object],
    ) -> tuple[tuple[UUID, bool], ...]:
        if isinstance(values, Mapping):
            entries: Iterable[object] = tuple(values.items())
        elif isinstance(values, Sequence) and not isinstance(values, str | bytes):
            entries = values
        else:
            raise ValidationError(
                "portfolio_scopes must be a sequence or mapping.", field="portfolio_scopes"
            )
        result: list[tuple[UUID, bool]] = []
        for entry in entries:
            reference: object
            include_descendants: object = True
            if isinstance(entry, tuple) and len(entry) == 2:
                reference, include_descendants = entry
            elif isinstance(entry, Mapping):
                reference = entry.get("portfolio_id", entry.get("id", entry.get("code")))
                include_descendants = entry.get("include_descendants", True)
            else:
                reference = entry
            if not isinstance(include_descendants, bool):
                if isinstance(include_descendants, str):
                    include_descendants = include_descendants.lower() in {"1", "true", "yes", "on"}
                else:
                    raise ValidationError(
                        "include_descendants must be boolean.", field="include_descendants"
                    )
            portfolio = self._portfolio(reference)
            candidate = (portfolio.id, include_descendants)
            if candidate not in result:
                if any(existing[0] == candidate[0] for existing in result):
                    raise ValidationError(
                        f"Portfolio {portfolio.code!r} has conflicting scope entries.",
                        field="portfolio_scopes",
                    )
                result.append(candidate)
            if len(result) > _MAX_SCOPES:
                raise ValidationError("Too many portfolio scope entries.", field="portfolio_scopes")
        return tuple(sorted(result, key=lambda item: str(item[0])))

    def _portfolio(self, reference: object) -> Portfolio:
        if isinstance(reference, UUID):
            row = self.session.get(Portfolio, reference)
        elif isinstance(reference, str):
            clean = reference.strip()
            try:
                row = self.session.get(Portfolio, UUID(clean))
            except ValueError:
                row = self.session.scalar(select(Portfolio).where(Portfolio.code == clean))
        else:
            row = None
        if row is None:
            raise ValidationError(f"Unknown portfolio {reference!r}.", field="portfolio_scopes")
        return row

    def _role_codes(self, user_id: UUID) -> tuple[str, ...]:
        statement = (
            select(Role.code)
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == user_id)
            .order_by(Role.code)
        )
        return tuple(self.session.execute(statement).scalars().all())

    def _actor_holds_roles(self, user_id: UUID, role_codes: Iterable[str]) -> bool:
        held = set(self._role_codes(user_id))
        return set(role_codes).issubset(held)

    def _is_administrator(self, user_id: UUID) -> bool:
        return bool(set(self._role_codes(user_id)).intersection(_ADMINISTRATOR_ROLE_CODES))

    def _active_administrator_count(self) -> int:
        statement = (
            select(func.count(func.distinct(UserRole.user_id)))
            .select_from(UserRole)
            .join(AppUser, AppUser.id == UserRole.user_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(AppUser.is_active.is_(True), Role.code.in_(_ADMINISTRATOR_ROLE_CODES))
        )
        return int(self.session.scalar(statement) or 0)

    def _permissions_by_role_id(self, role_ids: Sequence[UUID]) -> dict[UUID, tuple[str, ...]]:
        if not role_ids:
            return {}
        statement = (
            select(RolePermission.role_id, Permission.code)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id.in_(role_ids))
            .order_by(RolePermission.role_id, Permission.code)
        )
        result: dict[UUID, list[str]] = {}
        for role_id, code in self.session.execute(statement):
            result.setdefault(role_id, []).append(code)
        return {role_id: tuple(codes) for role_id, codes in result.items()}

    def _user_view(self, row: AppUser) -> AdminUserView:
        role_rows = self.session.execute(
            select(Role)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == row.id)
            .order_by(Role.name, Role.code)
        ).scalars().all()
        permissions = self._permissions_by_role_id(tuple(role.id for role in role_rows))
        roles = tuple(
            RoleView(role.id, role.code, role.name, role.is_system, permissions.get(role.id, ()))
            for role in role_rows
        )
        scope_rows = self.session.execute(
            select(UserPortfolioScope, Portfolio)
            .join(Portfolio, Portfolio.id == UserPortfolioScope.portfolio_id)
            .where(UserPortfolioScope.user_id == row.id)
            .order_by(Portfolio.path, Portfolio.code)
        ).all()
        scopes = tuple(
            PortfolioScopeView(
                portfolio_id=scope.portfolio_id,
                code=portfolio.code,
                name=portfolio.name,
                path=portfolio.path,
                include_descendants=scope.include_descendants,
            )
            for scope, portfolio in scope_rows
        )
        active_sessions = self.session.scalar(
            select(func.count(UserSession.id)).where(
                UserSession.user_id == row.id, UserSession.revoked_at.is_(None)
            )
        )
        active_keys = self.session.scalar(
            select(func.count(ApiKey.id)).where(
                ApiKey.created_by_id == row.id, ApiKey.revoked_at.is_(None)
            )
        )
        return AdminUserView(
            id=row.id,
            username=row.username,
            email=row.email,
            full_name=row.full_name,
            auth_source=row.auth_source,
            external_subject=row.external_subject,
            is_active=row.is_active,
            must_change_password=row.must_change_password,
            locale=row.locale,
            theme=row.theme,
            version=row.version,
            created_at=row.created_at,
            roles=roles,
            scopes=scopes,
            active_session_count=int(active_sessions or 0),
            active_api_key_count=int(active_keys or 0),
        )

    def _user_snapshot(self, row: AppUser) -> dict[str, object]:
        view = self._user_view(row)
        return {
            "id": str(view.id),
            "username": view.username,
            "email": view.email,
            "full_name": view.full_name,
            "auth_source": view.auth_source,
            "external_subject": view.external_subject,
            "is_active": view.is_active,
            "roles": list(view.role_codes),
            "scopes": [
                {
                    "portfolio_id": str(scope.portfolio_id),
                    "portfolio_code": scope.code,
                    "include_descendants": scope.include_descendants,
                }
                for scope in view.scopes
            ],
            "version": view.version,
        }

    def _touch_user(self, row: AppUser, actor_id: UUID, now: datetime) -> None:
        row.updated_at = now
        row.updated_by_id = actor_id
        row.request_id = self._write_request_id()
        row.version += 1

    def _maker_request(self, request_id: UUID | str, *, lock: bool) -> MakerCheckerRequest:
        parsed = _uuid(request_id, "request_id")
        statement = select(MakerCheckerRequest).where(
            MakerCheckerRequest.id == parsed,
            MakerCheckerRequest.operation == ROLE_ASSIGNMENT_OPERATION,
        )
        if lock:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        if row is None:
            raise NotFound(f"Role assignment {parsed} was not found.")
        return row

    def _invalidate_permissions(self, user_id: UUID) -> None:
        if self.roles is not None:
            self.roles.invalidate_user(user_id)

    def _require_admin(self, principal: Principal) -> None:
        if not isinstance(principal, Principal) or principal.kind is not PrincipalKind.USER:
            raise AuthorizationError(
                "An authenticated administrator is required.", field="principal"
            )
        authorize(principal, ADMIN_PERMISSION)
        actor = self.session.get(AppUser, principal.id)
        if actor is None or not actor.is_active:
            raise AuthorizationError("The administrator account is unavailable.", field="principal")

    def _now(self) -> datetime:
        value = self.clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("AdminUsersService clock must return an aware datetime.")
        return value.astimezone(UTC)

    def _write_request_id(self) -> str:
        """Use the inbound request id while retaining a safe service fallback."""

        return get_request_id() or self.request_id

    def _audit(
        self,
        event_type: AuditEventType,
        subject: object,
        payload: Mapping[str, object],
        principal: Principal,
    ) -> None:
        self.audit.record(
            event_type.value,
            (
                getattr(subject, "__tablename__", None) or _subject_type(subject),
                _subject_id(subject),
            ),
            dict(payload),
            actor=principal.id,
            request_id=self._write_request_id(),
        )


def _pending_request(row: MakerCheckerRequest) -> PendingRoleAssignment:
    payload = _role_payload(row.payload)
    return PendingRoleAssignment(
        id=row.id,
        user_id=payload.user_id,
        maker_id=row.maker_id,
        role_codes=payload.role_codes,
        created_at=row.created_at,
        version=row.version,
    )


@dataclass(frozen=True, slots=True)
class _RolePayload:
    user_id: UUID
    role_codes: tuple[str, ...]
    before_role_codes: tuple[str, ...]
    expected_version: int


def _role_payload(payload: Mapping[str, object]) -> _RolePayload:
    user_id = _uuid(payload.get("user_id"), "payload.user_id")
    raw_codes = payload.get("role_codes")
    if not isinstance(raw_codes, list | tuple) or any(
        not isinstance(code, str) or _ROLE_CODE_PATTERN.fullmatch(code) is None
        for code in raw_codes
    ):
        raise ValidationError(
            "Role assignment payload has invalid role_codes.", field="payload.role_codes"
        )
    codes = tuple(sorted(dict.fromkeys(cast(str, code) for code in raw_codes)))
    raw_before = payload.get("before")
    if not isinstance(raw_before, Mapping):
        raise ValidationError(
            "Role assignment payload has no prior user state.", field="payload.before"
        )
    raw_before_roles = raw_before.get("roles")
    if not isinstance(raw_before_roles, list | tuple) or any(
        not isinstance(code, str) or _ROLE_CODE_PATTERN.fullmatch(code) is None
        for code in raw_before_roles
    ):
        raise ValidationError(
            "Role assignment payload has invalid prior roles.", field="payload.before.roles"
        )
    before_codes = tuple(
        sorted(dict.fromkeys(cast(str, code) for code in raw_before_roles))
    )
    expected = payload.get("expected_version")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise ValidationError(
            "Role assignment payload has an invalid version.", field="payload.expected_version"
        )
    return _RolePayload(user_id, codes, before_codes, expected)


def _session_view(row: UserSession) -> SessionView:
    return SessionView(
        id=row.id,
        user_id=row.user_id,
        issued_at=row.issued_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        absolute_expires_at=row.absolute_expires_at,
        revoked_at=row.revoked_at,
    )


def _session_snapshot(row: UserSession) -> dict[str, object]:
    view = _session_view(row)
    return {
        "id": str(view.id),
        "user_id": str(view.user_id),
        "issued_at": view.issued_at.isoformat(),
        "last_seen_at": view.last_seen_at.isoformat(),
        "expires_at": view.expires_at.isoformat(),
        "absolute_expires_at": view.absolute_expires_at.isoformat(),
        "revoked_at": view.revoked_at.isoformat() if view.revoked_at else None,
    }


def _subject_type(subject: object) -> str:
    if isinstance(subject, MakerCheckerRequest):
        return "maker_checker_request"
    if isinstance(subject, UserSession):
        return "user_session"
    if isinstance(subject, AppUser):
        return "app_user"
    return type(subject).__name__.lower()


def _subject_id(subject: object) -> UUID:
    value = getattr(subject, "id", None)
    if not isinstance(value, UUID):
        raise TypeError("An audited admin subject must have a UUID id.")
    return value


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} must be a UUID.", field=field)


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text.", field=field)
    clean = value.strip()
    if not clean or len(clean) > maximum:
        raise ValidationError(
            f"{field} must contain between 1 and {maximum} characters.", field=field
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ValidationError(f"{field} contains a control character.", field=field)
    return clean


def _username(value: object) -> str:
    return _text(value, "username", _USERNAME_MAX_LENGTH).casefold()


def _choice(value: object, field: str, choices: Iterable[str]) -> str:
    clean = _text(value, field, 32).lower()
    if clean not in set(choices):
        raise ValidationError(f"{field} must be one of: {', '.join(sorted(choices))}.", field=field)
    return clean


def _sso_mapping(auth_source: object, external_subject: object) -> tuple[str, str | None]:
    source = _choice(auth_source, "auth_source", _AUTH_SOURCES)
    if source == "local":
        if external_subject not in {None, ""}:
            raise ValidationError(
                "Local users cannot have an external subject.", field="external_subject"
            )
        return source, None
    subject = _text(external_subject, "external_subject", _EXTERNAL_SUBJECT_MAX_LENGTH)
    return source, subject


def _reason(value: object) -> str:
    clean = validate_reason(value, required=True)
    assert clean is not None
    return clean


__all__ = [
    "ADMIN_PERMISSION",
    "AdminUserView",
    "AdminUsersService",
    "PendingRoleAssignment",
    "PortfolioScopeView",
    "RoleAssignmentResult",
    "RoleView",
    "ROLE_ASSIGNMENT_OPERATION",
    "SessionView",
]
