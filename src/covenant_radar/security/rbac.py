"""Persistence-neutral role and permission resolution.

This module owns the authorization decision, but not the database session or
the HTTP response.  Adapters supply role assignments through a small lookup
callable/protocol; the result is normalized to the closed :class:`Permission`
enum and cached per principal.  Role changes explicitly invalidate the
affected cache entry (or the complete cache for a role-definition change).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from threading import RLock
from typing import Final, Protocol, cast
from uuid import UUID

from covenant_radar.core.errors import AuthorizationError
from covenant_radar.security.permissions import Permission, coerce_permission

type PermissionLike = Permission | str
type UserPermissionLookup = Callable[[UUID], Iterable[PermissionLike]]
RolePermissions = Mapping[str, Iterable[PermissionLike]]


class PermissionConfigurationError(RuntimeError):
    """Raised when authorization configuration contains an unknown value."""


class RolePermissionSource(Protocol):
    """The persistence boundary needed by :class:`RolePermissionResolver`."""

    def permissions_for_user(self, user_id: UUID) -> Iterable[PermissionLike]:
        """Return the permissions granted through the user's current roles."""

    def permissions_by_role(self) -> RolePermissions:
        """Return all role-to-permission assignments for startup validation."""


class PrincipalKind(str, Enum):
    """The credential families that can establish a principal."""

    USER = "user"
    API_KEY = "api_key"


