"""Integration coverage for T-080's audit search and reconstruction screens."""

from __future__ import annotations

import csv
import html
import io
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent, ThresholdSnapshot
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastRun
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.operations import RetentionPurgeLog
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.reconstruction import ReconstructionService
from covenant_radar.web.routes.audit import create_audit_router

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
        self.principal_id = uuid4()
        self.other_actor_id = uuid4()
        self.principal = Principal.user(
            self.principal_id,
            (Permission.VIEW_AUDIT, Permission.EXPORT_EVIDENCE),
        )
        self.portfolio = Portfolio.create(
            code="AUDIT-ROOT",
            name="Audit screen portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t080-portfolio",
        )
        self.session.add(self.portfolio)
        self.session.add_all(
            [
                AppUser(
                    id=self.principal_id,
                    username="t080-auditor",
                    email="t080-auditor@example.com",
                    full_name="T080 Auditor",
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-t080-auditor",
                ),
                AppUser(
                    id=self.other_actor_id,
                    username="t080-operator",
                    email="t080-operator@example.com",
                    full_name="T080 Operator",
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-t080-operator",
                ),
            ]
        )
        self.session.flush()
        self.borrower = Borrower(
            reference="B-T080",
            legal_name="T080 Screen Borrower Private Limited",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t080-borrower",
        )
        self.session.add(self.borrower)
        self.session.flush()
        self.facility = Facility(
            reference="F-T080",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t080-facility",
        )
        self.session.add(self.facility)
        self.session.flush()
        self.covenant = Covenant(
            reference="CV-T080",
            facility_id=self.facility.id,
            name="Minimum DSCR",
            covenant_class="financial",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t080-covenant",
        )
        self.session.add(self.covenant)
        self.session.flush()
        self.covenant_version = CovenantVersion(
            covenant_id=self.covenant.id,
            version_no=1,
            threshold=Decimal("1.20"),
            direction="min",
            unit="x",
            frequency="quarterly",
            test_basis="trailing_12m",
            effective_from=date(2025, 1, 1),
            warning_headroom_pct=Decimal("10"),
            cure_days=30,
            grace_days=15,
            status="live",
            registered_by_id=self.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t080-covenant-version",
        )
        self.session.add(self.covenant_version)
        self.session.flush()
        self.threshold_snapshot = ThresholdSnapshot(
            values={"T1": {"act": "0.70"}},
            source="config",
            effective_from=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t080-thresholds",
        )
        self.session.add(self.threshold_snapshot)
        self.session.flush()
        self.run = ForecastRun(
            as_of_date=_NOW.date(),
            threshold_snapshot_id=self.threshold_snapshot.id,
            model_version="forecast.v1",
            started_at=_NOW,
            finished_at=_NOW,
            covenant_count=1,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t080-run",
        )
        self.session.add(self.run)
        self.session.flush()
        self.forecast = Forecast(
            run_id=self.run.id,
            covenant_version_id=self.covenant_version.id,
            horizon_days=90,
            probability=Decimal("0.35"),
            confidence=Decimal("0.80"),
            below_confidence_floor=False,
            projected_cross_date=date(2026, 10, 1),
            direction="min",
            formula_inputs={"candidate_inputs": {"dscr": "1.35"}},
            data_as_of=_NOW.date(),
            staleness_days=0,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t080-forecast",
        )
        self.session.add(self.forecast)
        self.session.flush()
        self.scope = Scope.from_paths(self.principal_id, [self.portfolio.path])
        self.service = ReconstructionService(self.session, clock=FixedClock(_NOW))

    def client(self, *, principal: Principal | None = None) -> TestClient:
        app = create_app(
            routers=(
                create_audit_router(
                    self.service,
                    scope_resolver=lambda _principal: self.scope,
                    cursor_secret=b"t080-audit-cursor-secret-32-bytes!",
                ),
            ),
            principal_resolver=lambda _request: principal or self.principal,
        )
        return TestClient(app)

    def record_events(
        self, count: int = 5, *, event_type: str = "forecast_candidate_scored"
    ) -> None:
        recorder = AuditRecorder(
            AuditRepository(self.session),
            clock=FixedClock(_NOW),
            request_id="rq-t080-events",
        )
        for offset in range(count):
            recorder.record(
                event_type,
                ("forecast", self.forecast.id),
                {"offset": offset},
                actor=self.principal_id,
                occurred_at=_NOW + timedelta(minutes=offset),
            )
        self.session.flush()

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_no_state_changing_control_for_auditor() -> None:
    world = _World()
    try:
        world.record_events(1)
        with world.client() as client:
            response = client.get(f"/audit/warnings/{world.forecast.id}")

        assert response.status_code == 200
        assert 'data-action="evidence-export"' in response.text
        assert 'name="edit"' not in response.text
        assert 'name="delete"' not in response.text
        assert 'name="approve"' not in response.text
        assert 'method="post" action="/audit/' in response.text
    finally:
        world.close()


