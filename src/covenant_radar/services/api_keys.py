"""API-key lifecycle: issue, rotate, revoke and list scoped service credentials.

Mirrors `security/sessions.py`'s hash-only persistence discipline: the raw
credential is generated here, returned to the caller exactly once, and never
stored, logged or re-derivable afterward — only its SHA-256 digest
(`ApiKey.key_hash`) and a short display `prefix` persist. Authentication of
an already-issued key at request time is `api/keys.py`'s job, not this
module's; this service is the administrative surface a human (gated on
`MANAGE_USERS`, the same permission the spec's access matrix grants an
administrator for "manage users, roles, connectors, jobs") uses to create
and retire those credentials.

A key's `portfolio_scope` follows `db/scoping.py`'s absence-is-no-access
rule: a key issued with no portfolio scope can read no portfolio-scoped
resource at all, exactly like a user with no `user_portfolio_scope` row.
There is no "grants everything" option.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.identity import ApiKey
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission, coerce_permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize

_KEY_LABEL = "crk"
_SECRET_BYTES = 32
_PREFIX_LENGTH = 16
_NAME_MAX_LENGTH = 100
_REASON_MAX_LENGTH = 500

#: The permission gating every API-key administration action. Service-to-
#: service credentials are an identity/access-administration concern, not a
#: distinct row in `spec §16.1`'s matrix, so they are gated on the same
#: permission that already covers "manage users, roles, connectors, jobs".
MANAGE_API_KEYS_PERMISSION = Permission.MANAGE_USERS


class ApiKeyAuditWriter(Protocol):
    """The append-only audit port from contract C-60."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's current transaction."""


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """The one-time result of issuing or rotating a key.

    ``raw_key`` is the only place the plaintext credential ever exists
    outside the caller's memory; it is never persisted and this object must
    never be logged or passed to the audit port.
    """

    id: UUID
    name: str
    prefix: str
    raw_key: str
    scopes: tuple[str, ...]
    portfolio_scope: tuple[str, ...] | None
    rate_limit_per_min: int
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ApiKeyView:
    """A non-secret administrative view of a persisted key."""

    id: UUID
    name: str
    prefix: str
    scopes: tuple[str, ...]
    portfolio_scope: tuple[str, ...] | None
    rate_limit_per_min: int
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    version: int


