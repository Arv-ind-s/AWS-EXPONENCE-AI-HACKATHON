"""Integration coverage for T-138 saved views and recent navigation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    Portfolio,
    Role,
    SavedQueueView,
    UserRole,
)
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.repositories.view import ViewRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.triage.views import QueueFilters
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.views import ViewService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.clock = FixedClock(_NOW)
        self.audit = AuditRecorder(
            AuditRepository(self.session), clock=self.clock, request_id="rq-t138"
        )
        self.owner = self.user("owner")
        self.recipient = self.user("recipient")
        self.admin = self.user("administrator")
        self.admin_role = Role(
            id=uuid4(),
            code="administrator",
            name="Administrator",
            is_system=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t138-role",
        )
        self.session.add(self.admin_role)
        self.session.flush()
        self.session.add(
            UserRole(
                user_id=self.admin.id,
                role_id=self.admin_role.id,
                granted_by_id=self.admin.id,
                granted_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t138-user-role",
            )
        )
        self.session.flush()
        self.scopes: dict[object, Scope] = {}

    def user(self, username: str) -> AppUser:
        row = AppUser(
            id=uuid4(),
            username=username,
            email=f"{username}@example.com",
            full_name=username.title(),
            auth_source="local",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t138-{username}",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def portfolio(self, code: str) -> Portfolio:
        row = Portfolio.create(
            code=code,
            name=f"Portfolio {code}",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t138-{code}",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def borrower(self, portfolio: Portfolio, reference: str) -> Borrower:
        row = Borrower(
            id=uuid4(),
            reference=reference,
            legal_name=f"Legal {reference}",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t138-{reference}",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def principal(self, user: AppUser) -> Principal:
        return Principal.user(
            user.id,
            (
                Permission.VIEW_QUEUE,
                Permission.VIEW_BORROWER,
                Permission.VIEW_CASE,
                Permission.VIEW_COVENANT,
                Permission.VIEW_DOCUMENT,
                Permission.VIEW_FORECAST,
                Permission.VIEW_MEMO,
            ),
        )

    def service(self) -> ViewService:
        return ViewService(
            self.session,
            repository=ViewRepository(self.session, clock=self.clock),
            audit=self.audit,
            clock=self.clock,
            scope_resolver=lambda principal: self.scopes[principal.id],
            request_id="rq-t138-service",
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def fixture() -> _Fixture:
    value = _Fixture()
    yield value
    value.close()


def test_shared_view_applies_within_recipient_scope(fixture: _Fixture) -> None:
    p1 = fixture.portfolio("P1")
    p2 = fixture.portfolio("P2")
    fixture.scopes[fixture.owner.id] = Scope.from_paths(fixture.owner.id, [p1.path, p2.path])
    fixture.scopes[fixture.recipient.id] = Scope.from_paths(fixture.recipient.id, [p2.path])
    service = fixture.service()
    owner = fixture.principal(fixture.owner)
    recipient = fixture.principal(fixture.recipient)

    view = service.create(
        owner,
        name="P1 warnings",
        filters=QueueFilters(portfolio=p1.id),
        share_all=True,
    )
    applied = service.get_view(recipient, view.id)

    assert applied.queue_filters.portfolio == p1.id
    assert applied.notice is not None
    assert "scope" in applied.notice


def test_deactivated_owner_view_retained_and_transferred(fixture: _Fixture) -> None:
    portfolio = fixture.portfolio("P1")
    fixture.scopes[fixture.owner.id] = Scope.from_paths(fixture.owner.id, [portfolio.path])
    fixture.scopes[fixture.recipient.id] = Scope.from_paths(fixture.recipient.id, [portfolio.path])
    service = fixture.service()
    owner = fixture.principal(fixture.owner)
    recipient = fixture.principal(fixture.recipient)
    view = service.create(owner, name="Shared", filters=QueueFilters(), share_all=True)

    fixture.owner.is_active = False
    fixture.session.flush()
    loaded = service.get_view(recipient, view.id)

    assert loaded.id == view.id
    persisted = fixture.session.get(SavedQueueView, view.id)
    assert persisted is not None
    assert persisted.owner_id == fixture.admin.id


def test_dangling_filter_dropped_with_notice(fixture: _Fixture) -> None:
    portfolio = fixture.portfolio("P1")
    fixture.scopes[fixture.owner.id] = Scope.from_paths(fixture.owner.id, [portfolio.path])
    service = fixture.service()
    owner = fixture.principal(fixture.owner)
    missing_id = uuid4()
    view = service.create(
        owner,
        name="Missing portfolio",
        filters=QueueFilters(portfolio=missing_id),
    )

    applied = service.get_view(owner, view.id)

    assert applied.queue_filters.portfolio is None
    assert applied.dropped_filters == ("portfolio",)
    assert applied.notice is not None
    assert "deleted" in applied.notice


def test_recent_items_filtered_by_access(fixture: _Fixture) -> None:
    p1 = fixture.portfolio("P1")
    p2 = fixture.portfolio("P2")
    borrower = fixture.borrower(p1, "B-1")
    fixture.borrower(p2, "B-2")
    fixture.scopes[fixture.owner.id] = Scope.from_paths(fixture.owner.id, [p1.path])
    service = fixture.service()
    owner = fixture.principal(fixture.owner)

    service.record_recent_item(owner, subject_type="borrower", subject_id=borrower.id)
    recent = service.recent_items(owner)
    assert [item.subject_id for item in recent] == [borrower.id]

    fixture.scopes[fixture.owner.id] = Scope.empty(fixture.owner.id)
    assert service.recent_items(owner) == ()


def test_default_view_applied_on_entry(fixture: _Fixture) -> None:
    portfolio = fixture.portfolio("P1")
    fixture.scopes[fixture.owner.id] = Scope.from_paths(fixture.owner.id, [portfolio.path])
    service = fixture.service()
    owner = fixture.principal(fixture.owner)

    first = service.default_view(owner)
    assert first.is_default
    assert first.queue_filters.portfolio is None

    second_record = service.create(owner, name="Act only", filters=QueueFilters(band="act"))
    service.set_default(owner, second_record.id)
    second = service.default_view(owner)
    assert second.id == second_record.id
    assert second.queue_filters.band == "act"