# A persona session may never carry these, whatever the persona's role
# grants.  ``ASSUME_PERSONA`` would let one impersonation start another, so
# the audit trail would name an acting admin who is themselves a persona.
# ``MANAGE_USERS`` would let a persona session edit the very role assignments
# that decide what a persona may do.  Both are stripped structurally rather
# than by configuration, so no seed file or role edit can reintroduce them.
PERSONA_FORBIDDEN_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {Permission.ASSUME_PERSONA, Permission.MANAGE_USERS}
)


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller with already-resolved permissions.

    ``permissions`` is immutable so a request cannot mutate its own authority
    after the dependency has made its decision.  API-key scopes are retained
    separately for audit and diagnostics; they are also represented in
    ``permissions`` after strict normalization.

    A principal may additionally be *impersonated*: ``id`` is then the persona
    being acted as, while ``acting_admin_id`` names the human who started the
    session.  Authorization reads ``id``; audit and separation-of-duties read
    :attr:`human_id`.  Keeping both on one object means no call site can
    authorize against the persona and then attribute the act to the persona
    as well, which is the failure mode impersonation exists to avoid.
    """

    id: UUID
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    kind: PrincipalKind = PrincipalKind.USER
    scopes: frozenset[Permission] = field(default_factory=frozenset)
    acting_admin_id: UUID | None = None
    persona_code: str | None = None
    persona_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        try:
            kind = self.kind if isinstance(self.kind, PrincipalKind) else PrincipalKind(self.kind)
        except ValueError as error:
            raise ValueError(f"Unknown principal kind: {self.kind!r}.") from error
        permissions = _normalize_permissions(self.permissions)
        scopes = _normalize_permissions(self.scopes)
        if kind is PrincipalKind.API_KEY:
            # API-key authority is exactly the key's scopes.  If a caller
            # supplies a broader permissions set, discard it rather than
            # allowing an accidental grant outside the credential scope.
            permissions = scopes
        elif scopes:
            raise ValueError("Only API-key principals may carry scopes.")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "permissions", permissions)
        object.__setattr__(self, "scopes", scopes)
        self._validate_persona(kind, permissions)

    def _validate_persona(self, kind: PrincipalKind, permissions: frozenset[Permission]) -> None:
        """Enforce the persona invariants, or refuse to build the principal.

        These are checked in the constructor rather than at the switch route
        because a `Principal` is rebuilt from the session on every request.
        A check that only ran at switch time would be bypassed by the next
        request, which is exactly when it matters.
        """
        persona_fields = (self.acting_admin_id, self.persona_code, self.persona_expires_at)
        if all(value is None for value in persona_fields):
            return
        if any(value is None for value in persona_fields):
            raise ValueError(
                "An impersonated principal requires acting_admin_id, persona_code and "
                "persona_expires_at together."
            )
        if kind is not PrincipalKind.USER:
            raise ValueError("Only a session-user principal may be impersonated.")
        if not isinstance(self.acting_admin_id, UUID):
            raise TypeError("acting_admin_id must be a UUID.")
        if self.acting_admin_id == self.id:
            raise ValueError("An administrator may not assume their own identity as a persona.")
        if not isinstance(self.persona_code, str) or not self.persona_code.strip():
            raise ValueError("persona_code must be non-empty text.")
        expires_at = self.persona_expires_at
        if not isinstance(expires_at, datetime):
            raise TypeError("persona_expires_at must be a datetime.")
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("persona_expires_at must be timezone-aware.")
        forbidden = permissions & PERSONA_FORBIDDEN_PERMISSIONS
        if forbidden:
            codes = ", ".join(sorted(permission.value for permission in forbidden))
            raise ValueError(f"A persona session may not hold: {codes}.")
        object.__setattr__(self, "persona_code", self.persona_code.strip())
        object.__setattr__(self, "persona_expires_at", expires_at.astimezone(UTC))

    @classmethod
    def user(cls, user_id: UUID, permissions: Iterable[PermissionLike]) -> Principal:
        """Construct a session-user principal."""
        return cls(id=user_id, permissions=_normalize_permissions(permissions))

    @classmethod
    def impersonated(
        cls,
        persona_user_id: UUID,
        permissions: Iterable[PermissionLike],
        *,
        acting_admin_id: UUID,
        persona_code: str,
        expires_at: datetime,
    ) -> Principal:
        """Construct a persona principal acted by a named administrator.

        ``permissions`` are the persona's own role grants, minus the two the
        persona may never hold.  They are *not* unioned with the acting
        administrator's: a switch narrows to the persona's authority, so the
        session can demonstrate a role the admin lacks without ever becoming
        the sum of every role it has visited.
        """
        granted = _normalize_permissions(permissions) - PERSONA_FORBIDDEN_PERMISSIONS
        return cls(
            id=persona_user_id,
            permissions=granted,
            acting_admin_id=acting_admin_id,
            persona_code=persona_code,
            persona_expires_at=expires_at,
        )

    @property
    def is_impersonating(self) -> bool:
        """Whether this principal is an administrator acting as a persona."""
        return self.acting_admin_id is not None

    @property
    def human_id(self) -> UUID:
        """The identifier of the human responsible for this request.

        For an ordinary session this is :attr:`id`.  For a persona session it
        is the administrator who started it.  Audit attribution and the
        distinct-actor rule both read this, never :attr:`id`.
        """
        return self.acting_admin_id if self.acting_admin_id is not None else self.id

    def persona_has_expired(self, now: datetime) -> bool:
        """Whether a persona session has passed its expiry at ``now``."""
        if self.persona_expires_at is None:
            return False
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Persona expiry comparison requires a timezone-aware instant.")
        return now >= self.persona_expires_at

    @classmethod
    def api_key(cls, key_id: UUID, scopes: Iterable[PermissionLike]) -> Principal:
        """Construct an API-key principal from its explicitly scoped grants."""
        normalized = _normalize_permissions(scopes)
        return cls(
            id=key_id,
            permissions=normalized,
            kind=PrincipalKind.API_KEY,
            scopes=normalized,
        )

    @property
    def is_api_key(self) -> bool:
        """Whether the principal was established by an API key."""
        return self.kind is PrincipalKind.API_KEY

    def has(self, permission: PermissionLike) -> bool:
        """Return whether this principal holds ``permission``."""
        return coerce_permission(permission) in self.permissions

    def has_permission(self, permission: PermissionLike) -> bool:
        """Named alias for :meth:`has` used by dependency call sites."""
        return self.has(permission)


@dataclass(frozen=True, slots=True)
class PermissionReachabilityReport:
    """The result of checking whether every enum permission is usable."""

    unreachable: tuple[Permission, ...]
    role_count: int

    @property
    def ok(self) -> bool:
        """Whether every declared permission is granted to at least one role."""
        return not self.unreachable

    @property
    def message(self) -> str:
        """A stable operator-facing diagnostic."""
        if self.ok:
            return f"All {len(Permission)} permissions are reachable from {self.role_count} roles."
        codes = ", ".join(permission.value for permission in self.unreachable)
        return f"Unreachable permissions: {codes}."


class RolePermissionResolver:
    """Resolve and cache a user's effective permissions.

    ``source`` may be a :class:`RolePermissionSource` or a plain callable,
    which keeps unit tests and non-SQL adapters small.  Cache invalidation is
    explicit because role changes are state transitions and must take effect
    before a subsequent request is authorized.
    """

    def __init__(
        self,
        source: RolePermissionSource | UserPermissionLookup,
        *,
        role_permissions: Callable[[], RolePermissions] | RolePermissions | None = None,
    ) -> None:
        self._source = source
        role_lookup: Callable[[], RolePermissions] | None
        if callable(role_permissions):
            role_lookup = role_permissions
        elif role_permissions is not None:
            snapshot = dict(role_permissions)

            def snapshot_lookup() -> RolePermissions:
                return snapshot

            role_lookup = snapshot_lookup
        else:
            source_role_lookup = getattr(source, "permissions_by_role", None)
            role_lookup = (
                cast(Callable[[], RolePermissions], source_role_lookup)
                if callable(source_role_lookup)
                else None
            )
        self._role_permissions = role_lookup
        self._cache: dict[UUID, frozenset[Permission]] = {}
        self._lock = RLock()
        self._lookup_count = 0
        self._global_generation = 0
        self._user_generations: dict[UUID, int] = {}

    def permissions_for(self, user_id: UUID) -> frozenset[Permission]:
        """Return the user's effective permissions, using the local cache."""
        while True:
            with self._lock:
                cached = self._cache.get(user_id)
                generation = (self._global_generation, self._user_generations.get(user_id, 0))
            if cached is not None:
                return cached

            permissions = _normalize_permissions(self._lookup_user_permissions(user_id))
            with self._lock:
                # A role change can happen while the adapter is being read.
                # Do not repopulate the cache with a result from before that
                # change; retry against the source under the new generation.
                current_generation = (
                    self._global_generation,
                    self._user_generations.get(user_id, 0),
                )
                if generation != current_generation:
                    continue
                existing = self._cache.get(user_id)
                if existing is not None:
                    return existing
                self._cache[user_id] = permissions
                return permissions

    def permissions_for_user(self, user_id: UUID) -> frozenset[Permission]:
        """Protocol-friendly alias for :meth:`permissions_for`."""
        return self.permissions_for(user_id)

    def principal_for_user(self, user_id: UUID) -> Principal:
        """Build a session principal from the effective role permissions."""
        return Principal.user(user_id, self.permissions_for(user_id))

    def invalidate(self, user_id: UUID | None = None) -> None:
        """Invalidate one user's cache entry or every entry."""
        with self._lock:
            if user_id is None:
                self._global_generation += 1
                self._cache.clear()
            else:
                self._user_generations[user_id] = self._user_generations.get(user_id, 0) + 1
                self._cache.pop(user_id, None)

    def invalidate_user(self, user_id: UUID) -> None:
        """Invalidate permissions after a user's role assignment changes."""
        self.invalidate(user_id)

    def role_changed(self, user_id: UUID | None = None) -> None:
        """Invalidate after a role grant, revoke, or role-definition change.

        A supplied user id is the efficient path for a user-role change.  A
        missing id represents a role-permission definition change and clears
        all cached principals because the affected users are not knowable at
        this boundary.
        """
        self.invalidate(user_id)

    @property
    def lookup_count(self) -> int:
        """Expose a read-only diagnostic useful for proving cache behavior."""
        with self._lock:
            return self._lookup_count

    def permission_reachability(self) -> PermissionReachabilityReport:
        """Check the configured role catalogue, if this source exposes it."""
        if self._role_permissions is None:
            raise PermissionConfigurationError(
                "Role permission source does not expose role assignments for startup validation."
            )
        return permission_reachability(self._role_permissions())

    def _lookup_user_permissions(self, user_id: UUID) -> Iterable[PermissionLike]:
        with self._lock:
            self._lookup_count += 1
        source_user_lookup = getattr(self._source, "permissions_for_user", None)
        if callable(source_user_lookup):
            lookup = cast(Callable[[UUID], Iterable[PermissionLike]], source_user_lookup)
            return lookup(user_id)
        lookup = cast(UserPermissionLookup, self._source)
        return lookup(user_id)


