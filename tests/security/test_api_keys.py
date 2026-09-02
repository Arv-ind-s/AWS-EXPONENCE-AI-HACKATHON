"""Security coverage for T-136's API-key authentication and rate limiting
(`R-32.b`, `R-32.c`): cross-portfolio isolation, immediate revocation, the
per-key `429`, and that key material never reaches a log or an audit event.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.api.deps import RequestPrincipalResolver
from covenant_radar.api.keys import (
    ApiKeyAuthenticator,
    ApiKeyRateLimitMiddleware,
    SqlAlchemyApiKeyLookup,
)
from covenant_radar.api.v1.routers import create_borrowers_router, create_evidence_router
from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import ApiKey, AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.api_keys import ApiKeyService
from covenant_radar.services.master_data import MasterDataService

pytestmark = pytest.mark.security

_NOW = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


class _Audit:
    """Collects every recorded event so a test can scan for leaked secrets."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append(
            {
                "event_type": event_type,
                "subject": subject,
                "payload": dict(payload),
                "actor": actor,
                "request_id": request_id,
            }
        )
        return object()


class _Bundle:
    """A portfolio pair, each with a borrower, facility and evidence item."""

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.admin = Principal.user(uuid4(), (Permission.MANAGE_USERS,))
        self._user(self.admin.id, "admin-user")

        self.portfolio_a = self._portfolio("PF-A")
        self.portfolio_b = self._portfolio("PF-B")
        self.borrower_a = self._borrower(self.portfolio_a, "B-A0001")
        self.borrower_b = self._borrower(self.portfolio_b, "B-B0001")
        self.evidence_a = self._evidence(self.borrower_a, suffix="a")
        self.evidence_b = self._evidence(self.borrower_b, suffix="b")
        self.session.commit()

        self.api_keys = ApiKeyService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-api-keys-0001",
        )
        self.lookup = SqlAlchemyApiKeyLookup(lambda: Session(self.engine))
        self.master_data_service = MasterDataService(
            self.session, audit=self.audit, clock=FixedClock(_NOW)
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def issue(self, *, portfolio_scope: list[str], rate_limit_per_min: int = 100) -> Any:
        issued = self.api_keys.issue(
            self.admin,
            name="service-integration",
            scopes=[Permission.VIEW_BORROWER, Permission.VIEW_EVIDENCE],
            portfolio_scope=portfolio_scope,
            rate_limit_per_min=rate_limit_per_min,
        )
        # `SqlAlchemyApiKeyLookup` authenticates through its own short-lived
        # session, exactly as a real deployment's request-scoped session
        # would after the issuing unit of work commits. Committing here
        # keeps the test honest about that boundary instead of relying on
        # same-session visibility that a real request would never have.
        self.session.commit()
        return issued

    def app_for(self, authenticator: ApiKeyAuthenticator) -> Any:
        cursor_secret = b"t-136-rate-limit-secret-32byte!"
        return create_app(
            routers=(
                create_borrowers_router(self.master_data_service),
                create_evidence_router(self.session, cursor_secret=cursor_secret),
            ),
            principal_resolver=RequestPrincipalResolver(api_keys=authenticator),
        )

    def _portfolio(self, code: str) -> Portfolio:
        portfolio = Portfolio.create(
            code=code, name=code, created_at=_NOW, updated_at=_NOW, request_id=f"rq-{code.lower()}"
        )
        self.session.add(portfolio)
        self.session.flush()
        return portfolio

    def _user(self, user_id: object, username: str) -> None:
        self.session.add(
            AppUser(
                id=user_id,
                username=username,
                email=f"{username}@example.test",
                full_name=username.title(),
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-user-{username}",
            )
        )
        self.session.flush()

    def _borrower(self, portfolio: Portfolio, reference: str) -> Borrower:
        borrower = Borrower(
            reference=reference,
            legal_name=f"Borrower {reference}",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-{reference.lower()}",
        )
        self.session.add(borrower)
        self.session.flush()
        facility = Facility(
            reference=f"F-{reference}",
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000.0000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-{reference.lower()}-facility",
        )
        self.session.add(facility)
        self.session.flush()
        return borrower

    def _evidence(self, borrower: Borrower, *, suffix: str) -> EvidenceItem:
        row = EvidenceItem(
            borrower_id=borrower.id,
            facility_id=None,
            family="payment",
            evidence_type="delay",
            first_seen=date(2026, 1, 1),
            last_seen=date(2026, 1, 15),
            state="sustained",
            counts_toward_pressure=True,
            source_event_ids=[f"evt-{suffix}"],
            last_scored_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-evidence-{suffix}",
        )
        self.session.add(row)
        self.session.flush()
        return row


def test_scoped_key_cannot_cross_portfolio_on_any_endpoint() -> None:
    bundle = _Bundle()
    try:
        issued = bundle.issue(portfolio_scope=[bundle.portfolio_a.path])
        authenticator = ApiKeyAuthenticator(bundle.lookup, clock=FixedClock(_NOW))
        app = bundle.app_for(authenticator)
        headers = {"Authorization": f"Bearer {issued.raw_key}"}

        with TestClient(app) as client:
            borrowers = client.get("/api/v1/borrowers", headers=headers)
            assert borrowers.status_code == 200
            references = {row["reference"] for row in borrowers.json()}
            assert references == {bundle.borrower_a.reference}

            cross_borrower = client.get(
                f"/api/v1/borrowers/{bundle.borrower_b.reference}", headers=headers
            )
            assert cross_borrower.status_code == 404

            evidence_list = client.get("/api/v1/evidence", headers=headers)
            assert evidence_list.status_code == 200
            evidence_ids = {row["id"] for row in evidence_list.json()}
            assert evidence_ids == {str(bundle.evidence_a.id)}

            cross_evidence = client.get(f"/api/v1/evidence/{bundle.evidence_b.id}", headers=headers)
            assert cross_evidence.status_code == 404
    finally:
        bundle.close()


def test_revocation_immediate() -> None:
    bundle = _Bundle()
    try:
        issued = bundle.issue(portfolio_scope=[bundle.portfolio_a.path])
        authenticator = ApiKeyAuthenticator(bundle.lookup, clock=FixedClock(_NOW))

        assert authenticator(issued.raw_key) is not None

        bundle.api_keys.revoke(
            bundle.admin, issued.id, expected_version=1, reason="key rotation drill"
        )
        bundle.session.commit()

        assert authenticator(issued.raw_key) is None
    finally:
        bundle.close()


def test_rate_limit_429_with_retry_after() -> None:
    bundle = _Bundle()
    try:
        issued = bundle.issue(portfolio_scope=[bundle.portfolio_a.path], rate_limit_per_min=1)
        authenticator = ApiKeyAuthenticator(bundle.lookup, clock=FixedClock(_NOW))
        app = bundle.app_for(authenticator)
        app.add_middleware(ApiKeyRateLimitMiddleware, lookup=bundle.lookup, audit=bundle.audit)
        headers = {"Authorization": f"Bearer {issued.raw_key}"}

        with TestClient(app) as client:
            first = client.get("/api/v1/borrowers", headers=headers)
            second = client.get("/api/v1/borrowers", headers=headers)

        assert first.status_code == 200
        assert second.status_code == 429
        assert int(second.headers["retry-after"]) > 0
        assert second.headers["x-ratelimit-limit"] == "1"
        assert second.headers["x-ratelimit-remaining"] == "0"
        assert any(
            event["event_type"] == "rate_limit_exceeded"
            and event["payload"]["category"] == "api_key"
            for event in bundle.audit.events
        )
    finally:
        bundle.close()


def test_revoked_key_is_not_masked_by_rate_limit() -> None:
    bundle = _Bundle()
    try:
        issued = bundle.issue(portfolio_scope=[bundle.portfolio_a.path], rate_limit_per_min=1)
        authenticator = ApiKeyAuthenticator(bundle.lookup, clock=FixedClock(_NOW))
        app = bundle.app_for(authenticator)
        app.add_middleware(ApiKeyRateLimitMiddleware, lookup=bundle.lookup, audit=bundle.audit)
        headers = {"Authorization": f"Bearer {issued.raw_key}"}

        with TestClient(app) as client:
            first = client.get("/api/v1/borrowers", headers=headers)
            assert first.status_code == 200

            bundle.api_keys.revoke(
                bundle.admin, issued.id, expected_version=1, reason="disable compromised key"
            )
            bundle.session.commit()

            revoked = client.get("/api/v1/borrowers", headers=headers)

        assert revoked.status_code == 401
        assert not any(
            event["event_type"] == "rate_limit_exceeded" for event in bundle.audit.events
        )
    finally:
        bundle.close()


def test_key_material_never_logged() -> None:
    bundle = _Bundle()
    try:
        issued = bundle.issue(portfolio_scope=[bundle.portfolio_a.path])
        rotated = bundle.api_keys.rotate(bundle.admin, issued.id, expected_version=1)
        bundle.api_keys.revoke(
            bundle.admin, issued.id, expected_version=2, reason="compromised credential"
        )

        secrets_to_check = [issued.raw_key, rotated.raw_key]

        audit_blob = json.dumps(
            [{k: v for k, v in event.items() if k != "subject"} for event in bundle.audit.events],
            default=str,
        )
        for secret in secrets_to_check:
            assert secret not in audit_blob

        persisted = bundle.session.get(ApiKey, issued.id)
        assert persisted is not None
        for secret in secrets_to_check:
            assert secret != persisted.key_hash
            assert secret not in persisted.key_hash
    finally:
        bundle.close()
