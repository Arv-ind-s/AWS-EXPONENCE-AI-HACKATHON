"""External-identity mapping and just-in-time user provisioning.

The provider clients deliberately stop at a verified set of claims.  This
module is the policy boundary between those claims and an application user:
it normalises bounded values, maps roles and portfolio scope from deployment
configuration, and refuses to link an external identity to an existing user
by email.  That last rule prevents an identity provider account from taking
over a pre-existing local account merely because the email address matches.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from threading import RLock
from typing import Protocol
from uuid import UUID

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import Conflict, DomainError, ValidationError
from covenant_radar.core.ids import new_id


class IdentitySource(str, Enum):
    """Authentication sources persisted by ``app_user.auth_source``."""

    OIDC = "oidc"
    SAML = "saml"


class SSOError(DomainError):
    """A deliberate, safe failure in an external authentication flow."""

    code = "sso_error"

    def __init__(self, reason: str, *, message: str = "Single sign-on could not be completed."):
        super().__init__(message)
        self.reason = reason


class ProviderUnavailable(SSOError):
    """The configured identity provider could not be reached or trusted."""

    code = "sso_provider_unavailable"

    def __init__(self, reason: str):
        super().__init__(reason, message="Single sign-on is currently unavailable.")


class SecurityValidationError(SSOError):
    """The provider response failed a security validation."""

    code = "sso_security_validation_failed"


class AuditWriter(Protocol):
    """The C-60 append-only audit surface."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one audit event in the caller's transaction."""


class AdministratorNotifier(Protocol):
    """The small notification port needed for unsafe role mappings."""

    def notify(self, event_type: str, payload: Mapping[str, object]) -> object:
        """Raise an administrator notification."""


@dataclass(frozen=True, slots=True)
class AttributeMapping:
    """Deployment-controlled mapping from provider claims to user fields.

    ``roles`` and ``portfolio_scope`` may name several claim keys.  Values
    can be scalars or lists, which covers both OIDC JSON claims and SAML
    multi-valued attributes without provider-specific branching.
    """

    subject: str = "sub"
    email: str = "email"
    username: str = "preferred_username"
    full_name: str = "name"
    roles: tuple[str, ...] = ("roles", "groups")
    portfolio_scope: tuple[str, ...] = ("portfolio_scope", "portfolio")

    def __post_init__(self) -> None:
        fields = (self.subject, self.email, self.username, self.full_name)
        if any(not _valid_claim_key(value) for value in fields):
            raise ValueError("Identity attribute names must be non-empty dotted keys.")
        for group in (self.roles, self.portfolio_scope):
            if not group or any(not _valid_claim_key(value) for value in group):
                raise ValueError("Identity attribute lists must contain valid dotted keys.")

    def map(self, claims: Mapping[str, object], *, source: IdentitySource) -> MappedIdentity:
        """Map and validate one already cryptographically verified claim set."""
        subject = _required_text(_lookup(claims, self.subject), "subject", 255)
        email = _required_email(_lookup(claims, self.email))
        username_value = _lookup(claims, self.username)
        username = _optional_text(username_value, 64) or email.split("@", 1)[0]
        full_name = _optional_text(_lookup(claims, self.full_name), 200) or username
        roles = _multi_values(claims, self.roles, limit=20, max_length=50)
        portfolio_scope = _multi_values(claims, self.portfolio_scope, limit=100, max_length=255)
        return MappedIdentity(
            source=source,
            subject=subject,
            email=email,
            username=username,
            full_name=full_name,
            roles=roles,
            portfolio_scope=portfolio_scope,
            claims=dict(claims),
        )


