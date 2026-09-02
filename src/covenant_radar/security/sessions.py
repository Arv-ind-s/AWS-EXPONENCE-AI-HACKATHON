"""Signed browser sessions with server-side revocation.

The browser receives a signed envelope containing a random bearer token.  The
database-facing store receives only a SHA-256 digest of that token, never the
token itself.  The service is deliberately persistence-neutral: a deployment
provides a store backed by its unit of work, while tests can use the included
small in-memory store.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Literal, Protocol, TypedDict
from uuid import UUID

from itsdangerous import BadData, URLSafeSerializer

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.ids import new_id


class SessionAuditWriter(Protocol):
    """The C-60 audit surface used by the session service."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an audit event in the caller's transaction."""


@dataclass(frozen=True, slots=True)
class SessionSettings:
    """Cookie and timeout settings for local browser sessions."""

    cookie_name: str = "covenant_radar_session"
    secret: bytes | str = b""
    idle_timeout: timedelta = timedelta(minutes=30)
    absolute_timeout: timedelta = timedelta(hours=12)
    challenge_timeout: timedelta = timedelta(minutes=5)
    secure_cookie: bool = True
    same_site: Literal["lax", "strict", "none"] = "lax"
    cookie_path: str = "/"

    def __post_init__(self) -> None:
        secret = self.secret.encode("utf-8") if isinstance(self.secret, str) else self.secret
        if len(secret) < 32:
            raise ValueError("Session signing secret must contain at least 32 bytes.")
        if not self.cookie_name or len(self.cookie_name) > 128:
            raise ValueError("Session cookie name must contain between 1 and 128 characters.")
        if any(character.isspace() or character in ";,=" for character in self.cookie_name):
            raise ValueError("Session cookie name contains an invalid character.")
        if self.idle_timeout <= timedelta(0):
            raise ValueError("Session idle timeout must be positive.")
        if self.absolute_timeout <= timedelta(0):
            raise ValueError("Session absolute timeout must be positive.")
        if self.idle_timeout > self.absolute_timeout:
            raise ValueError("Session idle timeout must not exceed absolute timeout.")
        if self.challenge_timeout <= timedelta(0):
            raise ValueError("Authentication challenge timeout must be positive.")
        if self.same_site not in {"lax", "strict", "none"}:
            raise ValueError("Session SameSite must be lax, strict or none.")
        if self.same_site == "none" and not self.secure_cookie:
            raise ValueError("SameSite=None cookies must be Secure.")
        if not self.cookie_path.startswith("/"):
            raise ValueError("Session cookie path must be absolute.")
        object.__setattr__(self, "secret", secret)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Persistence-neutral representation of ``plan.md``'s user_session."""

    id: UUID
    user_id: UUID
    token_hash: str
    issued_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    ip_hash: str | None = None
    user_agent_hash: str | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """The one-time result of issuing or refreshing a browser session."""

    cookie: str
    record: SessionRecord


@dataclass(frozen=True, slots=True)
class Challenge:
    """A short-lived, signed pre-authentication challenge."""

    cookie: str
    user_id: UUID
    purpose: str
    issued_at: datetime
    expires_at: datetime
    claims: Mapping[str, object]
    nonce: str


class SessionStore(Protocol):
    """Persistence port for session rows."""

    def create(self, record: SessionRecord) -> None:
        """Insert a new session row."""

    def get_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        """Find a session by its token digest."""

    def save(self, record: SessionRecord) -> None:
        """Persist changes to an existing session row."""

    def revoke_all(self, user_id: UUID, revoked_at: datetime) -> int:
        """Revoke all sessions for a user and return the affected count."""


class ChallengeReplayStore(Protocol):
    """Atomic short-lived challenge claim/release port.

    A production deployment should back this with a shared store when it
    runs multiple workers.  The included implementation is thread-safe and
    suitable for a single-process deployment and tests.
    """

    def claim(self, nonce: str, expires_at: datetime) -> bool:
        """Atomically claim a nonce, returning false if already claimed."""

    def release(self, nonce: str) -> None:
        """Release a claim after a failed verification attempt."""


