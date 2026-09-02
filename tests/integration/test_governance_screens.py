"""Integration coverage for T-081's governance workspace: thresholds, the
model registry, drift, and the evaluation scoreboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.maker_checker import MakerCheckerRequest
from covenant_radar.db.models.operations import DriftObservation, EvaluationRun, ModelRegistration
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.governance import create_governance_router

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _World:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.risk_head_id = uuid4()
        self.risk_id = uuid4()
        self.risk_head = Principal.user(
            self.risk_head_id,
            (Permission.PROPOSE_THRESHOLDS, Permission.APPROVE_THRESHOLDS),
        )
        self.risk = Principal.user(self.risk_id, (Permission.PROPOSE_THRESHOLDS,))
        self.session.add_all(
            [
                AppUser(
                    id=self.risk_head_id,
                    username="t081-risk-head",
                    email="t081-risk-head@example.com",
                    full_name="T081 Risk Head",
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-t081-risk-head",
                ),
                AppUser(
                    id=self.risk_id,
                    username="t081-risk",
                    email="t081-risk@example.com",
                    full_name="T081 Risk Officer",
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-t081-risk",
                ),
            ]
        )
        self.session.flush()

    def client(self, *, principal: Principal) -> TestClient:
        app = create_app(
            routers=(create_governance_router(self.session),),
            principal_resolver=lambda _request: principal,
        )
        return TestClient(app)

    def add_proposal(self, *, maker_id, note: str = "Calibration review.") -> MakerCheckerRequest:
        proposal_id = new_id()
        request = MakerCheckerRequest(
            id=proposal_id,
            subject_type="threshold_change",
            subject_id=proposal_id,
            operation="threshold_change",
            payload={
                "before": {"T1": {"act": "0.70", "amber": "0.40"}},
                "after": {"T1": {"act": "0.70", "amber": "0.45"}},
                "base_snapshot_id": str(uuid4()),
                "note": note,
            },
            maker_id=maker_id,
            state="pending",
            created_at=_NOW,
            updated_at=_NOW,
            created_by_id=maker_id,
            updated_by_id=maker_id,
            request_id="rq-t081-proposal",
        )
        self.session.add(request)
        self.session.flush()
        return request

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_pending_change_shows_proposer_and_hides_self_approval() -> None:
    world = _World()
    try:
        own_proposal = world.add_proposal(maker_id=world.risk_head_id, note="My own change.")
        other_proposal = world.add_proposal(maker_id=world.risk_id, note="A colleague's change.")

        with world.client(principal=world.risk_head) as client:
            response = client.get("/governance")

        assert response.status_code == 200
        body = response.text
        # Both pending proposals are shown, and each names its proposer.
        assert "T081 Risk Head" in body
        assert "T081 Risk Officer" in body
        # The viewer proposed `own_proposal`: no approve control for it, and
        # the screen says why.
        own_index = body.index(f'data-testid="pending-proposal-{own_proposal.id}"')
        own_section = body[own_index : own_index + 1500]
        assert f'approve-proposal-{own_proposal.id}' not in own_section
        assert "You proposed this change" in own_section
        # A colleague's proposal is approvable by this distinct risk head.
        other_index = body.index(f'data-testid="pending-proposal-{other_proposal.id}"')
        other_section = body[other_index : other_index + 1500]
        assert f'approve-proposal-{other_proposal.id}' in other_section
    finally:
        world.close()


def test_default_threshold_references_calibration_record() -> None:
    world = _World()
    try:
        with world.client(principal=world.risk_head) as client:
            response = client.get("/governance")

        assert response.status_code == 200
        body = response.text
        assert 'data-testid="calibration-reference"' in body
        assert "shipped default" in body
        assert "config/thresholds.default.json" in body
        # No history exists yet: only the shipped default appears.
        assert "No prior threshold snapshots are recorded." in body
    finally:
        world.close()


def test_no_evaluation_run_empty_state() -> None:
    world = _World()
    try:
        with world.client(principal=world.risk_head) as client:
            response = client.get("/governance")

        assert response.status_code == 200
        assert "No evaluation run has been recorded for any release yet." in response.text
        # Never a fabricated score.
        assert "Passed" not in response.text
        assert "Failed" not in response.text
    finally:
        world.close()


def test_drift_breach_shows_metric_window_and_rollback() -> None:
    world = _World()
    try:
        world.session.add(
            ModelRegistration(
                component="forecast_challenger",
                provider="internal",
                model_id="statistical-v2",
                state="retired",
                owner_id=world.risk_head_id,
                approved_by_id=world.risk_head_id,
                approved_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t081-model",
            )
        )
        window_start = _NOW - timedelta(days=7)
        world.session.add(
            DriftObservation(
                component="forecast_challenger",
                metric="calibration_error",
                window_start=window_start,
                window_end=_NOW,
                value=Decimal("0.22"),
                baseline=Decimal("0.10"),
                breached=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t081-drift",
            )
        )
        world.session.flush()

        with world.client(principal=world.risk_head) as client:
            response = client.get("/governance")

        assert response.status_code == 200
        body = response.text
        assert 'data-testid="drift-breach-alert"' in body
        assert "forecast_challenger" in body
        assert "calibration_error" in body
        assert "Rolled back — retired to the prior version" in body
    finally:
        world.close()


def test_unapproved_model_flagged() -> None:
    world = _World()
    try:
        world.session.add(
            ModelRegistration(
                component="extraction_stage1",
                provider="internal",
                model_id="stage1-v3",
                state="registered",
                owner_id=world.risk_head_id,
                approved_by_id=None,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t081-unapproved",
            )
        )
        world.session.flush()

        with world.client(principal=world.risk_head) as client:
            response = client.get("/governance")

        assert response.status_code == 200
        body = response.text
        assert 'data-testid="unapproved-models-alert"' in body
        assert "extraction_stage1" in body
        assert "Unapproved — not promotable" in body
    finally:
        world.close()


def test_scoreboard_shows_both_arms() -> None:
    world = _World()
    try:
        world.session.add_all(
            [
                EvaluationRun(
                    commit_sha="abc1234",
                    arm="baseline",
                    scores={"extraction_f1": "0.90"},
                    passed=False,
                    executed_at=_NOW,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-t081-eval-baseline",
                ),
                EvaluationRun(
                    commit_sha="abc1234",
                    arm="product",
                    scores={"extraction_f1": "0.97"},
                    passed=True,
                    executed_at=_NOW,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-t081-eval-product",
                ),
            ]
        )
        world.session.flush()

        with world.client(principal=world.risk_head) as client:
            response = client.get("/governance")

        assert response.status_code == 200
        body = response.text
        release_index = body.index("Release abc1234")
        release_section = body[release_index : release_index + 2000]
        assert "baseline" in release_section
        assert "product" in release_section
        assert "Passed" in release_section
        assert "Failed" in release_section
    finally:
        world.close()