@dataclass(frozen=True, slots=True)
class MappedIdentity:
    """Safe identity fields after attribute mapping and shape validation."""

    source: IdentitySource
    subject: str
    email: str
    username: str
    full_name: str
    roles: tuple[str, ...]
    portfolio_scope: tuple[str, ...]
    claims: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProvisioningSettings:
    """Role policy for just-in-time provisioning.

    The default role is required to be in the allow-list and explicitly
    forbidden from the administrative role set.  A deployment cannot
    accidentally turn an unknown provider role into administrator access.
    """

    allowed_roles: frozenset[str]
    default_role: str
    administrative_roles: frozenset[str] = frozenset(
        {"administrator", "admin", "platform_admin", "superuser"}
    )

    def __post_init__(self) -> None:
        roles = frozenset(_bounded_code(value, 50, "role") for value in self.allowed_roles)
        default = _bounded_code(self.default_role, 50, "default role")
        administrative = frozenset(
            _bounded_code(value, 50, "administrative role") for value in self.administrative_roles
        ) | frozenset({"administrator", "admin", "platform_admin", "superuser"})
        if not roles:
            raise ValueError("At least one provisionable role is required.")
        if default not in roles:
            raise ValueError("The JIT default role must be in allowed_roles.")
        if default in administrative:
            raise ValueError("The JIT default role must never be administrative.")
        object.__setattr__(self, "allowed_roles", roles)
        object.__setattr__(self, "default_role", default)
        object.__setattr__(self, "administrative_roles", administrative)


@dataclass(frozen=True, slots=True)
class ProvisionedIdentity:
    """Persistence-neutral identity used to issue a T-013 session."""

    id: UUID
    username: str
    email: str
    full_name: str
    auth_source: IdentitySource
    external_subject: str
    roles: tuple[str, ...]
    portfolio_scope: tuple[str, ...]
    is_active: bool = True
    created_at: datetime | None = None


class ProvisioningStore(Protocol):
    """Persistence port for external identities."""

    def find_by_external_subject(
        self, source: IdentitySource, subject: str
    ) -> ProvisionedIdentity | None:
        """Find a user by the immutable provider/source subject pair."""

    def find_by_email(self, email: str) -> ProvisionedIdentity | None:
        """Find an existing user by normalised email."""

    def create(self, user: ProvisionedIdentity) -> None:
        """Insert a newly provisioned user."""

    def save(self, user: ProvisionedIdentity) -> None:
        """Persist a mapped update to an existing external user."""


class InMemoryProvisioningStore:
    """Thread-safe store for offline tests and a single-process fixture."""

    def __init__(self) -> None:
        self._by_external: dict[tuple[IdentitySource, str], ProvisionedIdentity] = {}
        self._by_email: dict[str, ProvisionedIdentity] = {}
        self._by_username: dict[str, ProvisionedIdentity] = {}
        self._lock = RLock()

    def find_by_external_subject(
        self, source: IdentitySource, subject: str
    ) -> ProvisionedIdentity | None:
        with self._lock:
            return self._by_external.get((source, subject))

    def find_by_email(self, email: str) -> ProvisionedIdentity | None:
        with self._lock:
            return self._by_email.get(email)

    def create(self, user: ProvisionedIdentity) -> None:
        with self._lock:
            external_key = (user.auth_source, user.external_subject)
            if external_key in self._by_external:
                raise Conflict("The external identity is already provisioned.")
            if user.email in self._by_email:
                raise Conflict("The email address is already associated with an account.")
            if user.username in self._by_username:
                raise Conflict("The username is already associated with an account.")
            self._by_external[external_key] = user
            self._by_email[user.email] = user
            self._by_username[user.username] = user

    def save(self, user: ProvisionedIdentity) -> None:
        with self._lock:
            external_key = (user.auth_source, user.external_subject)
            previous = self._by_external.get(external_key)
            if previous is None:
                raise KeyError("The external identity does not exist.")
            if user.email != previous.email:
                existing = self._by_email.get(user.email)
                if existing is not None and existing.id != user.id:
                    raise Conflict("The email address is already associated with an account.")
                self._by_email.pop(previous.email, None)
            if user.username != previous.username:
                existing = self._by_username.get(user.username)
                if existing is not None and existing.id != user.id:
                    raise Conflict("The username is already associated with an account.")
                self._by_username.pop(previous.username, None)
            self._by_external[external_key] = user
            self._by_email[user.email] = user
            self._by_username[user.username] = user

    def users(self) -> tuple[ProvisionedIdentity, ...]:
        """Return a stable snapshot for diagnostics and focused tests."""
        with self._lock:
            return tuple(self._by_external.values())


