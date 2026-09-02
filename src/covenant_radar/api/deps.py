"""FastAPI dependencies and startup checks for authorization.

Routes opt in with ``Depends(requires(Permission.X))`` or explicitly opt out
with the ``public`` marker.  The dependency resolves either a request-bound
principal, a configured session/API-key resolver, or no principal; it always
checks before the route handler can run and maps the decision to the standing
401/403 HTTP contract.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from fastapi import HTTPException, Request
from starlette.routing import BaseRoute, Mount

from covenant_radar.core.context import get_request_id
from covenant_radar.core.errors import AuthorizationError
from covenant_radar.security.permissions import Permission, coerce_permission
from covenant_radar.security.rbac import (
    AUTHORIZATION_DENIED_EVENT,
    PermissionConfigurationError,
    PermissionLike,
    PermissionReachabilityReport,
    Principal,
    RolePermissionResolver,
    RolePermissions,
    authorize,
    permission_reachability,
)
from covenant_radar.security.sessions import SessionManager

_LOGGER = logging.getLogger(__name__)
_AUTHORIZATION_DECLARATION = "__covenant_radar_authorization_declaration__"
_PUBLIC_MARKER = "__covenant_radar_public__"
_PERMISSION_MARKER = "__covenant_radar_permission__"
_SUBJECT_MARKER = "__covenant_radar_subject__"
_STARTUP_CHECK_MARKER = "__covenant_radar_startup_check_registered__"
_PRINCIPAL_RESOLVER_STATE = "principal_resolver"
_AUDIT_WRITER_STATE = "audit_writer"
_REQUEST_PRINCIPAL_STATE = "principal"
_FRAMEWORK_PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)
_FRAMEWORK_PUBLIC_ROUTE_NAMES: Final[frozenset[str]] = frozenset(
    {"openapi", "swagger_ui_html", "swagger_ui_redirect", "redoc_html"}
)

type PrincipalResolverResult = Principal | None | Awaitable[Principal | None]


class PrincipalResolver(Protocol):
    """Resolve the request's session or API-key credential."""

    def resolve(self, request: Request) -> PrincipalResolverResult:
        """Return a principal, or ``None`` when the credential is invalid."""


class ApiKeyAuthenticator(Protocol):
    """Resolve one raw bearer credential without exposing its secret."""

    def __call__(self, credential: str) -> Principal | None:
        """Return a principal for a valid, active, scoped key."""


class AuditWriter(Protocol):
    """The small audit boundary used for authorization refusals."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an event in the caller's transaction."""


class RequestPrincipalResolver:
    """Resolve a browser session or an API key using injected adapters.

    The session manager validates signed cookies and server-side revocation;
    the role resolver supplies current permissions.  API-key authentication is
    deliberately a callback so hashing, expiry and rate limiting stay inside
    the database/security adapter that owns those records.
    """

    def __init__(
        self,
        *,
        sessions: SessionManager | None = None,
        roles: RolePermissionResolver | None = None,
        api_keys: ApiKeyAuthenticator | None = None,
    ) -> None:
        if sessions is None and roles is not None:
            raise ValueError("A session manager is required when a role resolver is configured.")
        self.sessions = sessions
        self.roles = roles
        self.api_keys = api_keys

    def resolve(self, request: Request) -> Principal | None:
        """Resolve bearer API keys first, then signed browser sessions."""
        authorization = request.headers.get("authorization", "")
        if authorization:
            scheme, separator, credential = authorization.partition(" ")
            if separator == " " and scheme.lower() == "bearer" and credential:
                if self.api_keys is None:
                    return None
                return self.api_keys(credential)
            return None

        if self.sessions is None:
            return None
        cookie = request.cookies.get(self.sessions.settings.cookie_name)
        session = self.sessions.validate(cookie)
        if session is None:
            return None
        if self.roles is None:
            raise PermissionConfigurationError(
                "A session principal resolver requires a role permission resolver."
            )
        return self.roles.principal_for_user(session.user_id)


def configure_principal_resolver(app: Any, resolver: PrincipalResolver) -> None:
    """Install the request credential resolver on an application instance."""
    app.state.principal_resolver = resolver


def configure_audit_writer(app: Any, audit: AuditWriter) -> None:
    """Install the audit writer used by authorization refusals."""
    app.state.audit_writer = audit