class CookieAttributes(TypedDict):
    """Typed Starlette-compatible cookie arguments."""

    key: str
    httponly: bool
    secure: bool
    samesite: Literal["lax", "strict", "none"]
    path: str
    max_age: int


class InMemorySessionStore:
    """Thread-safe reference store for local development and tests."""

    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}
        self._lock = RLock()

    def create(self, record: SessionRecord) -> None:
        with self._lock:
            if record.token_hash in self._records:
                raise ValueError("A session with this token digest already exists.")
            self._records[record.token_hash] = record

    def get_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        with self._lock:
            return self._records.get(token_hash)

    def save(self, record: SessionRecord) -> None:
        with self._lock:
            if record.token_hash not in self._records:
                raise KeyError("Session does not exist.")
            self._records[record.token_hash] = record

    def revoke_all(self, user_id: UUID, revoked_at: datetime) -> int:
        with self._lock:
            affected = 0
            for token_hash, record in tuple(self._records.items()):
                if record.user_id == user_id and record.revoked_at is None:
                    self._records[token_hash] = replace(record, revoked_at=revoked_at)
                    affected += 1
            return affected

    def records(self) -> tuple[SessionRecord, ...]:
        """Return a stable snapshot for diagnostics and focused tests."""
        with self._lock:
            return tuple(self._records.values())


