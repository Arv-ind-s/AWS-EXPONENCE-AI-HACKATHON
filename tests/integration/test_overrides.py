"""Integration coverage for T-111 risk-view override capture."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import NotFound, ValidationError
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    Covenant,
    CovenantVersion,
    Facility,
    Forecast,
    ForecastRun,
    OverrideRecord,
    Portfolio,
    ThresholdSnapshot,
    TraceRow,
)
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.overrides import OverrideService
from covenant_radar.web.routes.overrides import create_overrides_router

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object, str]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, dict(payload), actor, request_id))
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.principal = Principal.user(
            uuid4(),
            (Permission.VIEW_BORROWER, Permission.OVERRIDE_RISK_VIEW),
        )
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="t111-user",
                email="t111-user@example.com",
                full_name="T111 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t111-user",
            )
        )
        self.portfolio = Portfolio.create(
            code="T111",
            name="T111 portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-portfolio",
        )
        self.session.add(self.portfolio)
        self.session.flush()
        self.borrower = Borrower(
            reference="B-T111",
            legal_name="T111 Borrower Private Limited",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-borrower",
        )
        self.session.add(self.borrower)
        self.session.flush()
        self.facility = Facility(
            reference="F-T111",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            outstanding=Decimal("700"),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-facility",
        )
        self.session.add(self.facility)
        self.session.flush()
        self.covenant = Covenant(
            reference="CV-T111",
            facility_id=self.facility.id,
            name="Leverage ratio",
            covenant_class="financial",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-covenant",
        )
        self.session.add(self.covenant)
        self.session.flush()
        self.version = CovenantVersion(
            covenant_id=self.covenant.id,
            version_no=1,
            threshold=Decimal("2.50"),
            direction="max",
            unit="x",
            frequency="quarterly",
            test_basis="standalone",
            effective_from=date(2025, 1, 1),
            status="live",
            tested_at_least_once=False,
            registered_by_id=self.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-version",
        )
        self.session.add(self.version)
        self.session.flush()
        self.snapshot = ThresholdSnapshot(
            values={"T2": {"confidence_floor": "0.50"}},
            source="config",
            effective_from=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-snapshot",
        )
        self.session.add(self.snapshot)
        self.session.flush()
        self.run = ForecastRun(
            as_of_date=date(2026, 8, 31),
            threshold_snapshot_id=self.snapshot.id,
            model_version="forecast.v1",
            started_at=_NOW,
            finished_at=_NOW,
            covenant_count=1,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-run",
        )
        self.session.add(self.run)
        self.session.flush()
        self.forecast = Forecast(
            run_id=self.run.id,
            covenant_version_id=self.version.id,
            horizon_days=30,
            probability=Decimal("0.3500"),
            confidence=Decimal("0.8000"),
            below_confidence_floor=False,
            projected_cross_date=date(2026, 10, 1),
            direction="max",
            formula_inputs={"band": "amber"},
            data_as_of=date(2026, 8, 31),
            staleness_days=0,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-forecast",
        )
        self.session.add(self.forecast)
        self.session.flush()
        self.session.add(
            TraceRow(
                subject_type="forecast",
                subject_id=self.forecast.id,
                stage="6",
                decider="code",
                inputs={"band": "amber"},
                outputs={"band": "amber", "rank": 1},
                rule_or_prompt_version="triage.v1",
                thresholds_compared=[],
                confidence=Decimal("0.8000"),
                sources=[{"type": "forecast", "id": str(self.forecast.id)}],
                occurred_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t111-trace",
            )
        )
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.service = OverrideService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t111-service",
            scope_resolver=lambda _principal: self.scope,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def _record(
    fixture: _Fixture,
    *,
    reason: str = "Risk officer reviewed the latest evidence.",
) -> OverrideRecord:
    return fixture.service.record_override(
        fixture.principal,
        ("forecast", fixture.forecast.id),
        stage=6,
        user_action="reclassify",
        user_value={"band": "red"},
        reason=reason,
        scope=fixture.scope,
    )


def test_missing_reason_refused_nothing_written() -> None:
    fixture = _Fixture()
    try:
        with pytest.raises(ValidationError, match="A reason is required"):
            fixture.service.record_override(
                fixture.principal,
                ("forecast", fixture.forecast.id),
                stage=6,
                user_action="reclassify",
                user_value={"band": "red"},
                reason="",
                scope=fixture.scope,
            )
        assert fixture.session.scalar(select(func.count(OverrideRecord.id))) == 0
        assert fixture.audit.events == []
    finally:
        fixture.close()


def test_both_states_reconstructable() -> None:
    fixture = _Fixture()
    try:
        record = _record(fixture)
        assert fixture.service.repository.get(record.id, scope=fixture.scope) is record
        assert fixture.service.repository.list(scope=fixture.scope) == (record,)
        assert record.shown is not None
        assert record.shown["outputs"] == {"band": "amber", "rank": 1}
        assert record.user_value == {"band": "red"}
        assert record.threshold_snapshot_id == fixture.snapshot.id
        assert record.model_version == "forecast.v1"
        revised = fixture.service.current_view(
            fixture.principal,
            ("forecast", fixture.forecast.id),
            scope=fixture.scope,
        )
        assert revised.original["outputs"] == {"band": "amber", "rank": 1}
        assert revised.current["band"] == "red"
        assert fixture.forecast.probability == Decimal("0.3500")
    finally:
        fixture.close()


def test_second_override_shown_sequence_retained() -> None:
    fixture = _Fixture()
    try:
        first = _record(fixture)
        second = fixture.service.record_override(
            fixture.principal,
            ("forecast", fixture.forecast.id),
            stage=6,
            user_action="reclassify",
            user_value={"band": "watch"},
            reason="New evidence supports continued monitoring.",
            scope=fixture.scope,
        )
        history = fixture.service.list_overrides(
            fixture.principal,
            ("forecast", fixture.forecast.id),
            scope=fixture.scope,
        )
        assert tuple(item.id for item in history) == (first.id, second.id)
        assert second.shown is not None
        assert second.shown["band"] == "red"
        assert (
            fixture.service.current_view(
                fixture.principal,
                ("forecast", fixture.forecast.id),
                scope=fixture.scope,
            ).current["band"]
            == "watch"
        )
    finally:
        fixture.close()


def test_out_of_scope_404() -> None:
    fixture = _Fixture()
    try:
        hidden_portfolio = Portfolio.create(
            code="T111-HIDDEN",
            name="Hidden portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-hidden-portfolio",
        )
        hidden = Borrower(
            id=uuid4(),
            reference="B-T111-HIDDEN",
            legal_name="Hidden Borrower",
            portfolio_id=hidden_portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t111-hidden-borrower",
        )
        fixture.session.add_all([hidden_portfolio, hidden])
        fixture.session.flush()
        with pytest.raises(NotFound):
            fixture.service.record_override(
                fixture.principal,
                ("borrower", hidden.id),
                stage=3,
                user_action="reclassify",
                user_value={"band": "red"},
                reason="Hidden subject must not be enumerable.",
                scope=fixture.scope,
            )
        app = create_app(
            routers=(create_overrides_router(fixture.service),),
            principal_resolver=lambda _request: fixture.principal,
        )
        with TestClient(app) as client:
            response = client.post(
                "/overrides",
                data={
                    "subject": f"borrower:{hidden.id}",
                    "stage": "3",
                    "user_action": "reclassify",
                    "user_value": '{"band": "red"}',
                    "reason": "Hidden subject must not be enumerable.",
                },
                follow_redirects=False,
            )
        assert response.status_code == 404
        assert fixture.session.scalar(select(func.count(OverrideRecord.id))) == 0
    finally:
        fixture.close()


def test_reason_never_in_outbound_whitelist() -> None:
    fixture = _Fixture()
    try:
        sensitive_reason = "Discussed with Ananya Sharma about PAN ABCDE1234F."
        record = _record(fixture, reason=sensitive_reason)
        assert record.reason == sensitive_reason
        assert all("outbound" not in event[0] for event in fixture.audit.events)
        assert "reason" not in fixture.audit.events[-1][2]
        assert fixture.audit.events[-1][2]["reason_recorded"] is True
    finally:
        fixture.close()


def test_override_visible_on_case_file_and_why_panel() -> None:
    fixture = _Fixture()
    try:
        app = create_app(
            routers=(create_overrides_router(fixture.service),),
            principal_resolver=lambda _request: fixture.principal,
        )
        with TestClient(app) as client:
            response = client.get(
                f"/overrides/form/forecast/{fixture.forecast.id}",
                params={"stage": 6},
            )
            why_response = client.get(
                f"/why/forecast/{fixture.forecast.id}/override",
                params={"stage": 6},
            )
            post_response = client.post(
                "/overrides",
                data={
                    "subject": f"forecast:{fixture.forecast.id}",
                    "stage": "6",
                    "user_action": "reclassify",
                    "user_value": '{"band": "red"}',
                    "reason": "The route submission is fully captured locally.",
                },
                follow_redirects=False,
            )
        assert response.status_code == 200
        assert why_response.status_code == 200
        assert post_response.status_code == 303
        assert post_response.headers["location"] == f"/why/forecast/{fixture.forecast.id}"
        for body in (response.text, why_response.text):
            assert 'id="override-control"' in body
            assert 'name="reason"' in body
            assert 'data-surface="case-file why-panel"' in body
    finally:
        fixture.close()


__all__ = []