class ProvisioningService:
    """Map provider claims, apply role policy, and provision safely."""

    def __init__(
        self,
        store: ProvisioningStore,
        *,
        mapping: AttributeMapping | None = None,
        settings: ProvisioningSettings,
        clock: Clock | None = None,
        audit: AuditWriter | None = None,
        notifier: AdministratorNotifier
        | Callable[[str, Mapping[str, object]], object]
        | None = None,
        request_id: str | None = None,
    ) -> None:
        self.store = store
        self.mapping = mapping or AttributeMapping()
        self.settings = settings
        self.clock = clock or SystemClock()
        self.audit = audit
        self.notifier = notifier
        self.request_id = request_id or get_request_id() or new_request_id()

    def provision(
        self, claims: Mapping[str, object], *, source: IdentitySource | str
    ) -> ProvisionedIdentity:
        """Return an existing mapped user or create one just in time."""
        try:
            identity_source = IdentitySource(source)
        except (TypeError, ValueError) as error:
            self._audit_unmapped("unknown", claims, "source_invalid")
            raise SSOError("source_invalid") from error
        try:
            mapped = self.mapping.map(claims, source=identity_source)
            safe_roles, unknown_roles = self._roles(mapped.roles)
        except (DomainError, TypeError, ValueError) as error:
            self._audit_unmapped(identity_source.value, claims, "attribute_mapping_invalid")
            raise SSOError("attribute_mapping_invalid") from error
        if unknown_roles:
            safe_roles = (self.settings.default_role,)
            self._notify_unknown_role(mapped, unknown_roles)
        elif not safe_roles:
            safe_roles = (self.settings.default_role,)

        existing = self.store.find_by_external_subject(identity_source, mapped.subject)
        now = _utc(self.clock.now())
        if existing is not None:
            if not existing.is_active:
                self._audit(
                    "authentication_sso_failed",
                    mapped,
                    {"outcome": "failed", "reason": "account_inactive"},
                )
                raise SSOError("account_inactive")
            email_owner = self.store.find_by_email(mapped.email)
            if email_owner is not None and email_owner.id != existing.id:
                self._audit(
                    "authentication_sso_failed",
                    mapped,
                    {"outcome": "failed", "reason": "email_link_refused"},
                )
                raise SSOError("email_link_refused")
            updated = replace(
                existing,
                username=mapped.username,
                email=mapped.email,
                full_name=mapped.full_name,
                roles=safe_roles,
                portfolio_scope=mapped.portfolio_scope,
            )
            self.store.save(updated)
            self._audit(
                "authentication_sso_provisioned",
                mapped,
                {"outcome": "existing", "user_id": str(updated.id)},
            )
            return updated

        # Never link by email.  A matching local account needs an explicit
        # administrator-controlled migration, outside the sign-in path.
        if self.store.find_by_email(mapped.email) is not None:
            self._audit(
                "authentication_sso_failed",
                mapped,
                {"outcome": "failed", "reason": "email_link_refused"},
            )
            raise SSOError("email_link_refused")

        user = ProvisionedIdentity(
            id=new_id(),
            username=mapped.username,
            email=mapped.email,
            full_name=mapped.full_name,
            auth_source=identity_source,
            external_subject=mapped.subject,
            roles=safe_roles,
            portfolio_scope=mapped.portfolio_scope,
            created_at=now,
        )
        self.store.create(user)
        self._audit(
            "authentication_sso_provisioned",
            mapped,
            {"outcome": "created", "user_id": str(user.id)},
        )
        return user

    def _roles(self, roles: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        clean = tuple(dict.fromkeys(_bounded_code(value, 50, "role") for value in roles))
        known = tuple(value for value in clean if value in self.settings.allowed_roles)
        unknown = tuple(value for value in clean if value not in self.settings.allowed_roles)
        return known, unknown

    def _notify_unknown_role(self, mapped: MappedIdentity, unknown_roles: tuple[str, ...]) -> None:
        payload = {
            "source": mapped.source.value,
            "subject_reference": _subject_reference(mapped.source, mapped.subject),
            "unknown_roles": unknown_roles,
            "default_role": self.settings.default_role,
        }
        self._audit("authentication_sso_unknown_role_defaulted", mapped, payload)
        if self.notifier is None:
            return
        try:
            if hasattr(self.notifier, "notify"):
                self.notifier.notify("authentication_sso_unknown_role", payload)
            else:
                self.notifier("authentication_sso_unknown_role", payload)
        except Exception as error:
            self._audit(
                "authentication_sso_notification_failed",
                mapped,
                {"source": mapped.source.value, "error_type": type(error).__name__},
            )

    def _audit(
        self, event_type: str, mapped: MappedIdentity, payload: Mapping[str, object]
    ) -> None:
        if self.audit is None:
            return
        safe_payload = dict(payload)
        safe_payload.setdefault(
            "subject_reference", _subject_reference(mapped.source, mapped.subject)
        )
        self.audit.record(
            event_type,
            ("external_identity", _subject_reference(mapped.source, mapped.subject)),
            safe_payload,
            actor=None,
            request_id=self.request_id,
        )

    def _audit_unmapped(self, source: str, claims: Mapping[str, object], reason: str) -> None:
        if self.audit is None:
            return
        subject = claims.get("sub")
        subject_value = subject if isinstance(subject, str) else "unknown"
        source_value = source if source in {item.value for item in IdentitySource} else "unknown"
        reference = hmac.new(
            b"covenant-radar-sso-audit-v1",
            f"{source_value}:{subject_value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.audit.record(
            "authentication_sso_failed",
            ("external_identity", reference),
            {"outcome": "failed", "reason": reason, "subject_reference": reference},
            actor=None,
            request_id=self.request_id,
        )


def _valid_claim_key(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", value))


def _lookup(claims: Mapping[str, object], key: str) -> object | None:
    current: object = claims
    for component in key.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(component)
    return current


def _required_text(value: object | None, field: str, max_length: int) -> str:
    result = _optional_text(value, max_length)
    if not result:
        raise ValidationError(f"Mapped identity {field} is required.", field=field)
    return result


def _optional_text(value: object | None, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if (
        not value
        or len(value) > max_length
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return ""
    return value


def _required_email(value: object | None) -> str:
    email = _optional_text(value, 254).casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValidationError("Mapped identity email is invalid.", field="email")
    return email


def _multi_values(
    claims: Mapping[str, object], keys: Sequence[str], *, limit: int, max_length: int
) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        value = _lookup(claims, key)
        candidates = value if isinstance(value, list | tuple | set) else (value,)
        for candidate in candidates:
            clean = _optional_text(candidate, max_length)
            if clean and clean not in values:
                values.append(clean)
            if len(values) >= limit:
                return tuple(values)
    return tuple(values)


def _bounded_code(value: str, max_length: int, label: str) -> str:
    clean = _optional_text(value, max_length)
    if not clean:
        raise ValueError(f"{label.capitalize()} must be a non-empty safe code.")
    return clean


def _subject_reference(source: IdentitySource, subject: str) -> str:
    return hmac.new(
        b"covenant-radar-sso-audit-v1",
        f"{source.value}:{subject}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Authentication timestamps must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "AdministratorNotifier",
    "AttributeMapping",
    "AuditWriter",
    "IdentitySource",
    "InMemoryProvisioningStore",
    "MappedIdentity",
    "ProviderUnavailable",
    "ProvisionedIdentity",
    "ProvisioningService",
    "ProvisioningSettings",
    "ProvisioningStore",
    "SSOError",
    "SecurityValidationError",
]
