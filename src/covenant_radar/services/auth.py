"""Local authentication use cases.

The service depends on small persistence and audit protocols rather than on a
database session or web framework.  A database adapter can map
``AppUser``/``UserSession`` rows to the records below while keeping the
transaction boundary in the caller's unit of work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol
from uuid import UUID

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import Conflict, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.security.mfa import MfaEnrollment, TOTPService
from covenant_radar.security.passwords import PasswordService
from covenant_radar.security.sessions import Challenge, IssuedSession, SessionManager, SessionRecord

GENERIC_AUTHENTICATION_MESSAGE = "Invalid username or password."
GENERIC_MFA_MESSAGE = "The verification code is invalid."
GENERIC_SESSION_MESSAGE = "The sign-in session is invalid or expired."


class AuthStatus(str, Enum):
    """The deliberately small set of states exposed by the sign-in flow."""

    SUCCESS = "success"
    FAILED = "failed"
    MFA_REQUIRED = "mfa_required"
    MFA_ENROLLMENT_REQUIRED = "mfa_enrollment_required"
    PASSWORD_CHANGE_REQUIRED = "password_change_required"


@dataclass(frozen=True, slots=True)
class UserRecord:
    """Persistence-neutral local-user record.

    ``password_history`` is optional at the model boundary because the
    initial identity schema stores the active hash only.  A deployment that
    maintains historical hashes supplies them here; policy validation still
    covers the active hash in every deployment.
    """

    id: UUID
    username: str
    password_hash: str | None
    is_active: bool = True
    failed_attempts: int = 0
    locked_until: datetime | None = None
    password_changed_at: datetime | None = None
    must_change_password: bool = False
    mfa_secret_enc: str | None = None
    password_history: tuple[str, ...] = ()


class UserStore(Protocol):
    """Persistence port for local-user state."""

    def find_by_username(self, username: str) -> UserRecord | None:
        """Return a local user without exposing lookup details to callers."""

    def get(self, user_id: UUID) -> UserRecord | None:
        """Return a user by id."""

    def save(self, user: UserRecord) -> None:
        """Persist a user update in the current transaction."""


class AuditWriter(Protocol):
    """The C-60 audit surface used by authentication."""

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


@dataclass(frozen=True, slots=True)
class AuthenticationSettings:
    """Local-authentication controls not present in the base identity table."""

    lockout_threshold: int = 5
    lockout_duration: timedelta = timedelta(minutes=15)
    mfa_required: bool = False

    def __post_init__(self) -> None:
        if self.lockout_threshold < 1:
            raise ValueError("Lockout threshold must be positive.")
        if self.lockout_duration <= timedelta(0):
            raise ValueError("Lockout duration must be positive.")


@dataclass(frozen=True, slots=True)
class AuthResult:
    """A safe result for a browser authentication transition."""

    status: AuthStatus
    message: str | None = None
    user_id: UUID | None = None
    session: IssuedSession | None = None
    challenge_cookie: str | None = None

    @property
    def authenticated(self) -> bool:
        """Whether the result contains a fully authenticated session."""
        return self.status is AuthStatus.SUCCESS and self.session is not None


@dataclass(frozen=True, slots=True)
class EnrollmentChallenge:
    """Enrollment material paired with the signed challenge cookie."""

    challenge_cookie: str
    enrollment: MfaEnrollment


class AuthService:
    """Coordinate password, MFA, session and audit state transitions."""

    def __init__(
        self,
        users: UserStore,
        sessions: SessionManager,
        *,
        passwords: PasswordService | None = None,
        mfa: TOTPService | None = None,
        settings: AuthenticationSettings | None = None,
        clock: Clock | None = None,
        audit: AuditWriter,
        request_id: str | None = None,
    ) -> None:
        self.users = users
        self.sessions = sessions
        self.passwords = passwords or PasswordService()
        self.mfa = mfa
        self.settings = settings or AuthenticationSettings()
        self.clock = clock or SystemClock()
        self.audit = audit
        self.request_id = request_id or get_request_id() or new_request_id()
        if self.settings.mfa_required and self.mfa is None:
            raise ValueError("MFA is required but no TOTP service was configured.")

    def sign_in(
        self,
        username: str,
        password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        """Authenticate one local user without account-enumerating responses."""
        now = _utc(self.clock.now())
        normalized_username = _normalise_username(username)
        user = self.users.find_by_username(normalized_username)

        # Always perform one Argon2 verification, including for an unknown,
        # inactive or locked account.  The password service supplies a
        # process-local dummy hash when no real hash is available.
        verification = self.passwords.verify(
            password if isinstance(password, str) else "", user.password_hash if user else None
        )

        if user is None:
            self._audit_failure(None, "credential_rejected")
            return self._failed_result()

        locked = user.locked_until is not None and now < _utc(user.locked_until)
        if locked:
            self._audit_failure(user, "account_locked")
            return self._failed_result()

        # The lockout window is also the unlock window.  Start a fresh
        # failure count once it has elapsed, so the first bad attempt after
        # an automatic unlock does not immediately lock the account again.
        if user.locked_until is not None:
            user = replace(user, failed_attempts=0, locked_until=None)
            self.users.save(user)

        if not user.is_active or not verification.valid:
            if not user.is_active:
                self._audit_failure(user, "account_inactive")
            else:
                failed_attempts = max(0, user.failed_attempts) + 1
                new_locked_until = (
                    now + self.settings.lockout_duration
                    if failed_attempts >= self.settings.lockout_threshold
                    else None
                )
                updated = replace(
                    user,
                    failed_attempts=failed_attempts,
                    locked_until=new_locked_until,
                )
                self.users.save(updated)
                self._audit_failure(
                    user,
                    "account_locked" if new_locked_until is not None else "credential_rejected",
                )
            return self._failed_result()

        # A successful attempt after the unlock window clears the counter.
        updated = replace(
            user,
            failed_attempts=0,
            locked_until=None,
            password_hash=(
                self.passwords.hash(password, validate=False)
                if verification.needs_rehash
                else user.password_hash
            ),
        )
        if updated != user:
            self.users.save(updated)
            user = updated

        self._audit_success(user, "password_verified")

        if self.settings.mfa_required and user.mfa_secret_enc is None:
            challenge = self.sessions.issue_challenge(user.id, "mfa_enrollment", now=now)
            self._audit_success(user, "mfa_enrollment_required")
            return AuthResult(
                status=AuthStatus.MFA_ENROLLMENT_REQUIRED,
                user_id=user.id,
                challenge_cookie=challenge.cookie,
            )

        if self.settings.mfa_required:
            challenge = self.sessions.issue_challenge(user.id, "mfa", now=now)
            self._audit_success(user, "mfa_required")
            return AuthResult(
                status=AuthStatus.MFA_REQUIRED,
                user_id=user.id,
                challenge_cookie=challenge.cookie,
            )

        return self._issue_after_primary(
            user,
            now=now,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    authenticate = sign_in

    def verify_mfa(
        self,
        challenge_cookie: str,
        code: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        """Complete a second-factor challenge and issue the next state."""
        challenge = self.sessions.read_challenge(challenge_cookie, purpose="mfa")
        if challenge is None:
            self._audit_failure(None, "mfa_challenge_invalid")
            return AuthResult(status=AuthStatus.FAILED, message=GENERIC_MFA_MESSAGE)
        user = self.users.get(challenge.user_id)
        if user is None or not user.is_active or user.mfa_secret_enc is None:
            self._audit_failure(user, "mfa_challenge_invalid")
            return AuthResult(status=AuthStatus.FAILED, message=GENERIC_MFA_MESSAGE)
        if not self.sessions.claim_challenge(challenge):
            self._audit_failure(user, "mfa_challenge_replayed")
            return AuthResult(status=AuthStatus.FAILED, message=GENERIC_MFA_MESSAGE)
        if self.mfa is None or not self.mfa.verify(user.mfa_secret_enc, code, now=self.clock.now()):
            self.sessions.release_challenge(challenge)
            self._audit_failure(user, "mfa_code_rejected")
            return AuthResult(status=AuthStatus.FAILED, message=GENERIC_MFA_MESSAGE)

        self._audit_success(user, "mfa_verified")
        return self._issue_after_primary(
            user,
            now=_utc(self.clock.now()),
            ip_address=ip_address,
            user_agent=user_agent,
        )

    def begin_mfa_enrollment(self, challenge_cookie: str) -> EnrollmentChallenge:
        """Create enrollment material for a password-verified user."""
        challenge = self.sessions.read_challenge(challenge_cookie, purpose="mfa_enrollment")
        if challenge is None:
            raise ValidationError(GENERIC_SESSION_MESSAGE)
        user = self.users.get(challenge.user_id)
        if user is None or not user.is_active:
            raise ValidationError(GENERIC_SESSION_MESSAGE)
        if user.mfa_secret_enc is not None:
            raise Conflict("MFA is already enrolled for this account.")
        if self.mfa is None:
            raise ValidationError("MFA enrollment is not available.")
        enrollment = self.mfa.enroll(user.username)
        refreshed_challenge = self.sessions.issue_challenge(
            user.id,
            "mfa_enrollment",
            claims={"encrypted_secret": enrollment.encrypted_secret},
            now=_utc(self.clock.now()),
        )
        self._audit_success(user, "mfa_enrollment_started")
        return EnrollmentChallenge(
            challenge_cookie=refreshed_challenge.cookie,
            enrollment=enrollment,
        )

    def complete_mfa_enrollment(
        self,
        challenge_cookie: str,
        code: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        """Verify the first TOTP code and persist the encrypted secret."""
        challenge = self.sessions.read_challenge(challenge_cookie, purpose="mfa_enrollment")
        if challenge is None:
            self._audit_failure(None, "mfa_enrollment_challenge_invalid")
            return AuthResult(status=AuthStatus.FAILED, message=GENERIC_MFA_MESSAGE)
        encrypted_secret = challenge.claims.get("encrypted_secret")
        user = self.users.get(challenge.user_id)
        if user is None or not user.is_active or not isinstance(encrypted_secret, str):
            self._audit_failure(user, "mfa_enrollment_challenge_invalid")
            return AuthResult(status=AuthStatus.FAILED, message=GENERIC_MFA_MESSAGE)
        if not self.sessions.claim_challenge(challenge):
            self._audit_failure(user, "mfa_enrollment_replayed")
            return AuthResult(status=AuthStatus.FAILED, message=GENERIC_MFA_MESSAGE)
        if self.mfa is None or not self.mfa.verify(encrypted_secret, code, now=self.clock.now()):
            self.sessions.release_challenge(challenge)
            self._audit_failure(user, "mfa_enrollment_code_rejected")
            return AuthResult(status=AuthStatus.FAILED, message=GENERIC_MFA_MESSAGE)

        updated = replace(user, mfa_secret_enc=encrypted_secret)
        self.users.save(updated)
        self._audit_success(updated, "mfa_enrolled")
        return self._issue_after_primary(
            updated,
            now=_utc(self.clock.now()),
            ip_address=ip_address,
            user_agent=user_agent,
        )

    def change_password(
        self,
        credential: str,
        new_password: str,
        confirmation: str | None = None,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        """Change a password from a live session or password-change challenge."""
        user, challenge = self._user_from_credential(credential, purpose="password_change")
        if user is None:
            self._audit_failure(None, "password_change_credential_invalid")
            raise ValidationError(GENERIC_SESSION_MESSAGE)
        if confirmation is not None and not _same_text(new_password, confirmation):
            raise ValidationError("The new passwords do not match.", field="password_confirmation")
        if not isinstance(new_password, str):
            raise ValidationError("The new password is invalid.", field="password")

        claimed = False
        if challenge is not None:
            if not self.sessions.claim_challenge(challenge):
                self._audit_failure(user, "password_change_challenge_replayed")
                raise ValidationError(GENERIC_SESSION_MESSAGE)
            claimed = True
        try:
            previous_hashes = (user.password_hash,) + user.password_history
            self.passwords.validate_new_password(
                new_password,
                previous_hashes=tuple(value for value in previous_hashes if value),
            )
            password_hash = self.passwords.hash(new_password, validate=False)
            history = ((user.password_hash,) + user.password_history)[
                : self.passwords.policy.history_size
            ]
            updated = replace(
                user,
                password_hash=password_hash,
                password_history=tuple(value for value in history if value),
                password_changed_at=_utc(self.clock.now()),
                must_change_password=False,
                failed_attempts=0,
                locked_until=None,
            )
            self.users.save(updated)
            self.sessions.revoke_all(user.id, now=_utc(self.clock.now()), reason="password_change")
            issued = self.sessions.issue(
                user.id,
                ip_address=ip_address,
                user_agent=user_agent,
                now=_utc(self.clock.now()),
            )
            self._audit_success(updated, "password_changed")
            return AuthResult(status=AuthStatus.SUCCESS, user_id=user.id, session=issued)
        except Exception:
            if claimed:
                assert challenge is not None
                self.sessions.release_challenge(challenge)
            raise

    def sign_out(self, cookie: str | None) -> bool:
        """Revoke the current session and audit both valid and invalid attempts."""
        record = self.sessions.validate(cookie)
        if record is None:
            self._audit_failure(None, "logout_session_invalid")
            return False
        revoked = self.sessions.revoke(cookie)
        if revoked:
            self._audit_success_id(record.user_id, "logout")
        return revoked

    def refresh_session(
        self,
        cookie: str | None,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession | None:
        """Refresh a live session's idle deadline and audit the outcome."""
        refreshed = self.sessions.refresh(
            cookie,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        if refreshed is None:
            self._audit_failure(None, "session_refresh_invalid")
            return None
        self._audit_success_id(refreshed.record.user_id, "session_refreshed")
        return refreshed

    def validate_session(self, cookie: str | None) -> SessionRecord | None:
        """Expose the session read needed by protected web routes."""
        return self.sessions.validate(cookie)

    def revoke_sessions_for_role_change(self, user_id: UUID, *, actor: object) -> int:
        """Invalidate sessions immediately after a role assignment changes."""
        count = self.sessions.revoke_all(
            user_id,
            now=_utc(self.clock.now()),
            reason="role_change",
        )
        self.audit.record(
            AuditEventType.AUTHENTICATION_ROLE_CHANGED_SESSIONS_REVOKED.value,
            ("app_user", user_id),
            {"count": count, "reason": "role_change"},
            actor=actor,
            request_id=self.request_id,
        )
        return count

    handle_role_change = revoke_sessions_for_role_change

    def _issue_after_primary(
        self,
        user: UserRecord,
        *,
        now: datetime,
        ip_address: str | None,
        user_agent: str | None,
    ) -> AuthResult:
        if user.must_change_password:
            challenge = self.sessions.issue_challenge(user.id, "password_change", now=now)
            self._audit_success(user, "password_change_required")
            return AuthResult(
                status=AuthStatus.PASSWORD_CHANGE_REQUIRED,
                user_id=user.id,
                challenge_cookie=challenge.cookie,
            )
        issued = self.sessions.issue(
            user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            now=now,
        )
        self._audit_success(user, "authenticated")
        return AuthResult(status=AuthStatus.SUCCESS, user_id=user.id, session=issued)

    def _user_from_credential(
        self, credential: str, *, purpose: str
    ) -> tuple[UserRecord | None, Challenge | None]:
        session = self.sessions.validate(credential)
        if session is not None:
            return self.users.get(session.user_id), None
        challenge = self.sessions.read_challenge(credential, purpose=purpose)
        if challenge is None:
            return None, None
        return self.users.get(challenge.user_id), challenge

    def _failed_result(self) -> AuthResult:
        return AuthResult(status=AuthStatus.FAILED, message=GENERIC_AUTHENTICATION_MESSAGE)

    def _audit_failure(self, user: UserRecord | None, reason: str) -> None:
        self._audit(
            AuditEventType.AUTHENTICATION_FAILED.value,
            user.id if user is not None else new_id(),
            {"outcome": "failed", "reason": reason},
            actor=None,
        )

    def _audit_success(self, user: UserRecord, reason: str) -> None:
        self._audit_success_id(user.id, reason)

    def _audit_success_id(self, user_id: UUID, reason: str) -> None:
        self._audit(
            AuditEventType.AUTHENTICATION_SUCCEEDED.value,
            user_id,
            {"outcome": "succeeded", "reason": reason},
            actor=user_id,
        )

    def _audit(
        self, event_type: str, user_id: UUID, payload: Mapping[str, object], *, actor: object
    ) -> None:
        self.audit.record(
            event_type,
            ("app_user", user_id),
            dict(payload),
            actor=actor,
            request_id=self.request_id,
        )


def _normalise_username(username: str) -> str:
    if not isinstance(username, str):
        return ""
    clean = username.strip()
    if len(clean) > 64 or any(ord(character) < 32 or ord(character) == 127 for character in clean):
        return ""
    return clean.casefold()


def _same_text(left: str, right: str) -> bool:
    return left == right


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Authentication timestamps must be timezone-aware.")
    return value.astimezone(UTC)
