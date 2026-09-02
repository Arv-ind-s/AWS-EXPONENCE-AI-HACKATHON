"""Security tests for T-015's declarative route authorization."""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from covenant_radar.api.deps import (
    RouteDeclarationError,
    install_route_declaration_check,
    public,
    requires,
    validate_route_declarations,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

pytestmark = pytest.mark.security

_USER_ID = UUID("00000000-0000-7000-8000-000000000016")


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object], object]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del subject, request_id
        self.events.append((event_type, dict(payload), actor))
        return object()


def _declared_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    @public
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/queue", dependencies=[Depends(requires(Permission.VIEW_QUEUE))])
    def queue() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_every_route_declares_permission_or_public() -> None:
    report = validate_route_declarations(_declared_app())

    assert "GET /health" in report.public_routes
    assert "GET /queue" in report.protected_routes


def test_missing_declaration_refuses_startup() -> None:
    app = FastAPI()

    @app.get("/forgotten")
    def forgotten() -> dict[str, bool]:
        return {"ok": True}

    install_route_declaration_check(app)

    with pytest.raises(RouteDeclarationError, match="GET /forgotten"):
        with TestClient(app):
            pass


def test_refusal_names_missing_permission() -> None:
    app = FastAPI()
    app.state.principal_resolver = lambda _request: Principal.user(_USER_ID, ())
    reached = False

    @app.get("/queue", dependencies=[Depends(requires(Permission.VIEW_QUEUE))])
    def queue() -> dict[str, bool]:
        nonlocal reached
        reached = True
        return {"ok": True}

    response = TestClient(app).get("/queue")

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: VIEW_QUEUE."
    assert not reached


def test_refusal_is_audited() -> None:
    app = FastAPI()
    app.state.principal_resolver = lambda _request: Principal.api_key(_USER_ID, ())
    audit = _Audit()
    app.state.audit_writer = audit

    @app.get("/queue", dependencies=[Depends(requires(Permission.VIEW_QUEUE))])
    def queue() -> dict[str, bool]:
        return {"ok": True}

    response = TestClient(app).get("/queue")

    assert response.status_code == 403
    assert len(audit.events) == 1
    event, payload, actor = audit.events[0]
    assert event == "authorization_denied"
    assert payload["permission"] == "VIEW_QUEUE"
    assert payload["reason"] == "missing_permission"
    assert actor == _USER_ID