def test_search_filters_and_stable_pagination() -> None:
    world = _World()
    try:
        world.record_events(5)
        recorder = AuditRecorder(
            AuditRepository(world.session), clock=FixedClock(_NOW), request_id="rq-t080-other"
        )
        recorder.record(
            "memo_generated",
            ("forecast", world.forecast.id),
            {"noise": True},
            actor=world.other_actor_id,
            occurred_at=_NOW + timedelta(days=1),
        )
        world.session.flush()
        params = {
            "actor": str(world.principal_id),
            "subject": str(world.forecast.id),
            "subject_type": "forecast",
            "event_type": "forecast_candidate_scored",
            "from_date": _NOW.date().isoformat(),
            "to_date": _NOW.date().isoformat(),
            "page_size": "2",
        }
        with world.client() as client:
            first = client.get("/audit", params=params)
            next_href = _next_href(first.text)
            second = client.get(next_href)
            third_href = _next_href(second.text)
            third = client.get(third_href) if third_href else None

        assert first.status_code == second.status_code == 200
        assert third is not None and third.status_code == 200
        sequences = _sequences(first.text) + _sequences(second.text) + _sequences(third.text)
        assert sequences == [5, 4, 3, 2, 1]
        assert all("memo_generated" not in response.text for response in (first, second, third))
    finally:
        world.close()


def test_timeline_shows_every_part_in_order() -> None:
    world = _World()
    try:
        with world.client() as client:
            response = client.get(f"/audit/warnings/{world.forecast.id}")

        assert response.status_code == 200
        positions = [
            response.text.index(f'data-part="{part}"')
            for part in (
                "source_data",
                "formula_inputs",
                "covenant_version",
                "thresholds",
                "calculation",
                "trend",
                "forecast",
                "evidence",
                "drivers",
                "memo",
                "overrides",
                "dispositions",
            )
        ]
        assert positions == sorted(positions)
        assert response.text.count("Provenance") == 12
    finally:
        world.close()


def test_purged_part_named_with_rule() -> None:
    world = _World()
    try:
        document_id = uuid4()
        world.covenant_version.source_document_id = document_id
        world.session.add(
            RetentionPurgeLog(
                entity="document",
                criteria={"entity_id": str(document_id), "rule": "statutory_7y"},
                purged_count=1,
                executed_at=_NOW,
                executed_by="retention-job",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t080-purge",
            )
        )
        world.session.flush()
        with world.client() as client:
            response = client.get(f"/audit/warnings/{world.forecast.id}")

        assert response.status_code == 200
        assert "Purged" in response.text
        assert "statutory_7y" in response.text
    finally:
        world.close()


def test_chain_failure_shown_prominently() -> None:
    world = _World()
    try:
        world.record_events(2)
        first = world.session.query(AuditEvent).order_by(AuditEvent.sequence).first()
        assert first is not None
        first.hash = "tampered"
        world.session.flush()
        with world.client() as client:
            response = client.get(f"/audit/warnings/{world.forecast.id}")

        assert response.status_code == 200
        alert_position = response.text.index('class="panel audit-chain audit-chain--failed"')
        summary_position = response.text.index('id="warning-summary-title"')
        assert "Audit chain verification failed" in response.text
        assert alert_position < summary_position
    finally:
        world.close()


def test_export_audited_with_filter_and_count() -> None:
    world = _World()
    try:
        world.record_events(3)
        with world.client() as client:
            response = client.get(
                "/audit/export",
                params={
                    "actor": str(world.principal_id),
                    "event_type": "forecast_candidate_scored",
                },
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 3
        export_event = world.session.query(AuditEvent).order_by(AuditEvent.sequence.desc()).first()
        assert export_event is not None
        assert export_event.payload["row_count"] == 3
        assert export_event.payload["filters"]["event_type"] == "forecast_candidate_scored"
    finally:
        world.close()


def _next_href(body: str) -> str | None:
    marker = '<a class="button" href="/audit?'
    start = body.find(marker)
    if start < 0:
        return None
    value_start = start + len('<a class="button" href="')
    value_end = body.find('"', value_start)
    href = html.unescape(body[value_start:value_end])
    parsed = urlparse(href)
    assert parsed.path == "/audit"
    assert parse_qs(parsed.query)["cursor"]
    return href


def _sequences(body: str) -> list[int]:
    matches = re.findall(
        r'<tr class="ledger-row"[^>]*>\s*<td class="ledger-row__cell">(\d+)</td>',
        body,
    )
    return [int(sequence) for sequence in matches]
