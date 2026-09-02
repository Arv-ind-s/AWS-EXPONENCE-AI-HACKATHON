"""Portfolio scope resolution and ownership-path composition.

Portfolio scope is an authorization input, not a presentation concern.  This
module turns the authenticated principal's persisted grants into an immutable
value object and supplies the SQL ownership paths used by repositories.  The
repository layer can therefore apply one predicate before it executes any
read, including reads of records whose portfolio is reached through one or
more foreign-key joins.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Select, false, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from covenant_radar.core.errors import AuthorizationError, ExternalServiceError
from covenant_radar.security.rbac import Principal, PrincipalKind

_PATH_MAX_LENGTH: Final[int] = 660
_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9.:-]*(/[A-Za-z0-9][A-Za-z0-9.:-]*)*/"
)

AUDITOR_ROLE_CODE: Final[str] = "auditor"


class ScopeConfigurationError(RuntimeError):
    """Raised when a model has no safe, known portfolio ownership path."""


class ScopeAuditError(ExternalServiceError):
    """Raised when a permitted unscoped read cannot be audited."""


class UnscopedCaller(str, Enum):
    """Marker values for the two deliberately permitted unscoped callers.

    The class is a string subtype for stable audit payloads while its two
    instances below remain the only values accepted by the repository base.
    Raw request data must never be passed to an unscoped repository method.
    """

    AUDITOR = "auditor"
    RETENTION_JOB = "retention_job"


AUDITOR_CALLER: Final[UnscopedCaller] = UnscopedCaller.AUDITOR
RETENTION_JOB_CALLER: Final[UnscopedCaller] = UnscopedCaller.RETENTION_JOB


def _normalise_path(value: str) -> str:
    """Validate and canonicalise one materialised portfolio path.

    A trailing slash is part of the path format.  It is added for convenient
    API-key configuration, then the complete value is checked so a caller
    cannot inject SQL ``LIKE`` wildcards into a scope predicate.
    """
    if not isinstance(value, str):
        raise TypeError("Portfolio scope paths must be strings.")
    path = value if value.endswith("/") else f"{value}/"
    if len(path) > _PATH_MAX_LENGTH or not _PATH_PATTERN.fullmatch(path):
        raise ScopeConfigurationError(f"Invalid portfolio scope path: {value!r}.")
    return path


def _normalise_paths(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError("Portfolio scope paths must be an iterable of strings, not one string.")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = _normalise_path(value)
        if path not in seen:
            result.append(path)
            seen.add(path)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Scope:
    """The immutable portfolio authority for one request or job.

    ``exact_paths`` grants one portfolio.  ``descendant_paths`` grants the
    portfolio and all its descendants.  Keeping those sets separate avoids
    accidentally broadening a grant whose ``include_descendants`` flag is
    false.  Both are empty for a principal with no scope, and an empty scope
    always compiles to a false SQL predicate.
    """

    principal_id: UUID
    exact_paths: tuple[str, ...] = ()
    descendant_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.principal_id, UUID):
            raise TypeError("Scope principal_id must be a UUID.")
        object.__setattr__(self, "exact_paths", _normalise_paths(self.exact_paths))
        object.__setattr__(self, "descendant_paths", _normalise_paths(self.descendant_paths))

    @classmethod
    def empty(cls, principal_id: UUID) -> Scope:
        """Return a scope that can access no portfolio."""
        return cls(principal_id=principal_id)

    @classmethod
    def from_principal(cls, principal: Principal, session: Session) -> Scope:
        """Resolve a principal's scope at the request boundary."""
        return ScopeResolver(session).resolve(principal)

    @classmethod
    def from_paths(
        cls,
        principal_id: UUID,
        paths: Iterable[str],
        *,
        include_descendants: bool = True,
    ) -> Scope:
        """Build a scope from path prefixes, normally for an API key.

        API-key portfolio scopes are stored as prefixes and therefore use the
        same descendant semantics as a user grant with
        ``include_descendants=True``.
        """
        normalised = _normalise_paths(paths)
        if include_descendants:
            return cls(principal_id=principal_id, descendant_paths=normalised)
        return cls(principal_id=principal_id, exact_paths=normalised)

    @property
    def paths(self) -> tuple[str, ...]:
        """Return all configured paths in stable insertion order."""
        return self.exact_paths + tuple(
            path for path in self.descendant_paths if path not in self.exact_paths
        )

    @property
    def portfolio_paths(self) -> tuple[str, ...]:
        """Compatibility-facing name used by scope-aware callers."""
        return self.paths

    @property
    def is_empty(self) -> bool:
        """Whether this scope grants no portfolio rows."""
        return not self.exact_paths and not self.descendant_paths

    def predicate(self, path_column: Any) -> ColumnElement[bool]:
        """Return the parameterised SQL predicate for ``path_column``.

        Paths have already been validated, and SQLAlchemy still binds every
        value as a parameter.  The trailing slash in each prefix preserves
        portfolio-segment boundaries during ``LIKE`` matching.
        """
        clauses: list[ColumnElement[bool]] = []
        if self.exact_paths:
            clauses.append(path_column.in_(self.exact_paths))
        clauses.extend(path_column.like(f"{path}%", escape="\\") for path in self.descendant_paths)
        return or_(*clauses) if clauses else false()


