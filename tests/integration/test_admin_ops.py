"""Integration coverage for T-115's administrator operations surface."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from covenant_radar.asgi import create_app
from covenant_radar.config.capabilities import Capabilities, Capability
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import ConfigVersion
from covenant_radar.db.models.operations import JobRun, RetentionPurgeLog
from covenant_radar.scheduler.jobs import (
    InterruptionPolicy,
    JobDefinition,
    JobPolicy,
    JobRegistry,
    RetryPolicy,
)
from covenant_radar.scheduler.ledger import JobLedger
from covenant_radar.scheduler.runner import JobRunner
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.admin import create_admin_ops_router
from covenant_radar.web.view_models.admin_ops import RetentionPolicy, preview_retention

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


def _policy() -> JobPolicy:
    return JobPolicy(
        retry=RetryPolicy(max_attempts=1),
        interruption=InterruptionPolicy.RESTART,
        timeout_seconds=5,
    )


def _capabilities(*, model_provider: bool = False) -> Capabilities:
    return Capabilities(
        model_provider=Capability(model_provider, "none"),
        sso=Capability(False, "none"),
        ocr=Capability(False, "not configured"),
        smtp=Capability(False, "not configured"),
        webhooks=Capability(False, "not configured"),
        document_store=Capability(False, "none"),
    )


class _World:
    def __init__(self, tmp_path) -> None:
        self.engine = create_engine(
            f"sqlite:///{tmp_path / 'admin-ops.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.session_factory()
        self.principal = Principal.user(uuid4(), (Permission.MANAGE_JOBS,))
        self.audit = _Audit()
        self.registry = JobRegistry()
        self.runner = JobRunner(
            self.registry,
            self.session_factory,
            clock=FixedClock(_NOW),
            max_workers=1,
        )
        self.runtime = SimpleNamespace(registry=self.registry, runner=self.runner)
        self.app = create_app(
            routers=(
                create_admin_ops_router(
                    self.session,
                    runtime=self.runtime,
                    capabilities=_capabilities(),
                    audit=self.audit,
                    clock=FixedClock(_NOW),
                ),
            ),
            principal_resolver=lambda _request: self.principal,
        )

    def client(self) -> TestClient:
        return TestClient(self.app, follow_redirects=False)

    def add_job(self, name: str, handler) -> JobDefinition:
        definition = JobDefinition(name=name, handler=handler, policy=_policy())
        self.registry.register(definition)
        return definition

    def close(self) -> None:
        self.runner.shutdown(grace_period_seconds=0)
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def world(tmp_path) -> Iterator[_World]:
    value = _World(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_manual_trigger_refused_while_running(world: _World) -> None:
    definition = world.add_job("nightly", lambda _context: {"ok": True})
    running = JobLedger(world.session).start_or_refuse(
        definition,
        trigger="scheduled",
        started_at=_NOW,
        request_id="rq-running",
    )
    world.session.commit()

    with world.client() as client:
        response = client.post("/admin/jobs/nightly/run")

    assert response.status_code == 409
    assert running.run_id in response.json()["error"]
    assert "already running" in response.json()["error"]
    assert len(world.session.scalars(select(JobRun)).all()) == 1


def test_retry_links_to_failed_run(world: _World) -> None:
    attempts: list[int] = []

    def handler(context) -> dict[str, object]:
        attempts.append(context.attempt)
        if context.attempt == 1:
            raise RuntimeError("test failure")
        return {"retried": True}

    definition = world.add_job("flaky", handler)
    failed = world.runner.run_now(definition.name, trigger="manual", actor_id=world.principal.id)
    assert failed.state == "failed"

    with world.client() as client:
        response = client.post(
            "/admin/jobs/flaky/run",
            data={"retry_run_id": str(failed.id)},
        )

    assert response.status_code == 202
    assert response.json()["run_id"] == failed.run_id
    deadline = time.monotonic() + 2
    rows: list[JobRun] = []
    while time.monotonic() < deadline:
        world.session.expire_all()
        rows = list(
            world.session.scalars(
                select(JobRun).where(JobRun.job_name == "flaky").order_by(JobRun.attempt)
            ).all()
        )
        # The retry runs on the job runner's own thread, so the second row
        # exists as `running` before it reaches a terminal state.  Waiting
        # only for the row to appear made this assert whatever state the
        # scheduler happened to be in; wait for the attempt to actually end.
        if len(rows) == 2 and rows[1].state in {"succeeded", "failed"}:
            break
        time.sleep(0.01)

    assert attempts == [1, 2]
    assert len(rows) == 2
    assert rows[0].state == "failed"
    assert rows[1].state == "succeeded"
    assert rows[1].run_id == rows[0].run_id
    assert rows[1].attempt == 2


def test_unconfigured_capability_shown_not_errored(world: _World) -> None:
    with world.client() as client:
        response = client.get("/admin/jobs")

    assert response.status_code == 200
    assert 'data-testid="capability-model-provider"' in response.text
    assert "not configured" in response.text
    assert "state--error" not in response.text
    assert 'data-testid="retention-preview"' in response.text


def test_retention_change_previews_counts(world: _World) -> None:
    old = _NOW - timedelta(days=400)
    world.session.add(
        JobRun(
            id=uuid4(),
            job_name="old-job",
            run_id="jr-old-job",
            trigger="scheduled",
            started_at=old,
            finished_at=old + timedelta(minutes=1),
            state="succeeded",
            attempt=1,
            error=None,
            metrics=None,
            created_at=old,
            updated_at=old,
            request_id="rq-old-job",
        )
    )
    world.session.commit()

    with world.client() as client:
        response = client.post(
            "/admin/jobs",
            data={
                "action": "preview_retention",
                "regulatory_period_years": "9",
                "logs_min_days": "180",
                "raw_signal_months": "24",
                "forecast_months": "24",
                "notification_months": "12",
                "quarantine_days": "90",
            },
        )

    assert response.status_code == 200
    assert 'data-testid="retention-preview"' in response.text
    assert 'data-testid="retention-count-job-runs"' in response.text
    assert world.session.scalar(select(RetentionPurgeLog.id)) is None

    policy = RetentionPolicy(regulatory_period_years=9)
    preview = preview_retention(world.session, policy, clock=FixedClock(_NOW))
    with world.client() as client:
        applied = client.post(
            "/admin/jobs",
            data={
                "action": "apply_retention",
                "retention": json.dumps(policy.as_dict()),
                "preview_token": preview.token,
            },
        )

    assert applied.status_code == 303
    config = world.session.scalar(select(ConfigVersion).order_by(ConfigVersion.version.desc()))
    assert config is not None
    assert config.values_redacted["retention"] == policy.as_dict()
    assert world.audit.events[-1][0] == "retention_policy_changed"


def test_purge_log_not_rerunnable(world: _World) -> None:
    world.add_job("retention.purge", lambda _context: {"purged": 1})
    world.session.add(
        RetentionPurgeLog(
            id=uuid4(),
            entity="notifications",
            criteria={"older_than_days": 365},
            purged_count=4,
            executed_at=_NOW,
            executed_by="retention-job",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="jr-retention",
        )
    )
    world.session.commit()

    with world.client() as client:
        page = client.get("/admin/jobs")
        response = client.post("/admin/jobs/retention.purge/run")

    assert page.status_code == 200
    assert "retention-job" in page.text
    assert "cannot be re-run" in page.text
    assert response.status_code == 409
    assert "cannot be re-run" in response.json()["error"]