async def resolve_principal(request: Request) -> Principal | None:
    """Resolve a principal from request state or configured credentials."""
    state_principal = getattr(request.state, _REQUEST_PRINCIPAL_STATE, None)
    if state_principal is not None:
        if not isinstance(state_principal, Principal):
            raise PermissionConfigurationError("Request state contains an invalid principal.")
        return state_principal

    resolver = getattr(request.app.state, _PRINCIPAL_RESOLVER_STATE, None)
    if resolver is None:
        return None
    if hasattr(resolver, "resolve"):
        resolve = cast(Callable[[Request], PrincipalResolverResult], resolver.resolve)
        result = resolve(request)
    elif callable(resolver):
        resolve = cast(Callable[[Request], PrincipalResolverResult], resolver)
        result = resolve(request)
    else:
        raise PermissionConfigurationError("Configured principal resolver is not callable.")
    if inspect.isawaitable(result):
        result = await result
    if result is not None and not isinstance(result, Principal):
        raise PermissionConfigurationError("Principal resolver returned an invalid principal.")
    return result


def requires(
    permission: PermissionLike,
    *,
    subject: object | Callable[[Request, Principal], object] | None = None,
) -> Callable[[Request], Awaitable[Principal]]:
    """Create a FastAPI dependency that enforces one permission.

    ``subject`` is metadata for the scope layer and audit context.  It is not
    dereferenced here: row-level scope resolution belongs to the repository
    layer and must not be bypassed by an HTTP dependency.
    """
    normalized = coerce_permission(permission)

    async def dependency(request: Request) -> Principal:
        principal = await resolve_principal(request)
        if principal is None:
            _record_denial(
                request, principal, normalized, reason="unauthenticated", subject=subject
            )
            raise HTTPException(
                status_code=401,
                detail="Authentication required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            authorize(principal, normalized)
        except AuthorizationError as error:
            _record_denial(
                request, principal, normalized, reason="missing_permission", subject=subject
            )
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: {normalized.value}.",
            ) from error
        return principal

    dependency.__name__ = f"require_{normalized.value.lower()}"
    setattr(dependency, _PERMISSION_MARKER, normalized)
    setattr(dependency, _AUTHORIZATION_DECLARATION, normalized)
    setattr(dependency, _SUBJECT_MARKER, subject)
    return dependency


def public(target: Callable[..., object] | None = None) -> Callable[..., object] | None:
    """Mark a route or dependency as intentionally public.

    As a decorator, use ``@public`` on the endpoint.  As a FastAPI
    dependency, use ``Depends(public)``.  Supporting both forms keeps the
    marker explicit in the route declaration while avoiding a second public
    marker API.
    """
    if target is not None:
        setattr(target, _PUBLIC_MARKER, True)
        setattr(target, _AUTHORIZATION_DECLARATION, "public")
        return target
    return None


# The callable itself also carries the marker so ``Depends(public)`` is
# discoverable by the registry before FastAPI executes it.
setattr(public, _PUBLIC_MARKER, True)
setattr(public, _AUTHORIZATION_DECLARATION, "public")

# `public` is deliberately dual-form, and the two forms want different
# signatures.  As a decorator it takes the endpoint; as `Depends(public)` it
# takes nothing.  FastAPI builds a dependant by reading `inspect.signature`,
# so without this it reads `target` as a request parameter, tries to derive a
# JSON schema for `Callable[..., object]`, and fails — which took the whole of
# `/openapi.json` (and `/docs` with it) down with a 500, since one
# unschemable route breaks generation for every route.  Publishing the
# zero-argument signature FastAPI should see keeps both forms working:
# `inspect.signature` honours `__signature__`, while calling `public(fn)`
# directly is unaffected.
public.__signature__ = inspect.Signature()  # type: ignore[attr-defined]


public_dependency = public
mark_public = public
public_route = public


@dataclass(frozen=True, slots=True)
class RouteDeclaration:
    """One registered route's authorization declaration."""

    path: str
    methods: tuple[str, ...]
    name: str
    permission: Permission | None = None
    is_public: bool = False

    @property
    def label(self) -> str:
        """Return a stable method/path label for logs and diagnostics."""
        methods = ",".join(self.methods)
        return f"{methods} {self.path}"


@dataclass(frozen=True, slots=True)
class RouteDeclarationReport:
    """Startup output for protected and explicitly public routes."""

    routes: tuple[RouteDeclaration, ...]

    @property
    def public_routes(self) -> tuple[str, ...]:
        """Labels of routes intentionally exposed without authentication."""
        return tuple(route.label for route in self.routes if route.is_public)

    @property
    def protected_routes(self) -> tuple[str, ...]:
        """Labels of routes protected by a permission declaration."""
        return tuple(route.label for route in self.routes if route.permission is not None)


