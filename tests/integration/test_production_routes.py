"""The composed production app must actually mount every router that exists.

Every feature router in this codebase is well covered by a test that builds a
minimal app around that one router.  None of those tests can see whether
`create_production_app` ever includes it, and for six browser routers plus the
entire `/api/v1` surface the answer was no: the routers were written, tested
and simply never wired, so `/cases` and `/certificates` returned 404 while
being linked from the navigation, and the documented REST API did not exist at
runtime.

These tests close that gap by asserting against the real composition root.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine

import covenant_radar.api.v1.routers as api_routers
import covenant_radar.web.routes as web_routes
from covenant_radar.api.deps import iter_application_routes
from covenant_radar.config.settings import load_settings
from covenant_radar.db.base import Base
from covenant_radar.web.application import create_production_app


@pytest.fixture(scope="module")
def production_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FastAPI]:
    """Build the real composition root against a throwaway database.

    Explicitly configured rather than reading the developer's `.env`, so the
    route table under test is the one the code produces, not the one a local
    environment happens to yield.
    """
    database = tmp_path_factory.mktemp("routes") / "routes.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()

    settings = load_settings(
        environ={
            "COVENANT_RADAR_DATABASE__URL": f"sqlite:///{database}",
            "COVENANT_RADAR_SECURITY_SESSION_SECRET": "route-table-test-secret-0123456789",
            # `none` keeps the document and export stores out of the
            # field-encryption key path; this test is about which routes
            # exist, not about storage.
            "COVENANT_RADAR_DOCUMENTS__STORE": "none",
            "COVENANT_RADAR_AI__PROVIDER": "none",
        }
    )
    yield create_production_app(settings)


@pytest.fixture(scope="module")
def mounted_paths(production_app: FastAPI) -> frozenset[str]:
    # Deliberately the same walker the authorization guard uses, so this test
    # cannot pass against a route table the guard cannot see.
    return frozenset(path for _, path in iter_application_routes(production_app) if path)


def _router_factories(package: Any) -> set[str]:
    """Every `create_*_router` callable defined across a router package."""
    found: set[str] = set()
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        for name, value in vars(module).items():
            if not name.startswith("create_") or not name.endswith("_router"):
                continue
            if not inspect.isfunction(value):
                continue
            # Skip re-exports: only count a factory in the module defining it,
            # so an alias such as `create_signal_ingest_router` is not demanded
            # as a separate mount of the same routes.
            if value.__module__ != module.__name__:
                continue
            found.add(name)
    return found


# Paths that must be reachable, keyed by the router that provides them.  These
# are the routers whose absence from the composition root was invisible to the
# per-router tests.
REQUIRED_BROWSER_PATHS = (
    "/",
    "/cases",
    "/certificates",
    "/dispositions",
    "/statements/restate",
    "/statements/quarantine",
    "/views",
    "/recent-items",
    "/bulk",
    "/exports",
    "/borrowers",
    "/covenants",
    "/intake",
    "/simulator",
    "/audit",
    "/governance",
    "/search",
    "/notifications",
    "/admin/users",
    "/admin/jobs",
    "/admin/config",
)

REQUIRED_API_PATHS = (
    "/api/v1/borrowers",
    "/api/v1/facilities",
    "/api/v1/covenants",
    "/api/v1/cases",
    "/api/v1/tests",
    "/api/v1/evidence",
    "/api/v1/forecasts",
    "/api/v1/memos",
    "/api/v1/simulations",
    "/api/v1/audit-events",
    "/api/v1/ingest/signals",
)


@pytest.mark.parametrize("path", REQUIRED_BROWSER_PATHS)
def test_browser_path_is_mounted(mounted_paths: frozenset[str], path: str) -> None:
    assert path in mounted_paths, (
        f"{path} is not mounted by create_production_app. A router that exists but is "
        "never included answers 404 in the running product while its own unit tests pass."
    )


def test_borrower_create_route_precedes_case_file_reference_route(
    production_app: FastAPI,
) -> None:
    paths = [path for _, path in iter_application_routes(production_app)]
    assert paths.index("/borrowers/new") < paths.index("/borrowers/{reference}")


@pytest.mark.parametrize("path", REQUIRED_API_PATHS)
def test_api_path_is_mounted(mounted_paths: frozenset[str], path: str) -> None:
    assert path in mounted_paths, (
        f"{path} is missing; the public REST API (spec R-32) is not served."
    )


# Factories the composition root legitimately does not name, each because it
# is already composed somewhere else.  Every exemption is paired with a path
# asserted above, so an exemption cannot be used to hide an unreachable router.
COMPOSED_ELSEWHERE = {
    # `routes/admin.py` includes both into the users router it returns.
    "create_admin_ops_router": "/admin/jobs",
    "create_admin_config_router": "/admin/config",
    # `asgi.create_app` mounts this for every app it builds, not just this one.
    "create_system_router": "/health",
}


def test_every_web_router_factory_is_used(mounted_paths: frozenset[str]) -> None:
    """No `create_*_router` in `web/routes` may go unmounted.

    Read from the composition root's source rather than its route table:
    a factory can be composed inside another factory, so requiring one mount
    per factory would be wrong. Requiring either a reference or a documented
    exemption backed by a live path catches the real defect — a router module
    nobody wired at all.
    """
    source = inspect.getsource(create_production_app)
    unused = sorted(
        name
        for name in _router_factories(web_routes)
        if name not in source and name not in COMPOSED_ELSEWHERE
    )
    assert not unused, (
        "These browser router factories are never referenced by create_production_app: "
        f"{unused}. Mount them or delete them; an unmounted router is a 404 with tests."
    )
    for name, witness in COMPOSED_ELSEWHERE.items():
        assert witness in mounted_paths, (
            f"{name} is exempt from the reference check because it is composed elsewhere, "
            f"but {witness} is not mounted, so it is not actually reachable."
        )


def test_every_api_router_factory_is_used() -> None:
    """The same rule for the versioned REST API package.

    Aliases are resolved rather than exempted: `create_signal_ingest_router`
    is another name for `create_ingest_router`, and mounting it twice would
    register the same paths twice.
    """
    module = importlib.import_module("covenant_radar.web.application")
    source = inspect.getsource(module)
    unused = []
    for name in sorted(_router_factories(api_routers)):
        if name in source:
            continue
        target = getattr(api_routers, name, None)
        aliases = [
            other
            for other in dir(api_routers)
            if other != name and getattr(api_routers, other, None) is target
        ]
        if any(alias in source for alias in aliases):
            continue
        unused.append(name)
    assert not unused, (
        f"These API router factories are never referenced by the composition root: {unused}."
    )


def test_openapi_document_generates(production_app: FastAPI) -> None:
    """`/openapi.json` must build.

    One route whose signature cannot be turned into a JSON schema fails the
    whole document, taking `/docs` down with it — which is what a
    `Depends(public)` marker carrying a `Callable` parameter used to do.
    """
    document = production_app.openapi()
    assert document["paths"], "OpenAPI generation produced no paths."
    for path in REQUIRED_API_PATHS:
        assert path in document["paths"], f"{path} is absent from the OpenAPI document."