class ApiKeyService:
    """Coordinate API-key issuance, rotation, revocation and listing.

    ``session`` must belong to the caller's current unit of work, exactly
    like every other service in this layer: a key's creation and its audit
    event must never be able to land in different transactions.
    """

    def __init__(
        self,
        session: Session,
        *,
        audit: ApiKeyAuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
    ) -> None:
        if not isinstance(session, Session):
            raise TypeError("ApiKeyService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("ApiKeyService requires an append-only audit writer.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 40:
            raise ValueError("ApiKeyService request_id must be between 1 and 40 characters.")

    # ---- issuance and lifecycle ------------------------------------------

    def issue(
        self,
        principal: Principal,
        *,
        name: str,
        scopes: Sequence[Permission | str],
        portfolio_scope: Sequence[str] | None = None,
        rate_limit_per_min: int = 60,
        expires_at: datetime | None = None,
    ) -> IssuedApiKey:
        """Create a new key and return its plaintext credential exactly once."""
        self._require_principal(principal)
        clean_name = _required_text(name, "api_key.name", maximum=_NAME_MAX_LENGTH)
        normalized_scopes = _normalise_scopes(scopes)
        normalized_paths = _normalise_portfolio_scope(portfolio_scope)
        _validate_rate_limit(rate_limit_per_min)
        now = self._now()
        normalized_expiry = _normalise_expiry(expires_at, now)

        raw_key, prefix, key_hash = _generate_credential()
        row = ApiKey(
            id=new_id(),
            name=clean_name,
            key_hash=key_hash,
            prefix=prefix,
            scopes=list(normalized_scopes),
            portfolio_scope=list(normalized_paths) if normalized_paths is not None else None,
            rate_limit_per_min=rate_limit_per_min,
            expires_at=normalized_expiry,
            last_used_at=None,
            revoked_at=None,
            created_at=now,
            updated_at=now,
            created_by_id=self._attributed_id(principal),
            updated_by_id=self._attributed_id(principal),
            request_id=self.request_id,
        )
        self.session.add(row)
        self._flush_or_conflict("An API key with this credential already exists.")
        self._audit(
            "api_key_issued",
            row,
            {
                "action": "issued",
                "name": row.name,
                "prefix": row.prefix,
                "scopes": list(normalized_scopes),
                "portfolio_scope": list(normalized_paths) if normalized_paths is not None else None,
                "rate_limit_per_min": rate_limit_per_min,
                "expires_at": normalized_expiry.isoformat() if normalized_expiry else None,
            },
            principal,
        )
        return IssuedApiKey(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            raw_key=raw_key,
            scopes=tuple(normalized_scopes),
            portfolio_scope=normalized_paths,
            rate_limit_per_min=rate_limit_per_min,
            expires_at=normalized_expiry,
        )

    def rotate(
        self,
        principal: Principal,
        key_id: UUID,
        *,
        expected_version: int,
    ) -> IssuedApiKey:
        """Replace a key's secret material, invalidating the previous one."""
        self._require_principal(principal)
        row = self._get_or_not_found(key_id)
        if row.revoked_at is not None:
            raise Conflict(f"API key {row.name!r} is revoked and cannot be rotated.")
        self._check_version(row, expected_version)

        raw_key, prefix, key_hash = _generate_credential()
        row.key_hash = key_hash
        row.prefix = prefix
        self._touch(row, principal)
        self._flush_or_conflict("An API key with this credential already exists.")
        self._audit(
            "api_key_rotated",
            row,
            {"action": "rotated", "name": row.name, "prefix": row.prefix},
            principal,
        )
        return IssuedApiKey(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            raw_key=raw_key,
            scopes=tuple(row.scopes),
            portfolio_scope=tuple(row.portfolio_scope) if row.portfolio_scope is not None else None,
            rate_limit_per_min=row.rate_limit_per_min,
            expires_at=row.expires_at,
        )

    def revoke(
        self,
        principal: Principal,
        key_id: UUID,
        *,
        expected_version: int,
        reason: str,
    ) -> ApiKeyView:
        """Revoke a key immediately; a revoked key authenticates no request."""
        self._require_principal(principal)
        clean_reason = _required_text(reason, "api_key.revoke_reason", maximum=_REASON_MAX_LENGTH)
        row = self._get_or_not_found(key_id)
        self._check_version(row, expected_version)
        if row.revoked_at is None:
            row.revoked_at = self._now()
            self._touch(row, principal)
            self._audit(
                "api_key_revoked",
                row,
                {"action": "revoked", "name": row.name, "reason": clean_reason},
                principal,
            )
        return _view(row)

    def get(self, principal: Principal, key_id: UUID) -> ApiKeyView:
        """Return one key's non-secret administrative view."""
        self._require_principal(principal)
        return _view(self._get_or_not_found(key_id))

    def list_keys(self, principal: Principal) -> Sequence[ApiKeyView]:
        """Return every key's non-secret administrative view."""
        self._require_principal(principal)
        rows = self.session.execute(select(ApiKey).order_by(ApiKey.created_at)).scalars().all()
        return tuple(_view(row) for row in rows)

    # ---- internal invariants ---------------------------------------------

    def _require_principal(self, principal: Principal) -> None:
        authorize(principal, MANAGE_API_KEYS_PERMISSION)

    def _get_or_not_found(self, key_id: UUID) -> ApiKey:
        row = self.session.get(ApiKey, key_id)
        if row is None:
            raise NotFound(f"API key {key_id} was not found.")
        return row

    def _check_version(self, row: ApiKey, expected_version: int) -> None:
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError(
                "expected_version must be a positive integer.", field="expected_version"
            )
        if row.version != expected_version:
            raise Conflict(
                f"API key {row.name!r} changed since version {expected_version}: "
                f"the current version is {row.version}."
            )

    def _touch(self, row: ApiKey, principal: Principal) -> None:
        row.updated_at = self._now()
        row.updated_by_id = self._attributed_id(principal)
        row.version = row.version + 1

    def _audit(
        self,
        event_type: str,
        row: ApiKey,
        payload: Mapping[str, object],
        principal: Principal,
    ) -> None:
        self.audit.record(
            event_type,
            ("api_key", row.id),
            dict(payload),
            actor=principal.id,
            request_id=self.request_id,
        )

    def _flush_or_conflict(self, message: str) -> None:
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as error:
            raise Conflict(message) from error

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("ApiKeyService clock must return an aware datetime.")
        return now.astimezone(UTC)

    @staticmethod
    def _attributed_id(principal: Principal) -> UUID | None:
        return principal.id if principal.kind is PrincipalKind.USER else None


def _generate_credential() -> tuple[str, str, str]:
    raw_key = f"{_KEY_LABEL}_{secrets.token_urlsafe(_SECRET_BYTES)}"
    prefix = raw_key[:_PREFIX_LENGTH]
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return raw_key, prefix, key_hash


def _normalise_scopes(scopes: Sequence[Permission | str]) -> tuple[str, ...]:
    if isinstance(scopes, str) or not scopes:
        raise ValidationError("At least one scope is required.", field="api_key.scopes")
    try:
        normalized = sorted({coerce_permission(scope).value for scope in scopes})
    except (TypeError, ValueError) as error:
        raise ValidationError(
            f"Invalid permission scope: {error}.", field="api_key.scopes"
        ) from error
    return tuple(normalized)


def _normalise_portfolio_scope(paths: Sequence[str] | None) -> tuple[str, ...] | None:
    if paths is None:
        return None
    if not paths:
        raise ValidationError(
            "portfolio_scope must be omitted or a non-empty list of paths.",
            field="api_key.portfolio_scope",
        )
    # `Scope.from_paths` already validates and canonicalises path syntax; a
    # throwaway principal id is used purely to reuse that validation.
    return Scope.from_paths(new_id(), paths).paths


def _validate_rate_limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(
            "rate_limit_per_min must be a positive integer.", field="api_key.rate_limit_per_min"
        )


def _normalise_expiry(value: datetime | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(
            "expires_at must be a timezone-aware datetime.", field="api_key.expires_at"
        )
    normalized = value.astimezone(UTC)
    if normalized <= now:
        raise ValidationError("expires_at must be in the future.", field="api_key.expires_at")
    return normalized


def _required_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} is required.", field=field)
    clean = value.strip()
    if not clean:
        raise ValidationError(f"{field} is required.", field=field)
    if len(clean) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ValidationError(f"{field} contains an invalid control character.", field=field)
    return clean


def _view(row: ApiKey) -> ApiKeyView:
    return ApiKeyView(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=tuple(row.scopes),
        portfolio_scope=tuple(row.portfolio_scope) if row.portfolio_scope is not None else None,
        rate_limit_per_min=row.rate_limit_per_min,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        version=row.version,
    )


__all__ = [
    "MANAGE_API_KEYS_PERMISSION",
    "ApiKeyAuditWriter",
    "ApiKeyService",
    "ApiKeyView",
    "IssuedApiKey",
]
