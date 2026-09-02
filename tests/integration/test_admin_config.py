"""Integration coverage for T-114's administrator configuration surface."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.config.thresholds import ThresholdStore
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.forecast import ForecastRun, TriageEntry
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.maker_checker import MakerCheckerRequest
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.thresholds import SqlAlchemyThresholdRepository
from covenant_radar.domain.interventions.catalogue import CatalogueEntry
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.catalogue import CatalogueService
from covenant_radar.web.routes.admin import create_admin_config_router

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        _subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, dict(payload)))
        return object()


class _World:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.maker_id = new_id()
        self.checker_id = new_id()
        self.maker = Principal.user(self.maker_id, (Permission.PROPOSE_THRESHOLDS,))
        self.checker = Principal.user(
            self.checker_id,
            (Permission.PROPOSE_THRESHOLDS, Permission.APPROVE_THRESHOLDS),
        )
        self.session.add_all(
            [
                self._user(self.maker_id, "maker", "T114 Maker"),
                self._user(self.checker_id, "checker", "T114 Checker"),
            ]
        )
        self.session.flush()
        self.audit = _Audit()
        self.store = ThresholdStore(
            SqlAlchemyThresholdRepository(self.session),
            clock=FixedClock(_NOW),
            request_id="rq-t114-threshold",
        )
        self.catalogue = CatalogueService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t114-catalogue",
        )
        self.router = create_admin_config_router(
            self.session,
            threshold_store=self.store,
            catalogue_service=self.catalogue,
        )
        self.current_principal = self.maker

    @staticmethod
    def _user(user_id, username: str, full_name: str) -> AppUser:
        return AppUser(
            id=user_id,
            username=username,
            email=f"{username}@example.test",
            full_name=full_name,
            auth_source="local",
            is_active=True,
            failed_attempts=0,
            must_change_password=False,
            locale="en",
            theme="light",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-{username}",
        )

    def client(self) -> TestClient:
        app = create_app(
            routers=(self.router,),
            principal_resolver=lambda _request: self.current_principal,
        )
        return TestClient(app, follow_redirects=False)

    def add_completed_run(self) -> tuple[ForecastRun, TriageEntry]:
        portfolio = Portfolio.create(
            code="T114-PORTFOLIO",
            name="T114 Portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t114-portfolio",
        )
        borrower = Borrower(
            id=new_id(),
            reference="T114-BORROWER",
            legal_name="T114 Borrower Private Limited",
            portfolio_id=portfolio.id,
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t114-borrower",
        )
        run = ForecastRun(
            id=new_id(),
            as_of_date=date(2026, 8, 30),
            threshold_snapshot_id=self.store.snapshot_id(),
            started_at=_NOW - timedelta(hours=1),
            finished_at=_NOW,
            covenant_count=1,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t114-run",
        )
        entry = TriageEntry(
            id=new_id(),
            run_id=run.id,
            borrower_id=borrower.id,
            probability=Decimal("0.50"),
            confidence=Decimal("0.90"),
            band="amber",
            rank=1,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t114-triage",
        )
        self.session.add_all([portfolio, borrower, run, entry])
        self.session.flush()
        return run, entry

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def world() -> Iterator[_World]:
    value = _World()
    try:
        yield value
    finally:
        value.close()


def test_preview_shows_band_change_count(world: _World) -> None:
    world.add_completed_run()

    with world.client() as client:
        response = client.post(
            "/admin/config/thresholds/preview",
            data={"values": '{"T1": {"amber": "0.55"}}', "note": "Recalibration"},
        )

    assert response.status_code == 200
    assert 'data-testid="band-change-count"' in response.text
    assert "1 of 1 borrower would change band" in response.text
    assert "amber → watch" in response.text
    assert world.session.scalar(select(MakerCheckerRequest)) is None


def test_invariant_violation_refused_before_submission(world: _World) -> None:
    with world.client() as client:
        response = client.post(
            "/admin/config/thresholds/preview",
            data={"values": '{"T1": {"amber": "0.80"}}', "note": "Invalid"},
        )

    assert response.status_code == 422
    assert "T1 invariant: amber must not exceed act" in response.text
    assert world.session.scalar(select(MakerCheckerRequest)) is None


def test_approval_applies_at_next_run_not_retroactively(world: _World) -> None:
    old_run, _entry = world.add_completed_run()

    with world.client() as client:
        submitted = client.post(
            "/admin/thresholds",
            data={"values": '{"T1": {"act": "0.65"}}', "note": "Updated calibration"},
        )
        assert submitted.status_code == 303
        proposal = world.session.scalar(select(MakerCheckerRequest))
        assert proposal is not None

        world.current_principal = world.checker
        approved = client.post(f"/admin/thresholds/proposals/{proposal.id}/approve")

    assert approved.status_code == 303
    world.session.expire_all()
    snapshot_id = world.store.snapshot_id()
    assert snapshot_id != old_run.threshold_snapshot_id
    assert (
        world.session.get(ForecastRun, old_run.id).threshold_snapshot_id
        == old_run.threshold_snapshot_id
    )

    next_run = ForecastRun(
        id=new_id(),
        as_of_date=date(2026, 8, 31),
        threshold_snapshot_id=snapshot_id,
        started_at=_NOW,
        finished_at=_NOW + timedelta(hours=1),
        covenant_count=1,
        state="complete",
        created_at=_NOW,
        updated_at=_NOW + timedelta(hours=1),
        request_id="rq-t114-next-run",
    )
    world.session.add(next_run)
    world.session.flush()

    with world.client() as client:
        page = client.get("/admin/config")

    assert page.status_code == 200
    assert 'data-testid="threshold-application"' in page.text
    assert str(next_run.id) in page.text


def test_catalogue_entry_without_effect_model_refused(world: _World) -> None:
    with world.client() as client:
        response = client.post(
            "/admin/config/catalogue",
            data={
                "id": "NO-EFFECT",
                "role_tag": "credit",
                "text": "Review the account.",
                "effect_parameters": '{"amount": "-0.10"}',
                "applicable_covenant_classes": "leverage",
                "assumptions": "The action is approved.",
            },
        )

    assert response.status_code == 422
    assert "effect model" in response.text.lower()
    assert world.catalogue.find("NO-EFFECT") is None
    assert world.session.scalar(
        select(MakerCheckerRequest).where(
            MakerCheckerRequest.operation == "intervention_catalogue_change"
        )
    ) is None


def test_changes_audited_with_actor_and_approver(world: _World) -> None:
    entry = CatalogueEntry(
        id="AUDITED-ACTION",
        role_tag="credit",
        text="Review the account.",
        effect_model="level_shift",
        effect_parameters={"amount": "-0.10"},
        applicable_covenant_classes=("leverage",),
        assumptions=("The action is approved.",),
        requires_approval=True,
    )

    with world.client() as client:
        submitted = client.post(
            "/admin/config/catalogue",
            data={
                "id": entry.id,
                "role_tag": entry.role_tag.value,
                "text": entry.text,
                "effect_model": entry.effect_model.value,
                "effect_parameters": '{"amount": "-0.10"}',
                "applicable_covenant_classes": "leverage",
                "assumptions": "The action is approved.",
                "requires_approval": "true",
            },
        )
        assert submitted.status_code == 303
        proposal = world.session.scalar(
            select(MakerCheckerRequest).where(
                MakerCheckerRequest.operation == "intervention_catalogue_change"
            )
        )
        assert proposal is not None

        world.current_principal = world.checker
        decided = client.post(
            f"/admin/config/catalogue/approvals/{proposal.id}",
            data={"decision": "approve", "reason": "Reviewed by risk control"},
        )

    assert decided.status_code == 303
    event = next(
        payload
        for event_type, payload in world.audit.events
        if event_type == "maker_checker_approved"
    )
    assert event["maker_id"] == str(world.maker_id)
    assert event["checker_id"] == str(world.checker_id)
    assert event["reason"] == "Reviewed by risk control"
