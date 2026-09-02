"""Integration coverage for T-110's scoped case workspace."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    ActionTaken,
    AppUser,
    Borrower,
    Case,
    CaseComment,
    CaseEvent,
    ForecastRun,
    Intervention,
    Memo,
    Notification,
    Portfolio,
    UserPortfolioScope,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.cases import create_cases_router

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
        self.events.append((event_type, {**payload, "actor": actor, "request_id": request_id}))
        return object()


class _Fixture:
    def __init__(self, permissions: tuple[Permission, ...] | None = None) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.clock = FixedClock(_NOW)
        self.audit = _Audit()
        self.principal = Principal.user(
            uuid4(),
            permissions or (Permission.VIEW_CASE, Permission.UPDATE_CASE, Permission.LOG_ACTION),
        )
        self.user = self._user(self.principal.id, "analyst")
        self.portfolio = self._portfolio("T110")
        self.borrower = self._borrower("B-T110", self.portfolio)
        self.session.add(
            UserPortfolioScope(
                user_id=self.principal.id,
                portfolio_id=self.portfolio.id,
                include_descendants=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t110-scope",
            )
        )
        self.case = Case(
            id=new_id(),
            reference="C-T110",
            borrower_id=self.borrower.id,
            state="open",
            band_at_open="act",
            assignee_id=self.principal.id,
            due_at=_NOW + timedelta(hours=24),
            sla_hours=24,
            created_at=_NOW,
            updated_at=_NOW,
            created_by_id=self.principal.id,
            updated_by_id=self.principal.id,
            request_id="rq-t110-case",
        )
        self.session.add(self.case)
        self.session.flush()

    def _portfolio(self, code: str) -> Portfolio:
        portfolio = Portfolio.create(
            code=code,
            name=f"Portfolio {code}",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t110-portfolio-{code.lower()}",
        )
        self.session.add(portfolio)
        self.session.flush()
        return portfolio

    def _borrower(self, reference: str, portfolio: Portfolio) -> Borrower:
        borrower = Borrower(
            id=new_id(),
            reference=reference,
            legal_name="Meridian Auto Components Private Limited",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t110-borrower-{reference.lower()}",
        )
        self.session.add(borrower)
        self.session.flush()
        return borrower

    def _user(self, user_id, username: str) -> AppUser:
        user = AppUser(
            id=user_id,
            username=username,
            email=f"{username}@example.test",
            full_name=username.title(),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t110-user-{username}",
        )
        self.session.add(user)
        self.session.flush()
        return user

    def client(self, permissions: tuple[Permission, ...] | None = None) -> TestClient:
        principal = self.principal
        if permissions is not None:
            principal = Principal.user(principal.id, permissions)
        app = create_app(
            routers=(
                create_cases_router(
                    self.session,
                    audit_writer=self.audit,
                    clock=self.clock,
                ),
            ),
            principal_resolver=lambda _request: principal,
        )
        return TestClient(app)

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def fixture() -> _Fixture:
    value = _Fixture()
    try:
        yield value
    finally:
        value.close()


def test_list_scoped_and_filtered(fixture: _Fixture) -> None:
    other_portfolio = fixture._portfolio("OTHER")
    other_borrower = fixture._borrower("B-OTHER", other_portfolio)
    fixture.session.add(
        Case(
            id=new_id(),
            reference="C-OTHER",
            borrower_id=other_borrower.id,
            state="closed",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t110-other-case",
        )
    )
    fixture.session.flush()

    with fixture.client() as client:
        response = client.get("/cases")
        filtered = client.get("/cases?state=closed")

    assert response.status_code == 200
    assert "C-T110" in response.text
    assert "C-OTHER" not in response.text
    assert filtered.status_code == 200
    assert "C-T110" not in filtered.text
    assert "C-OTHER" not in filtered.text


def test_case_filter_offers_scoped_assignee_choices_instead_of_uuid_text(
    fixture: _Fixture,
) -> None:
    with fixture.client() as client:
        response = client.get("/cases")
        filtered = client.get(
            "/cases",
            params={"state": "open", "assignee": str(fixture.principal.id)},
        )

    assert response.status_code == 200
    assert '<select class="field__control" id="case-assignee" name="assignee"' in response.text
    assert f'<option value="{fixture.principal.id}">' in response.text
    assert "Analyst (@analyst)" in response.text
    assert 'id="case-assignee" name="assignee" type="text"' not in response.text
    assert filtered.status_code == 200
    assert "C-T110" in filtered.text
    assert 'href="/cases"' in filtered.text


def test_out_of_scope_mention_not_notified_and_author_told(fixture: _Fixture) -> None:
    outside = fixture._user(uuid4(), "outside")
    fixture.session.flush()

    with fixture.client() as client:
        response = client.post(
            "/cases/C-T110",
            data={"action": "comment", "comment": "Please review this, @outside."},
            follow_redirects=False,
        )
        detail = client.get("/cases/C-T110?notice=mention_scope")

    assert response.status_code == 303
    assert "notice=mention_scope" in response.headers["location"]
    assert detail.status_code == 200
    assert "no notification was sent" in detail.text
    comment = fixture.session.scalar(
        select(CaseComment).where(CaseComment.case_id == fixture.case.id)
    )
    assert comment is not None
    assert comment.mentions == ["outside"]
    assert (
        fixture.session.scalar(
            select(Notification.id).where(Notification.recipient_id == outside.id)
        )
        is None
    )


def test_logged_action_links_to_catalogue(fixture: _Fixture) -> None:
    intervention = Intervention(
        id=new_id(),
        code="REDUCE-EXPOSURE",
        role_tag="credit",
        text="Review and reduce funded exposure.",
        effect_model="linear",
        effect_parameters={"days": 30},
        applicable_covenant_classes=["financial"],
        requires_approval=False,
        is_active=True,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t110-intervention",
    )
    fixture.session.add(intervention)
    fixture.session.flush()

    with fixture.client() as client:
        response = client.post(
            "/cases/C-T110",
            data={
                "action": "log_action",
                "intervention_code": "REDUCE-EXPOSURE",
                "action_detail": "Treasury review requested.",
            },
            follow_redirects=False,
        )
        detail = client.get("/cases/C-T110")

    assert response.status_code == 303
    action = fixture.session.scalar(
        select(ActionTaken).where(ActionTaken.case_id == fixture.case.id)
    )
    assert action is not None
    assert action.intervention_id == intervention.id
    assert action.free_text == "Treasury review requested."
    assert "REDUCE-EXPOSURE" in detail.text
    assert "Catalogue intervention" in detail.text


def test_state_control_absent_without_permission(fixture: _Fixture) -> None:
    permissions = (Permission.VIEW_CASE,)
    with fixture.client(permissions) as client:
        detail = client.get("/cases/C-T110")
        response = client.post("/cases/C-T110", data={"action": "state", "state": "closed"})

    assert detail.status_code == 200
    assert 'id="case-state-change"' not in detail.text
    assert response.status_code == 403
    assert "UPDATE_CASE" in response.json()["message"]


def test_superseded_memo_marked(fixture: _Fixture) -> None:
    old_run = ForecastRun(
        id=new_id(),
        as_of_date=date(2026, 8, 1),
        started_at=_NOW,
        finished_at=_NOW,
        state="superseded",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t110-old-run",
    )
    fixture.session.add(old_run)
    fixture.session.flush()
    fixture.session.add(
        Memo(
            id=new_id(),
            borrower_id=fixture.borrower.id,
            run_id=old_run.id,
            case_id=fixture.case.id,
            template_version="v1",
            slots={"headline": "Earlier warning"},
            drafted_text="The earlier warning is retained for reconstruction.",
            created_at=_NOW,
            updated_at=_NOW,
            generated_by_id=fixture.principal.id,
            request_id="rq-t110-old-memo",
        )
    )
    fixture.session.flush()

    with fixture.client() as client:
        response = client.get("/cases/C-T110")

    assert response.status_code == 200
    assert 'data-memo-state="superseded"' in response.text
    assert "Superseded run" in response.text


def test_no_history_editing_control(fixture: _Fixture) -> None:
    fixture.session.add(
        CaseEvent(
            id=new_id(),
            case_id=fixture.case.id,
            event_type="opened",
            actor_id=fixture.principal.id,
            payload={"band": "act"},
            occurred_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            created_by_id=fixture.principal.id,
            updated_by_id=fixture.principal.id,
            request_id="rq-t110-history",
        )
    )
    fixture.session.flush()

    with fixture.client() as client:
        response = client.get("/cases/C-T110")

    assert response.status_code == 200
    assert 'data-history-event="opened"' in response.text
    assert "Edit history" not in response.text
    assert "Delete history" not in response.text
    assert 'name="history_action"' not in response.text