class RouteDeclarationError(RuntimeError):
    """Raised when a registered route has no unambiguous declaration."""


def validate_route_declarations(app: Any) -> RouteDeclarationReport:
    """Validate every application route and log the resulting registry."""
    declarations: list[RouteDeclaration] = []
    for route, path_prefix in _iter_routes(app.routes, prefix=""):
        if _is_framework_route(route, path_prefix):
            continue
        declaration = _route_declaration(route, path_prefix)
        if declaration is None:
            raise RouteDeclarationError(
                f"Route {_route_label(route, path_prefix)} has no authorization declaration; "
                "declare a Permission with requires(...) or mark it public with @public."
            )
        declarations.append(declaration)

    report = RouteDeclarationReport(tuple(declarations))
    for declared_route in report.routes:
        if declared_route.is_public:
            _LOGGER.info("Authorization public route declared: %s", declared_route.label)
        else:
            _LOGGER.info(
                "Authorization protected route declared: %s requires %s",
                declared_route.label,
                declared_route.permission.value if declared_route.permission is not None else "",
            )
    return report


def check_route_declarations(app: Any) -> RouteDeclarationReport:
    """Alias used by startup wiring and security tests."""
    return validate_route_declarations(app)


def install_route_declaration_check(
    app: Any,
    *,
    role_permissions: Callable[[], RolePermissions] | RolePermissions | None = None,
) -> None:
    """Register the fail-closed route and permission checks at startup."""
    if getattr(app.state, _STARTUP_CHECK_MARKER, False):
        return

    async def startup_check() -> None:
        # Deliberately not caught: a route with no authorization declaration is
        # a route nobody decided the access rules for, and `spec §N-04` makes
        # that a refusal rather than a warning.  Downgrading it to a log line
        # turns a fail-closed guarantee into a message in a stream nobody
        # reads, and lets an unprotected endpoint ship.
        validate_route_declarations(app)
        if role_permissions is not None:
            assignments = role_permissions() if callable(role_permissions) else role_permissions
            report = permission_reachability(assignments)
            if not report.ok:
                _LOGGER.error("Authorization permission reachability failed: %s", report.message)
                raise PermissionConfigurationError(report.message)

    app.router.on_event("startup")(startup_check)
    setattr(app.state, _STARTUP_CHECK_MARKER, True)
    app.state.authorization_startup_check = startup_check


register_route_declaration_check = install_route_declaration_check
register_authorization_checks = install_route_declaration_check


def permission_reachability_report(
    role_permissions: Callable[[], RolePermissions] | RolePermissions,
) -> PermissionReachabilityReport:
    """Return a deterministic reachability report for diagnostics/tests."""
    assignments = role_permissions() if callable(role_permissions) else role_permissions
    return permission_reachability(assignments)


def _record_denial(
    request: Request,
    principal: Principal | None,
    permission: Permission,
    *,
    reason: str,
    subject: object | Callable[[Request, Principal], object] | None,
) -> None:
    audit = getattr(request.state, _AUDIT_WRITER_STATE, None)
    if audit is None:
        audit = getattr(request.app.state, _AUDIT_WRITER_STATE, None)
    payload: dict[str, object] = {
        "outcome": "denied",
        "reason": reason,
        "permission": permission.value,
        "principal_kind": principal.kind.value if principal is not None else "anonymous",
        "route": request.url.path,
    }
    if subject is not None and not callable(subject):
        payload["subject_declared"] = True
    if audit is None:
        _LOGGER.warning(
            "Authorization refusal was not audited: permission=%s route=%s reason=%s",
            permission.value,
            request.url.path,
            reason,
        )
        return
    actor: object = principal.id if principal is not None else None
    request_id = getattr(request.state, "request_id", None) or get_request_id() or "unknown"
    try:
        audit.record(
            AUTHORIZATION_DENIED_EVENT,
            ("route", request.url.path),
            payload,
            actor=actor,
            request_id=request_id,
        )
    except Exception:
        # Authorization remains denied if an audit adapter is unhealthy.  A
        # logging failure must never turn a refusal into an allow decision.
        _LOGGER.exception(
            "Authorization refusal audit failed: permission=%s route=%s",
            permission.value,
            request.url.path,
        )