class ScopeResolver:
    """Resolve and cache one principal's scope for a request lifetime.

    Instantiate this resolver at the request or job boundary.  Its cache is
    intentionally local to that resolver, so a role or scope change cannot
    leak a stale authority into a later request.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._cache: dict[tuple[PrincipalKind, UUID], Scope] = {}

    def resolve(self, principal: Principal) -> Scope:
        """Return the immutable scope for ``principal``, querying once."""
        if not isinstance(principal, Principal):
            raise TypeError("Scope resolution requires an authenticated Principal.")
        key = (principal.kind, principal.id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        if principal.kind is PrincipalKind.USER:
            scope = self._resolve_user(principal.id)
        elif principal.kind is PrincipalKind.API_KEY:
            scope = self._resolve_api_key(principal.id)
        else:
            raise ScopeConfigurationError(f"Unsupported principal kind: {principal.kind!r}.")

        self._cache[key] = scope
        return scope

    def _resolve_user(self, user_id: UUID) -> Scope:
        from covenant_radar.db.models.identity import UserPortfolioScope
        from covenant_radar.db.models.portfolio import Portfolio

        rows = self._session.execute(
            select(Portfolio.path, UserPortfolioScope.include_descendants)
            .join(Portfolio, Portfolio.id == UserPortfolioScope.portfolio_id)
            .where(UserPortfolioScope.user_id == user_id)
        ).all()
        exact: list[str] = []
        descendants: list[str] = []
        for path, include_descendants in rows:
            normalised = _normalise_path(path)
            (descendants if include_descendants else exact).append(normalised)
        return Scope(
            principal_id=user_id,
            exact_paths=tuple(exact),
            descendant_paths=tuple(descendants),
        )

    def _resolve_api_key(self, key_id: UUID) -> Scope:
        from covenant_radar.db.models.identity import ApiKey

        value = self._session.scalar(select(ApiKey.portfolio_scope).where(ApiKey.id == key_id))
        if value is None:
            return Scope.empty(key_id)
        if not isinstance(value, list | tuple):
            raise ScopeConfigurationError(
                f"API key {key_id} has a malformed portfolio_scope; access is refused."
            )
        return Scope.from_paths(key_id, value)


def resolve_scope(principal: Principal, session: Session) -> Scope:
    """Resolve one scope without retaining it beyond the current caller."""
    return ScopeResolver(session).resolve(principal)


scope_for_principal = resolve_scope


@dataclass(frozen=True, slots=True)
class OwnershipPath:
    """A SQLAlchemy join chain ending at ``portfolio.path``."""

    path_column: Any
    joins: tuple[tuple[type[Any], ColumnElement[bool]], ...] = ()

    def apply(self, statement: Select[Any]) -> Select[Any]:
        """Compose the ownership joins into a select statement."""
        for target, on_clause in self.joins:
            statement = statement.join(target, on_clause)
        return statement


def _ownership_chain(model: type[Any], edges: tuple[tuple[type[Any], str], ...]) -> OwnershipPath:
    from covenant_radar.db.models.portfolio import Portfolio

    current = model
    joins: list[tuple[type[Any], ColumnElement[bool]]] = []
    for target, foreign_key_name in edges:
        foreign_key = getattr(current, foreign_key_name, None)
        if foreign_key is None:
            raise ScopeConfigurationError(
                f"{model.__name__} does not expose required ownership field "
                f"{current.__name__}.{foreign_key_name}."
            )
        joins.append((target, foreign_key == target.id))
        current = target
    return OwnershipPath(path_column=Portfolio.path, joins=tuple(joins))


def ownership_path_for(model: type[Any]) -> OwnershipPath:
    """Return the registered, fail-closed portfolio path for ``model``.

    The ordering follows the most direct ownership relation.  Polymorphic
    records with no unambiguous foreign-key route are intentionally rejected;
    their repository must supply an explicit ``OwnershipPath`` instead of
    guessing from an arbitrary subject id.
    """
    from covenant_radar.db.models.portfolio import Portfolio

    if model is Portfolio:
        return OwnershipPath(path_column=Portfolio.path)

    # The direct relationship is the safest and also covers Borrower.
    if hasattr(model, "portfolio_id"):
        return _ownership_chain(model, ((Portfolio, "portfolio_id"),))

    from covenant_radar.db.models.borrower import Borrower
    from covenant_radar.db.models.covenant import Covenant, CovenantVersion
    from covenant_radar.db.models.document import Document
    from covenant_radar.db.models.facility import Facility
    from covenant_radar.db.models.forecast import Forecast
    from covenant_radar.db.models.signal import EvidenceItem
    from covenant_radar.db.models.workflow import Case, Memo

    if hasattr(model, "borrower_id"):
        return _ownership_chain(model, ((Borrower, "borrower_id"), (Portfolio, "portfolio_id")))
    if hasattr(model, "facility_id"):
        return _ownership_chain(
            model,
            (
                (Facility, "facility_id"),
                (Borrower, "borrower_id"),
                (Portfolio, "portfolio_id"),
            ),
        )
    if hasattr(model, "covenant_version_id"):
        return _ownership_chain(
            model,
            (
                (CovenantVersion, "covenant_version_id"),
                (Covenant, "covenant_id"),
                (Facility, "facility_id"),
                (Borrower, "borrower_id"),
                (Portfolio, "portfolio_id"),
            ),
        )
    if hasattr(model, "covenant_id"):
        return _ownership_chain(
            model,
            (
                (Covenant, "covenant_id"),
                (Facility, "facility_id"),
                (Borrower, "borrower_id"),
                (Portfolio, "portfolio_id"),
            ),
        )
    if hasattr(model, "document_id"):
        return _ownership_chain(
            model,
            ((Document, "document_id"), (Borrower, "borrower_id"), (Portfolio, "portfolio_id")),
        )
    # ForecastDriver carries both ``forecast_id`` and an optional
    # ``evidence_id``.  The forecast is its authoritative owner; checking the
    # optional evidence relation first would hide drivers that are correctly
    # persisted without a source evidence link.
    if hasattr(model, "forecast_id"):
        return _ownership_chain(
            model,
            (
                (Forecast, "forecast_id"),
                (CovenantVersion, "covenant_version_id"),
                (Covenant, "covenant_id"),
                (Facility, "facility_id"),
                (Borrower, "borrower_id"),
                (Portfolio, "portfolio_id"),
            ),
        )
    if hasattr(model, "evidence_id"):
        return _ownership_chain(
            model,
            ((EvidenceItem, "evidence_id"), (Borrower, "borrower_id"), (Portfolio, "portfolio_id")),
        )
    if hasattr(model, "case_id"):
        return _ownership_chain(
            model,
            (
                (Case, "case_id"),
                (Borrower, "borrower_id"),
                (Portfolio, "portfolio_id"),
            ),
        )
    if hasattr(model, "memo_id"):
        return _ownership_chain(
            model,
            ((Memo, "memo_id"), (Borrower, "borrower_id"), (Portfolio, "portfolio_id")),
        )

    raise ScopeConfigurationError(
        f"No portfolio ownership path is registered for {model.__name__}; "
        "a scoped repository cannot be created for this model."
    )


def resolve_portfolio_path(session: Session, entity: object) -> str | None:
    """Resolve an entity's owning portfolio path through its join chain.

    ``None`` means that the row is absent or its ownership relation is broken;
    neither condition grants access.  This helper is useful for write-side
    validation and diagnostics in addition to the query predicate used by a
    repository.
    """
    model: type[Any] = cast(type[Any], type(entity))
    ownership = ownership_path_for(model)
    entity_id = getattr(entity, "id", None)
    if not isinstance(entity_id, UUID):
        raise TypeError("An entity with a UUID id is required to resolve portfolio ownership.")
    statement = select(ownership.path_column).select_from(model)
    statement = ownership.apply(statement).where(model.id == entity_id)
    return cast(str | None, session.scalar(statement))


def portfolio_path_for(entity: object, session: Session) -> str | None:
    """Resolve the owning path using the natural ``entity, session`` order."""
    return resolve_portfolio_path(session, entity)


def grant_reaches_path(granted_path: str, target_path: str, include_descendants: bool) -> bool:
    """Whether one portfolio grant reaches one target portfolio path.

    This is the single rule deciding whether a user's portfolio grant covers a
    given portfolio.  It lives here rather than in a view model because both
    the browser (which decides who may be *offered* a case) and
    `services/bulk.py` (which decides who may be *assigned* one) must apply
    exactly the same test; two copies would be free to drift, and a drift in
    this particular rule is an authorisation gap.
    """

    grant = granted_path.rstrip("/") + "/"
    target = target_path.rstrip("/") + "/"
    return target.startswith(grant) if include_descendants else target == grant


def auditor_role_present(session: Session, principal: Principal) -> bool:
    """Return whether ``principal`` is an active user with the auditor role.

    Any database/configuration failure returns ``False``.  Unscoped access is
    fail-closed when the role catalogue cannot be read.
    """
    if principal.kind is not PrincipalKind.USER:
        return False
    from covenant_radar.db.models.identity import AppUser, Role, UserRole

    try:
        value = session.scalar(
            select(Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .join(AppUser, AppUser.id == UserRole.user_id)
            .where(
                UserRole.user_id == principal.id,
                Role.code == AUDITOR_ROLE_CODE,
                AppUser.is_active.is_(True),
            )
            .limit(1)
        )
    except SQLAlchemyError:
        return False
    return value is not None


def authorise_unscoped_caller(
    session: Session,
    caller: UnscopedCaller,
    principal: Principal | None,
) -> object:
    """Validate one of the two named unscoped-read callers.

    The retention job is a non-user process and therefore cannot carry a
    principal.  The auditor path requires the exact persisted ``auditor``
    role; a broad permission such as ``VIEW_AUDIT`` is not sufficient.
    """
    if caller is RETENTION_JOB_CALLER:
        if principal is not None:
            raise AuthorizationError(
                "Unscoped repository access by the retention job cannot carry a user principal."
            )
        return RETENTION_JOB_CALLER.value
    if caller is AUDITOR_CALLER:
        if principal is None or not auditor_role_present(session, principal):
            raise AuthorizationError("Unscoped repository access requires the auditor role.")
        return principal.id
    raise AuthorizationError("Unscoped repository access has no valid named caller.")


__all__ = [
    "AUDITOR_CALLER",
    "AUDITOR_ROLE_CODE",
    "OwnershipPath",
    "RETENTION_JOB_CALLER",
    "Scope",
    "ScopeAuditError",
    "ScopeConfigurationError",
    "ScopeResolver",
    "UnscopedCaller",
    "authorise_unscoped_caller",
    "grant_reaches_path",
    "ownership_path_for",
    "portfolio_path_for",
    "resolve_portfolio_path",
    "resolve_scope",
    "scope_for_principal",
]
