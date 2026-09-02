"""Integration coverage for T-119's durable in-app notification centre."""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    Notification,
    NotificationReadState,
    Portfolio,
    UserPortfolioScope,
)
from covenant_radar.db.scoping import Scope
from covenant_radar.notifications.inapp import InAppNotificationService
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.borrower import create_borrower_router
from covenant_radar.web.routes.notifications import create_notifications_router

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
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.clock = FixedClock(_NOW)
        self.audit = _Audit()
        self.permissions = (Permission.VIEW_QUEUE, Permission.VIEW_BORROWER)
        self.user = AppUser(
            id=uuid4(),
            username="t119-user",
            email="t119-user@example.test",
            full_name="T119 User",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t119-user",
        )
        self.session.add(self.user)
        self.session.flush()
        self.principal = Principal.user(self.user.id, self.permissions)
        self.portfolio = Portfolio.create(
            code="T119-ROOT",
            name="T119 portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t119-portfolio",
        )
        self.session.add(self.portfolio)
        self.session.flush()
        self.borrower = self._borrower("B-T119", self.portfolio)
        self.scope = Scope.from_paths(self.user.id, (self.portfolio.path,))
        self.session.add(
            UserPortfolioScope(
                user_id=self.user.id,
                portfolio_id=self.portfolio.id,
                include_descendants=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t119-scope",
            )
        )
        self.session.flush()
        self.service = InAppNotificationService(
            self.session,
            scope_resolver=lambda _principal: self.scope,
            permission_resolver=lambda _user_id: self.permissions,
            audit=self.audit,
            clock=self.clock,
            request_id="rq-t119-read-state",
        )

    def _borrower(self, reference: str, portfolio: Portfolio) -> Borrower:
        borrower = Borrower(
            id=uuid4(),
            reference=reference,
            legal_name="T119 Borrower Private Limited",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t119-{reference.lower()}",
        )
        self.session.add(borrower)
        self.session.flush()
        return borrower

    def notification(
        self,
        *,
        template: str = "system_failure",
        payload: dict[str, object] | None = None,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        created_at: datetime = _NOW,
    ) -> Notification:
        row = Notification(
            id=uuid4(),
            recipient_id=self.user.id,
            channel="inapp",
            template=template,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload or {"message": "T119 notification"},
            state="sent",
            scheduled_for=created_at,
            sent_at=created_at,
            attempts=1,
            created_at=created_at,
            updated_at=created_at,
            request_id="rq-t119-notification",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def client(self, *, include_borrower_route: bool = False) -> TestClient:
        routers = [create_notifications_router(self.session, service=self.service)]
        if include_borrower_route:
            routers.append(create_borrower_router(self.session))
        app = create_app(
            routers=tuple(routers),
            principal_resolver=lambda _request: self.principal,
        )
        app.state.notification_service = self.service
        return TestClient(app, follow_redirects=False)

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


def test_lost_access_hides_content_not_existence(fixture: _Fixture) -> None:
    other_portfolio = Portfolio.create(
        code="T119-OTHER",
        name="Other portfolio",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t119-other-portfolio",
    )
    fixture.session.add(other_portfolio)
    fixture.session.flush()
    other_borrower = fixture._borrower("B-T119-OTHER", other_portfolio)
    fixture.notification(
        template="band_change",
        payload={
            "borrower_reference": other_borrower.reference,
            "summary": "Confidential summary",
            "details": "CONFIDENTIAL-T119-CONTENT",
        },
        subject_type="borrower",
        subject_id=other_borrower.id,
    )

    page = fixture.service.list_notifications(fixture.principal)

    assert page.total == 1
    assert len(page.items) == 1
    item = page.items[0]
    assert item.is_accessible is False
    assert item.deep_link is None
    assert "CONFIDENTIAL-T119-CONTENT" not in item.title
    assert "CONFIDENTIAL-T119-CONTENT" not in item.body
    assert item.title == "Notification unavailable"


def test_bulk_read_is_one_action(fixture: _Fixture) -> None:
    for index in range(3):
        fixture.notification(payload={"message": f"Notification {index}"})

    marked = fixture.service.mark_all_read(fixture.principal)

    assert marked == 3
    assert len(fixture.audit.events) == 1
    event_type, payload = fixture.audit.events[0]
    assert event_type == "notifications_marked_read"
    assert payload["count"] == 3
    assert fixture.session.scalar(select(NotificationReadState)) is not None
    assert (
        fixture.session.scalar(
            select(NotificationReadState).where(NotificationReadState.read_at.is_(None))
        )
        is None
    )


def test_count_within_budget_at_scale(fixture: _Fixture) -> None:
    rows = [
        Notification(
            id=uuid4(),
            recipient_id=fixture.user.id,
            channel="inapp",
            template="system_failure",
            payload={"message": "scale"},
            state="sent",
            scheduled_for=_NOW,
            sent_at=_NOW,
            attempts=1,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t119-scale",
        )
        for _ in range(3_000)
    ]
    fixture.session.add_all(rows)
    fixture.session.flush()

    started = perf_counter()
    count = fixture.service.unread_count(fixture.principal)
    elapsed = perf_counter() - started

    assert count == 3_000
    assert elapsed < 0.5


def test_works_without_javascript(fixture: _Fixture) -> None:
    notification = fixture.notification(payload={"message": "No JS required"})

    with fixture.client() as client:
        response = client.get("/notifications")

    assert response.status_code == 200
    assert "No JS required" in response.text
    assert 'method="post"' in response.text
    assert 'action="/notifications/read-all"' in response.text
    assert f'action="/notifications/{notification.id}/read"' in response.text
    assert "Notifications (1)" in response.text


def test_deep_links_resolve(fixture: _Fixture) -> None:
    fixture.notification(
        template="band_change",
        payload={
            "borrower_reference": fixture.borrower.reference,
            "summary": "Visible summary",
            "details": "Visible details",
        },
        subject_type="borrower",
        subject_id=fixture.borrower.id,
    )

    page = fixture.service.list_notifications(fixture.principal)
    link = page.items[0].deep_link
    assert link == "/borrowers/B-T119"

    with fixture.client(include_borrower_route=True) as client:
        response = client.get(link)

    assert response.status_code == 200
    assert "B-T119" in response.text


def test_notification_fragments_and_unread_badge(fixture: _Fixture) -> None:
    fixture.notification(payload={"message": "Live notification"})

    with fixture.client() as client:
        fragment = client.get(
            "/notifications",
            headers={"HX-Request": "true", "HX-Target": "notification-results"},
        )
        badge = client.get(
            "/notifications/unread-count",
            headers={"HX-Request": "true", "HX-Target": "shell-notification-link"},
        )

    assert fragment.status_code == badge.status_code == 200
    assert "Live notification" in fragment.text
    assert 'id="notification-results"' in fragment.text
    assert "<html" not in fragment.text
    assert "Notifications (1)" in badge.text
    assert fragment.headers["vary"] == "HX-Request, HX-Target"
    assert badge.headers["vary"] == "HX-Request, HX-Target"