def _iter_routes(routes: Iterable[BaseRoute], *, prefix: str) -> Iterable[tuple[BaseRoute, str]]:
    for route in routes:
        # FastAPI >= 0.135 does not copy an included router's routes onto the
        # app; it stores a wrapper holding the original router and resolves it
        # per request.  Those wrappers carry no path, endpoint or dependant, so
        # walking `app.routes` alone sees a handful of anonymous objects and
        # none of the real endpoints — which would make this check either
        # vacuous or, as it did here, refuse every route for lack of a
        # declaration it was never able to read.
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            include_prefix = getattr(context, "prefix", "") or ""
            yield from _iter_routes(
                original.routes, prefix=_join_paths(prefix, include_prefix)
            )
            continue
        route_path = _join_paths(prefix, getattr(route, "path", ""))
        if isinstance(route, Mount) and hasattr(route.app, "routes"):
            yield from _iter_routes(route.app.routes, prefix=route_path)
        else:
            yield route, route_path


def iter_application_routes(app: Any) -> Iterable[tuple[BaseRoute, str]]:
    """Yield every concrete route of `app` with its fully-resolved path.

    The public form of the walk `validate_route_declarations` relies on.  It
    exists because reading `app.routes` directly is wrong on this FastAPI
    version — included routers are stored as wrappers and resolved per request
    — and every caller that got that wrong (the authorization guard, the API
    contract check) failed quietly rather than loudly, by finding no routes to
    inspect instead of reporting that it could not see them.
    """
    return _iter_routes(getattr(app, "routes", app), prefix="")


def _is_framework_route(route: BaseRoute, path: str) -> bool:
    if (
        path in _FRAMEWORK_PUBLIC_PATHS
        and getattr(route, "name", None) in _FRAMEWORK_PUBLIC_ROUTE_NAMES
    ):
        return True
    if isinstance(route, Mount):
        return True
    return False


def _route_declaration(route: BaseRoute, path: str) -> RouteDeclaration | None:
    endpoint = getattr(route, "endpoint", None)
    permission_values: list[Permission] = []
    is_public = bool(getattr(endpoint, _PUBLIC_MARKER, False))
    endpoint_permission = getattr(endpoint, _PERMISSION_MARKER, None)
    if endpoint_permission is not None:
        permission_values.append(coerce_permission(cast(PermissionLike, endpoint_permission)))

    for dependency in _dependency_calls(getattr(route, "dependant", None)):
        if getattr(dependency, _PUBLIC_MARKER, False):
            is_public = True
        dependency_permission = getattr(dependency, _PERMISSION_MARKER, None)
        if dependency_permission is not None:
            permission_values.append(coerce_permission(cast(PermissionLike, dependency_permission)))

    unique_permissions = tuple(dict.fromkeys(permission_values))
    if is_public and unique_permissions:
        raise RouteDeclarationError(
            f"Route {_route_label(route, path)} declares both public access and a permission."
        )
    if is_public:
        declaration = RouteDeclaration(
            path=path,
            methods=_route_methods(route),
            name=getattr(route, "name", ""),
            is_public=True,
        )
        return declaration
    if unique_permissions:
        return RouteDeclaration(
            path=path,
            methods=_route_methods(route),
            name=getattr(route, "name", ""),
            permission=unique_permissions[0],
        )
    return None


def _dependency_calls(dependant: Any) -> Iterable[Callable[..., object]]:
    if dependant is None:
        return
    for child in getattr(dependant, "dependencies", ()):
        call = getattr(child, "call", None)
        if callable(call):
            yield call
        yield from _dependency_calls(child)


def _route_methods(route: BaseRoute) -> tuple[str, ...]:
    methods = getattr(route, "methods", None)
    if methods:
        return tuple(sorted(str(method) for method in methods))
    if route.__class__.__name__ == "WebSocketRoute":
        return ("WEBSOCKET",)
    return ("*",)


def _route_label(route: BaseRoute, path: str) -> str:
    return f"{','.join(_route_methods(route))} {path}"


def _join_paths(prefix: str, path: str) -> str:
    if not prefix:
        return path
    if prefix.endswith("/") and path.startswith("/"):
        return prefix.rstrip("/") + path
    return prefix + path


__all__ = [
    "ApiKeyAuthenticator",
    "AuditWriter",
    "PrincipalResolver",
    "RequestPrincipalResolver",
    "RouteDeclaration",
    "RouteDeclarationError",
    "RouteDeclarationReport",
    "check_route_declarations",
    "configure_audit_writer",
    "configure_principal_resolver",
    "install_route_declaration_check",
    "iter_application_routes",
    "mark_public",
    "permission_reachability_report",
    "public",
    "public_dependency",
    "public_route",
    "register_authorization_checks",
    "register_route_declaration_check",
    "requires",
    "resolve_principal",
    "validate_route_declarations",
]