def authorize(principal: Principal, permission: PermissionLike) -> None:
    """Raise the canonical authorization error when a grant is absent."""
    normalized = coerce_permission(permission)
    if not principal.has(normalized):
        raise AuthorizationError(f"Missing permission: {normalized.value}.", field="permission")


def permission_reachability(role_permissions: RolePermissions) -> PermissionReachabilityReport:
    """Return all declared permissions that no role currently grants."""
    granted: set[Permission] = set()
    for role_code, values in role_permissions.items():
        try:
            granted.update(_normalize_permissions(values))
        except (TypeError, ValueError) as error:
            raise PermissionConfigurationError(
                f"Role {role_code!r} contains an invalid permission: {error}"
            ) from error
    unreachable = tuple(permission for permission in Permission if permission not in granted)
    return PermissionReachabilityReport(unreachable=unreachable, role_count=len(role_permissions))


def check_unreachable_permissions(
    role_permissions: RolePermissions,
) -> PermissionReachabilityReport:
    """Compatibility-facing name for the startup reachability check."""
    return permission_reachability(role_permissions)


def ensure_permissions_reachable(role_permissions: RolePermissions) -> PermissionReachabilityReport:
    """Validate reachability and raise an actionable startup error on failure."""
    report = permission_reachability(role_permissions)
    if not report.ok:
        raise PermissionConfigurationError(report.message)
    return report