class InMemoryChallengeReplayStore:
    """Thread-safe single-process implementation of challenge replay control."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._claimed: dict[str, datetime] = {}
        self._lock = RLock()
        self._clock = clock or SystemClock()

    def claim(self, nonce: str, expires_at: datetime) -> bool:
        with self._lock:
            now = self._clock.now()
            self._claimed = {
                value: expiry for value, expiry in self._claimed.items() if expiry > now
            }
            if nonce in self._claimed:
                return False
            self._claimed[nonce] = expires_at
            return True

    def release(self, nonce: str) -> None:
        with self._lock:
            self._claimed.pop(nonce, None)


class SessionManager:
    """Issue, validate, refresh and revoke signed sessions."""

    _CHALLENGE_PURPOSES = frozenset({"mfa", "mfa_enrollment", "password_change"})

    def __init__(
        self,
        store: SessionStore,
        *,
        settings: SessionSettings,
        clock: Clock | None = None,
        audit: SessionAuditWriter | None = None,
        challenge_replay: ChallengeReplayStore | None = None,
        request_id: str | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock or SystemClock()
        self.audit = audit
        self.challenge_replay = challenge_replay or InMemoryChallengeReplayStore(clock=self.clock)
        self.request_id = request_id or get_request_id() or new_request_id()
        self._serializer = URLSafeSerializer(settings.secret, salt="covenant-radar/session/v1")

    def issue(
        self,
        user_id: UUID,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> IssuedSession:
        """Create a session row and its signed cookie value."""
        issued_at = _utc(now or self.clock.now())
        absolute_expires_at = issued_at + self.settings.absolute_timeout
        expires_at = min(issued_at + self.settings.idle_timeout, absolute_expires_at)
        raw_token = secrets.token_urlsafe(32)
        record = SessionRecord(
            id=new_id(),
            user_id=user_id,
            token_hash=_token_hash(raw_token),
            issued_at=issued_at,
            last_seen_at=issued_at,
            expires_at=expires_at,
            absolute_expires_at=absolute_expires_at,
            ip_hash=_client_hash(ip_address, self.settings.secret),
            user_agent_hash=_client_hash(user_agent, self.settings.secret),
        )
        self.store.create(record)
        cookie = self._serialize_session(record.id, raw_token)
        self._audit(
            "authentication_session_issued",
            user_id,
            {"session_id": str(record.id)},
            actor=user_id,
        )
        return IssuedSession(cookie=cookie, record=record)

    def validate(self, cookie: str | None, *, now: datetime | None = None) -> SessionRecord | None:
        """Return a live session row, or ``None`` for every invalid case."""
        parsed = self._parse_session(cookie)
        if parsed is None:
            return None
        session_id, raw_token = parsed
        record = self.store.get_by_token_hash(_token_hash(raw_token))
        if record is None or record.id != session_id:
            return None
        if not _same_digest(record.token_hash, _token_hash(raw_token)):
            return None
        instant = _utc(now or self.clock.now())
        if record.revoked_at is not None:
            return None
        if instant >= record.absolute_expires_at or instant >= record.expires_at:
            return None
        return record

    def refresh(
        self,
        cookie: str | None,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> IssuedSession | None:
        """Move the idle deadline forward without extending the absolute one."""
        record = self.validate(cookie, now=now)
        if record is None or cookie is None:
            return None
        instant = _utc(now or self.clock.now())
        expires_at = min(instant + self.settings.idle_timeout, record.absolute_expires_at)
        if instant >= expires_at:
            return None
        refreshed = replace(
            record,
            last_seen_at=instant,
            expires_at=expires_at,
            ip_hash=_client_hash(ip_address, self.settings.secret) or record.ip_hash,
            user_agent_hash=_client_hash(user_agent, self.settings.secret)
            or record.user_agent_hash,
        )
        self.store.save(refreshed)
        self._audit(
            "authentication_session_refreshed",
            record.user_id,
            {"session_id": str(record.id)},
            actor=record.user_id,
        )
        return IssuedSession(cookie=cookie, record=refreshed)

    def revoke(self, cookie: str | None, *, now: datetime | None = None) -> bool:
        """Revoke one session, including a session that has just expired."""
        parsed = self._parse_session(cookie)
        if parsed is None:
            return False
        session_id, raw_token = parsed
        record = self.store.get_by_token_hash(_token_hash(raw_token))
        if record is None or record.id != session_id:
            return False
        if record.revoked_at is not None:
            return False
        instant = _utc(now or self.clock.now())
        self.store.save(replace(record, revoked_at=instant))
        self._audit(
            "authentication_session_revoked",
            record.user_id,
            {"session_id": str(record.id), "reason": "logout"},
            actor=record.user_id,
        )
        return True

    def revoke_all(self, user_id: UUID, *, now: datetime | None = None, reason: str) -> int:
        """Revoke every session for *user_id* and audit the invalidation."""
        instant = _utc(now or self.clock.now())
        count = self.store.revoke_all(user_id, instant)
        self._audit(
            "authentication_sessions_revoked",
            user_id,
            {"count": count, "reason": _safe_reason(reason)},
            actor=user_id,
        )
        return count

    def issue_challenge(
        self,
        user_id: UUID,
        purpose: str,
        *,
        claims: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> Challenge:
        """Issue a signed, short-lived pre-authentication challenge."""
        if purpose not in self._CHALLENGE_PURPOSES:
            raise ValueError(f"Unsupported authentication challenge purpose: {purpose!r}.")
        issued_at = _utc(now or self.clock.now())
        expires_at = issued_at + self.settings.challenge_timeout
        nonce = secrets.token_urlsafe(24)
        clean_claims = dict(claims or {})
        if any(not isinstance(key, str) for key in clean_claims):
            raise TypeError("Authentication challenge claim keys must be strings.")
        payload: dict[str, object] = {
            "v": 1,
            "sub": str(user_id),
            "purpose": purpose,
            "iat": issued_at.isoformat(),
            "exp": expires_at.isoformat(),
            "nonce": nonce,
            "claims": clean_claims,
        }
        cookie = self._serializer.dumps(payload)
        return Challenge(
            cookie=cookie,
            user_id=user_id,
            purpose=purpose,
            issued_at=issued_at,
            expires_at=expires_at,
            claims=clean_claims,
            nonce=nonce,
        )

    def read_challenge(
        self,
        cookie: str | None,
        *,
        purpose: str | None = None,
        now: datetime | None = None,
    ) -> Challenge | None:
        """Validate a challenge envelope without consuming its nonce."""
        payload = self._parse_payload(cookie)
        if payload is None:
            return None
        if set(payload) != {"v", "sub", "purpose", "iat", "exp", "nonce", "claims"}:
            return None
        if payload.get("v") != 1 or not isinstance(payload["sub"], str):
            return None
        if (
            not isinstance(payload["purpose"], str)
            or payload["purpose"] not in self._CHALLENGE_PURPOSES
        ):
            return None
        if purpose is not None and payload["purpose"] != purpose:
            return None
        if not isinstance(payload["iat"], str) or not isinstance(payload["exp"], str):
            return None
        if not isinstance(payload["nonce"], str) or not payload["nonce"]:
            return None
        if not isinstance(payload["claims"], dict):
            return None
        try:
            user_id = UUID(payload["sub"])
            issued_at = _utc(datetime.fromisoformat(payload["iat"]))
            expires_at = _utc(datetime.fromisoformat(payload["exp"]))
        except (TypeError, ValueError):
            return None
        instant = _utc(now or self.clock.now())
        if issued_at > instant or instant >= expires_at:
            return None
        return Challenge(
            cookie=cookie or "",
            user_id=user_id,
            purpose=payload["purpose"],
            issued_at=issued_at,
            expires_at=expires_at,
            claims=dict(payload["claims"]),
            nonce=payload["nonce"],
        )

    def claim_challenge(self, challenge: Challenge) -> bool:
        """Atomically claim a challenge for a successful completion."""
        return self.challenge_replay.claim(challenge.nonce, challenge.expires_at)

    def release_challenge(self, challenge: Challenge) -> None:
        """Allow another attempt after a failed MFA/password check."""
        self.challenge_replay.release(challenge.nonce)

    def cookie_attributes(self) -> CookieAttributes:
        """Return safe arguments for a framework's ``set_cookie`` method."""
        max_age = max(1, int(self.settings.idle_timeout.total_seconds()))
        return {
            "key": self.settings.cookie_name,
            "httponly": True,
            "secure": self.settings.secure_cookie,
            "samesite": self.settings.same_site,
            "path": self.settings.cookie_path,
            "max_age": max_age,
        }

    def challenge_cookie_attributes(
        self, name: str = "covenant_radar_challenge"
    ) -> CookieAttributes:
        """Return safe arguments for a pre-authentication challenge cookie."""
        return {
            "key": name,
            "httponly": True,
            "secure": self.settings.secure_cookie,
            "samesite": self.settings.same_site,
            "path": self.settings.cookie_path,
            "max_age": max(1, int(self.settings.challenge_timeout.total_seconds())),
        }

    def _serialize_session(self, session_id: UUID, raw_token: str) -> str:
        return self._serializer.dumps({"v": 1, "sid": str(session_id), "token": raw_token})

    def _parse_session(self, cookie: str | None) -> tuple[UUID, str] | None:
        payload = self._parse_payload(cookie)
        if payload is None or set(payload) != {"v", "sid", "token"}:
            return None
        if payload.get("v") != 1:
            return None
        session_id_value = payload.get("sid")
        token_value = payload.get("token")
        if not isinstance(session_id_value, str) or not isinstance(token_value, str):
            return None
        raw_token = token_value
        if not raw_token or len(raw_token) > 256:
            return None
        try:
            return UUID(session_id_value), raw_token
        except ValueError:
            return None

    def _parse_payload(self, cookie: str | None) -> dict[str, object] | None:
        if not cookie or len(cookie) > 4096:
            return None
        try:
            payload = self._serializer.loads(cookie)
        except BadData:
            return None
        return payload if isinstance(payload, dict) else None

    def _audit(
        self,
        event_type: str,
        subject_id: UUID,
        payload: Mapping[str, object],
        *,
        actor: object,
    ) -> None:
        if self.audit is not None:
            self.audit.record(
                event_type,
                ("app_user", subject_id),
                dict(payload),
                actor=actor,
                request_id=self.request_id,
            )


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _same_digest(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


def _client_hash(value: str | None, key: bytes | str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return None
    material = key.encode("utf-8") if isinstance(key, str) else key
    return hmac.new(material, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _safe_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Session revocation reason must be non-empty text.")
    clean = " ".join(reason.split())
    return clean[:100]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Authentication timestamps must be timezone-aware.")
    return value.astimezone(UTC)


SessionService = SessionManager
