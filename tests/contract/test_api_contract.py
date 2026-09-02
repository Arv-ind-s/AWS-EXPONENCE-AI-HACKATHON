"""Contract coverage for T-136's generated OpenAPI document (`R-32.a`).

The document is generated fresh from a live application instance in every
test here — none is read from disk — because a checked-in copy is exactly
the kind of generated artefact the standing prohibitions forbid committing,
and would drift from the implementation the moment a route changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.api.deps import iter_application_routes
from covenant_radar.api.openapi import (
    DEPRECATION_POLICY,
    build_openapi_document,
    validate_openapi_document,
)
from covenant_radar.api.v1.routers import (
    create_audit_events_router,
    create_borrowers_router,
    create_cases_router,
    create_covenant_tests_router,
    create_covenants_router,
    create_evidence_router,
    create_facilities_router,
    create_forecast_router,
    create_memos_router,
    create_simulations_router,
)
from covenant_radar.asgi import create_app
from covenant_radar.db.base import Base
from covenant_radar.services.master_data import MasterDataService
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.contract

_DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs" / "api"
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "patch", "trace"})


class _Audit:
    def record(self, *args: object, **kwargs: object) -> object:
        return object()


def _build_app() -> Any:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)
    audit = _Audit()
    cursor_secret = b"t-136-contract-test-cursor-secret-bytes"

    master_data_service = MasterDataService(session, audit=audit)
    registry_service = RegistryService(session, audit=audit, maker_checker_enabled=False)

    return create_app(
        routers=(
            create_borrowers_router(master_data_service),
            create_facilities_router(master_data_service),
            create_covenants_router(registry_service),
            create_covenant_tests_router(session, cursor_secret=cursor_secret),
            create_evidence_router(session, cursor_secret=cursor_secret),
            create_forecast_router(session, cursor_secret=cursor_secret),
            create_simulations_router(session, cursor_secret=cursor_secret),
            create_memos_router(session, cursor_secret=cursor_secret),
            create_cases_router(session, cursor_secret=cursor_secret),
            create_audit_events_router(session, cursor_secret=cursor_secret),
        ),
        principal_resolver=lambda _request: None,
    )


def _route_operations(app: Any) -> set[tuple[str, str]]:
    operations: set[tuple[str, str]] = set()
    # `iter_application_routes` rather than `app.routes`: this FastAPI version
    # keeps an included router behind a wrapper instead of copying its routes
    # onto the app, so a direct scan finds none of them and this contract
    # check silently compares the document against an empty implementation.
    for route, path in iter_application_routes(app):
        methods = getattr(route, "methods", None)
        if not path or not methods or not getattr(route, "include_in_schema", True):
            continue
        operations.update((path, method) for method in methods if method != "HEAD")
    return operations


def _document_operations(document: dict[str, Any]) -> set[tuple[str, str]]:
    operations: set[tuple[str, str]] = set()
    for path, item in document.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        operations.update(
            (path, method.upper()) for method in item if method.lower() in _HTTP_METHODS
        )
    return operations


def test_document_validates() -> None:
    app = _build_app()
    document = build_openapi_document(app, version="1.0.0-test")

    assert validate_openapi_document(document) == []
    assert document["info"]["x-deprecation-policy"] == DEPRECATION_POLICY

    policy_doc = (_DOCS_ROOT / "deprecation-policy.md").read_text(encoding="utf-8")
    assert " ".join(DEPRECATION_POLICY.split()) in " ".join(policy_doc.split())


def test_implementation_matches_document() -> None:
    app = _build_app()
    document = build_openapi_document(app, version="1.0.0-test")

    missing_from_document = _route_operations(app) - _document_operations(document)
    assert missing_from_document == set()


def test_document_matches_implementation() -> None:
    app = _build_app()
    document = build_openapi_document(app, version="1.0.0-test")

    phantom_in_document = _document_operations(document) - _route_operations(app)
    assert phantom_in_document == set()