def _normalize_permissions(values: Iterable[PermissionLike]) -> frozenset[Permission]:
    try:
        return frozenset(coerce_permission(value) for value in values)
    except (TypeError, ValueError) as error:
        raise PermissionConfigurationError(f"Invalid permission configuration: {error}") from error


class InMemoryRolePermissionSource:
    """Thread-safe reference source for local development and tests."""

    def __init__(
        self,
        *,
        roles: RolePermissions | None = None,
        user_roles: Mapping[UUID, Iterable[str]] | None = None,
    ) -> None:
        self._roles: dict[str, frozenset[Permission]] = {
            code: _normalize_permissions(values) for code, values in (roles or {}).items()
        }
        self._user_roles: dict[UUID, tuple[str, ...]] = {
            user_id: tuple(role_codes) for user_id, role_codes in (user_roles or {}).items()
        }
        self._lock = RLock()

    def permissions_for_user(self, user_id: UUID) -> frozenset[Permission]:
        with self._lock:
            role_codes = self._user_roles.get(user_id, ())
            return frozenset(
                permission
                for role_code in role_codes
                for permission in self._roles.get(role_code, frozenset())
            )

    def permissions_by_role(self) -> RolePermissions:
        with self._lock:
            return {code: tuple(values) for code, values in self._roles.items()}

    def set_user_roles(self, user_id: UUID, role_codes: Iterable[str]) -> None:
        with self._lock:
            self._user_roles[user_id] = tuple(role_codes)

    def set_role_permissions(self, role_code: str, permissions: Iterable[PermissionLike]) -> None:
        with self._lock:
            self._roles[role_code] = _normalize_permissions(permissions)


# A stable event name for all authorization refusals.  It is deliberately
# independent of HTTP so API and web callers produce the same audit shape.
AUTHORIZATION_DENIED_EVENT: Final[str] = "authorization_denied"


__all__ = [
    "AUTHORIZATION_DENIED_EVENT",
    "PERSONA_FORBIDDEN_PERMISSIONS",
    "InMemoryRolePermissionSource",
    "PermissionConfigurationError",
    "PermissionReachabilityReport",
    "Principal",
    "PrincipalKind",
    "RolePermissionResolver",
    "RolePermissionSource",
    "PermissionLike",
    "RolePermissions",
    "UserPermissionLookup",
    "authorize",
    "check_unreachable_permissions",
    "ensure_permissions_reachable",
    "permission_reachability",
]
