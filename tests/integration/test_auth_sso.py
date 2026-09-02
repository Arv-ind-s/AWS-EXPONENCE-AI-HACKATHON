"""Offline integration coverage for T-014 OIDC, SAML and JIT provisioning."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from covenant_radar.core.clock import FixedClock
from covenant_radar.security.oidc import OIDCClient, OIDCError, OIDCSettings
from covenant_radar.security.passwords import Argon2Parameters, PasswordPolicy, PasswordService
from covenant_radar.security.provisioning import (
    AttributeMapping,
    IdentitySource,
    InMemoryProvisioningStore,
    ProvisioningService,
    ProvisioningSettings,
)
from covenant_radar.security.saml import SAMLError, SAMLServiceProvider, SAMLSettings
from covenant_radar.security.sessions import InMemorySessionStore, SessionManager, SessionSettings
from covenant_radar.services.auth import AuthService, UserRecord
from covenant_radar.web.routes.auth import create_auth_router
from tests.fixtures.idp import (
    FIXED_NOW,
    IDP_ENTITY_ID,
    OIDC_OLD_KEY,
    SAML_CERTIFICATE_PEM,
    SAML_ENTITY_ID,
    LocalOIDCTransport,
    make_saml_response,
)

pytestmark = pytest.mark.integration

_USER_ID = UUID("00000000-0000-7000-8000-000000000021")
_PASSWORD = "Correct-Horse-123!"


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del subject, actor, request_id
        self.events.append((event_type, dict(payload)))
        return object()


class _Users:
    def __init__(self, user: UserRecord) -> None:
        self.user = user

    def find_by_username(self, username: str) -> UserRecord | None:
        return self.user if username == self.user.username else None

    def get(self, user_id: UUID) -> UserRecord | None:
        return self.user if user_id == self.user.id else None

    def save(self, user: UserRecord) -> None:
        self.user = user


def _auth_service(audit: _Audit) -> AuthService:
    passwords = PasswordService(
        parameters=Argon2Parameters(time_cost=1, memory_cost=1024, parallelism=1),
        policy=PasswordPolicy(min_length=12),
    )
    users = _Users(
        UserRecord(id=_USER_ID, username="breakglass", password_hash=passwords.hash(_PASSWORD))
    )
    sessions = SessionManager(
        InMemorySessionStore(),
        settings=SessionSettings(secret=b"s" * 32, secure_cookie=False),
        clock=FixedClock(FIXED_NOW),
    )
    return AuthService(
        users,
        sessions,
        passwords=passwords,
        clock=FixedClock(FIXED_NOW),
        audit=audit,
        request_id="rq-sso-test",
    )


def _oidc(transport: LocalOIDCTransport, audit: _Audit) -> OIDCClient:
    settings = OIDCSettings(
        issuer="https://idp.example.test",
        client_id="radar-client",
        client_secret="fixture-secret",
        redirect_uri="https://radar.example.test/sso/oidc/callback",
    )
    return OIDCClient(
        settings,
        clock=FixedClock(FIXED_NOW),
        audit=audit,
        transport=transport,
        request_id="rq-oidc-test",
    )


def _saml(audit: _Audit) -> SAMLServiceProvider:
    settings = SAMLSettings(
        entity_id=SAML_ENTITY_ID,
        idp_entity_id=IDP_ENTITY_ID,
        single_sign_on_url="https://idp.example.test/saml/sso",
        assertion_consumer_service_url="https://radar.example.test/sso/saml/acs",
        idp_certificate=SAML_CERTIFICATE_PEM,
    )
    return SAMLServiceProvider(
        settings,
        clock=FixedClock(FIXED_NOW),
        audit=audit,
        request_id="rq-saml-test",
    )


def _set_oidc_nonce(authorization_url: str, transport: LocalOIDCTransport) -> str:
    query = parse_qs(urlsplit(authorization_url).query)
    transport.nonce = query["nonce"][0]
    return query["state"][0]


def _provisioner(audit: _Audit, *, notifier=None) -> ProvisioningService:
    return ProvisioningService(
        InMemoryProvisioningStore(),
        mapping=AttributeMapping(),
        settings=ProvisioningSettings(
            allowed_roles=frozenset({"relationship_manager", "credit_officer"}),
            default_role="relationship_manager",
        ),
        clock=FixedClock(FIXED_NOW),
        audit=audit,
        notifier=notifier,
        request_id="rq-provision-test",
    )


def test_oidc_happy_path_issues_session() -> None:
    audit = _Audit()
    transport = LocalOIDCTransport()
    oidc = _oidc(transport, audit)
    service = _auth_service(audit)
    app = FastAPI()
    app.include_router(
        create_auth_router(service, oidc=oidc, provisioning=_provisioner(audit)),
    )

    with TestClient(app) as client:
        start = client.get("/sso/oidc/start?next=/queue", follow_redirects=False)
        state = _set_oidc_nonce(start.headers["location"], transport)
        callback = client.get(
            f"/sso/oidc/callback?code=fixture-code&state={state}",
            follow_redirects=False,
        )

    assert start.status_code == 303
    assert callback.status_code == 303
    assert callback.headers["location"] == "/queue"
    assert "covenant_radar_session=" in callback.headers["set-cookie"]
    assert any(event == "authentication_sso_session_issued" for event, _ in audit.events)


def test_oidc_state_mismatch_refused() -> None:
    audit = _Audit()
    transport = LocalOIDCTransport()
    oidc = _oidc(transport, audit)
    request = asyncio.run(oidc.begin_authorization())

    with pytest.raises(OIDCError):
        asyncio.run(
            oidc.complete_callback("fixture-code", "attacker-state", expected_state=request.state)
        )

    assert audit.events[-1][1]["reason"] == "state_mismatch"


def test_oidc_nonce_mismatch_refused() -> None:
    audit = _Audit()
    transport = LocalOIDCTransport()
    oidc = _oidc(transport, audit)
    request = asyncio.run(oidc.begin_authorization())
    transport.nonce = "a-different-nonce"

    with pytest.raises(OIDCError):
        asyncio.run(
            oidc.complete_callback("fixture-code", request.state, expected_state=request.state)
        )

    assert audit.events[-1][1]["reason"] == "nonce_mismatch"


def test_jwks_rotation_handled() -> None:
    audit = _Audit()
    transport = LocalOIDCTransport(key=OIDC_OLD_KEY, kid="old")
    oidc = _oidc(transport, audit)
    request = asyncio.run(oidc.begin_authorization())
    transport.nonce = request.nonce
    asyncio.run(oidc._get_jwks())
    transport.rotate()

    identity = asyncio.run(
        oidc.complete_callback("fixture-code", request.state, expected_state=request.state)
    )

    assert identity.subject == "subject-1"
    assert len([item for item in transport.requests if item.url.path == "/jwks"]) == 2


def test_saml_unsigned_assertion_refused() -> None:
    audit = _Audit()
    provider = _saml(audit)
    request = provider.begin_authorization()

    with pytest.raises(SAMLError):
        provider.complete_callback(
            make_saml_response(request.request_id, signed=False), request.relay_state
        )

    assert audit.events[-1][1]["reason"] == "assertion_signature_missing_or_ambiguous"


def test_saml_replay_refused() -> None:
    audit = _Audit()
    provider = _saml(audit)
    first = provider.begin_authorization()
    response = make_saml_response(first.request_id, assertion_id="_same-assertion")
    assert provider.complete_callback(response, first.relay_state).subject == "subject-1"
    second = provider.begin_authorization()

    with pytest.raises(SAMLError):
        provider.complete_callback(
            make_saml_response(second.request_id, assertion_id="_same-assertion"),
            second.relay_state,
        )

    assert audit.events[-1][1]["reason"] == "assertion_replayed"


def test_saml_audience_mismatch_refused() -> None:
    audit = _Audit()
    provider = _saml(audit)
    request = provider.begin_authorization()

    with pytest.raises(SAMLError):
        provider.complete_callback(
            make_saml_response(request.request_id, audience="https://other.example.test"),
            request.relay_state,
        )

    assert audit.events[-1][1]["reason"] == "audience_mismatch"


def test_attribute_mapping_to_roles_and_scope() -> None:
    audit = _Audit()
    provider = _saml(audit)
    request = provider.begin_authorization()
    identity = provider.complete_callback(
        make_saml_response(request.request_id), request.relay_state
    )
    provisioned = _provisioner(audit).provision(identity.claims, source=IdentitySource.SAML)

    assert provisioned.roles == ("relationship_manager",)
    assert provisioned.portfolio_scope == ("portfolio/root",)
    assert provisioned.auth_source is IdentitySource.SAML


def test_unknown_role_provisions_default_and_notifies() -> None:
    audit = _Audit()
    notifications: list[tuple[str, dict[str, object]]] = []
    provisioner = _provisioner(
        audit, notifier=lambda event, payload: notifications.append((event, dict(payload)))
    )

    provisioned = provisioner.provision(
        {
            "sub": "subject-unknown-role",
            "email": "unknown@example.test",
            "preferred_username": "unknown",
            "name": "Unknown Role",
            "roles": ["not-a-role"],
            "portfolio_scope": ["portfolio/root"],
        },
        source=IdentitySource.OIDC,
    )

    assert provisioned.roles == ("relationship_manager",)
    assert notifications[0][0] == "authentication_sso_unknown_role"
    assert notifications[0][1]["default_role"] == "relationship_manager"


def test_provider_down_leaves_local_path_available() -> None:
    class DownTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fixture provider down", request=request)

    audit = _Audit()
    service = _auth_service(audit)
    oidc = _oidc(LocalOIDCTransport(), audit)
    oidc._transport = DownTransport()
    app = FastAPI()
    app.include_router(
        create_auth_router(service, oidc=oidc, provisioning=_provisioner(audit)),
    )

    with TestClient(app) as client:
        unavailable = client.get("/sso/oidc/start", follow_redirects=False)
        local = client.post(
            "/sign-in",
            data={"username": "breakglass", "password": _PASSWORD, "next": "/"},
            follow_redirects=False,
        )

    assert unavailable.status_code == 503
    assert "OIDC sign-in is unavailable" in unavailable.text
    assert "Local sign-in remains available" in unavailable.text
    assert local.status_code == 303
