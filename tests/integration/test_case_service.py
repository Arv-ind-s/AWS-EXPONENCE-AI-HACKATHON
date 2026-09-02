"""Integration coverage for T-109 case ownership, SLA and lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict, NotFound
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    Case,
    CaseEvent,
    ForecastRun,
    Notification,
    Portfolio,
    TriageEntry,
)
from covenant_radar.db.repositories.case import CaseRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.cases.sla import SlaThresholds
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.cases import CaseService

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
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.principal = Principal.user(uuid4(), (Permission.VIEW_CASE, Permission.UPDATE_CASE))
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="t109-user",
                email="t109-user@example.com",
                full_name="T109 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t109-user",
            )
        )
        self.portfolio = Portfolio.create(
            code="T109",
            name="T109 portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t109-portfolio",
        )
        self.borrower = Borrower(
            id=new_id(),
            reference="B-T109",
            legal_name="T109 Borrower Private Limited",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t109-borrower",
        )
        self.session.add_all([self.portfolio, self.borrower])
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.clock = FixedClock(_NOW)
        self.service = CaseService(
            self.session,
            SlaThresholds(24, 72, 168),
            audit=self.audit,
            clock=self.clock,
            request_id="rq-t109-service",
            scope_resolver=lambda _principal: self.scope,
            default_owner_id=self.principal.id,
            administrator_ids=(self.principal.id,),
        )

    def run(self, *, suffix: str) -> ForecastRun:
        run = ForecastRun(
            id=new_id(),
            as_of_date=self.clock.now().date(),
            started_at=self.clock.now(),
            finished_at=self.clock.now(),
            covenant_count=1,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t109-run-{suffix}",
        )
        self.session.add(run)
        self.session.flush()
        return run

    def entry(self, run: ForecastRun, *, band: str, suffix: str) -> TriageEntry:
        entry = TriageEntry(
            id=new_id(),
            run_id=run.id,
            borrower_id=self.borrower.id,
            band=band,
            rank=1,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
            request_id=f"rq-t109-entry-{suffix}",
        )
        self.session.add(entry)
        self.session.flush()
        return entry

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


def test_reentry_updates_existing_case(fixture: _Fixture) -> None:
    first_run = fixture.run(suffix="first")
    first = fixture.service.open_or_update(
        fixture.principal,
        fixture.entry(first_run, band="amber", suffix="amber"),
        scope=fixture.scope,
    )
    assert first is not None
    original_id = first.id
    original_due_at = first.due_at

    second_run = fixture.run(suffix="second")
    updated = fixture.service.open_or_update(
        fixture.principal,
        fixture.entry(second_run, band="act", suffix="act"),
        scope=fixture.scope,
    )

    assert updated is first
    assert updated.id == original_id
    assert updated.band_at_open == "act"
    assert updated.sla_hours == 24
    assert updated.due_at == fixture.clock.now() + timedelta(hours=24)
    assert updated.due_at != original_due_at
    assert fixture.session.scalar(select(func.count(Case.id))) == 1


def test_reescalation_after_closure_links_prior(fixture: _Fixture) -> None:
    run = fixture.run(suffix="re-escalate")
    prior = fixture.service.open_or_update(
        fixture.principal,
        fixture.entry(run, band="act", suffix="prior"),
        scope=fixture.scope,
    )
    assert prior is not None
    fixture.service.transition_case(
        fixture.principal,
        prior.id,
        "closed",
        closure_reason="Borrower cured the warning.",
        scope=fixture.scope,
    )

    successor = fixture.service.open_or_update(
        fixture.principal,
        fixture.entry(run, band="act", suffix="successor"),
        scope=fixture.scope,
    )
    assert successor is not None
    assert successor.id != prior.id
    assert successor.reference != prior.reference
    assert successor.opened_from_run_id == run.id
    event = fixture.session.scalar(
        select(CaseEvent).where(
            CaseEvent.case_id == successor.id,
            CaseEvent.event_type == "reopened",
        )
    )
    assert event is not None
    assert event.payload is not None
    assert event.payload["prior_case_id"] == str(prior.id)
    assert fixture.session.scalar(select(func.count(Case.id)).where(Case.state != "closed")) == 1


def test_sla_breach_escalates_and_lists_overdue(fixture: _Fixture) -> None:
    run = fixture.run(suffix="overdue")
    case = fixture.service.open_or_update(
        fixture.principal,
        fixture.entry(run, band="act", suffix="overdue"),
        scope=fixture.scope,
    )
    assert case is not None
    due_at = case.due_at
    assert due_at is not None

    fixture.clock.set(due_at)
    overdue_before = fixture.service.overdue_cases(
        fixture.principal, scope=fixture.scope, now=fixture.clock.now()
    )
    assert overdue_before == (case,)
    escalated = fixture.service.escalate_overdue(
        fixture.principal, scope=fixture.scope, now=fixture.clock.now()
    )

    assert escalated == (case,)
    assert case.state == "escalated"
    assert (
        fixture.session.scalar(
            select(func.count(Notification.id)).where(Notification.template == "case_sla_breach")
        )
        == 1
    )
    assert any(event[2]["action"] == "sla_breached" for event in fixture.audit.events)


def test_missing_assignee_falls_to_default_and_notifies(fixture: _Fixture) -> None:
    run = fixture.run(suffix="fallback")
    case = fixture.service.open_or_update(
        fixture.principal,
        fixture.entry(run, band="amber", suffix="fallback"),
        scope=fixture.scope,
    )

    assert case is not None
    assert case.assignee_id == fixture.principal.id
    assert (
        fixture.session.scalar(
            select(func.count(Notification.id)).where(
                Notification.template == "case_assignee_fallback",
                Notification.recipient_id == fixture.principal.id,
            )
        )
        == 1
    )
    fallback_event = fixture.session.scalar(
        select(CaseEvent).where(
            CaseEvent.case_id == case.id,
            CaseEvent.event_type == "assignee_fallback",
        )
    )
    assert fallback_event is not None


def test_history_append_only(fixture: _Fixture) -> None:
    run = fixture.run(suffix="history")
    case = fixture.service.open_or_update(
        fixture.principal,
        fixture.entry(run, band="amber", suffix="history"),
        scope=fixture.scope,
    )
    assert case is not None
    fixture.service.transition_case(fixture.principal, case.id, "in_progress", scope=fixture.scope)
    fixture.service.transition_case(
        fixture.principal,
        case.id,
        "closed",
        closure_reason="Work completed and evidence retained.",
        scope=fixture.scope,
    )

    history = fixture.service.history(fixture.principal, case.id, scope=fixture.scope)
    assert [event.event_type for event in history] == [
        "opened",
        "assignee_fallback",
        "state_changed",
        "state_changed",
    ]
    assert [event.occurred_at for event in history] == sorted(
        event.occurred_at for event in history
    )
    repository = CaseRepository(fixture.session)
    assert not hasattr(repository, "update")
    assert not hasattr(repository, "delete")


def test_out_of_scope_case_is_not_found(fixture: _Fixture) -> None:
    with pytest.raises(NotFound):
        fixture.service.get_case(fixture.principal, uuid4(), scope=fixture.scope)


def test_multiple_open_cases_are_refused(fixture: _Fixture) -> None:
    run = fixture.run(suffix="duplicate")
    first = fixture.service.open_or_update(
        fixture.principal,
        fixture.entry(run, band="act", suffix="duplicate"),
        scope=fixture.scope,
    )
    assert first is not None
    duplicate = Case(
        id=new_id(),
        reference="C-DUPLICATE",
        borrower_id=fixture.borrower.id,
        state="open",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t109-duplicate",
    )
    fixture.session.add(duplicate)
    fixture.session.flush()

    with pytest.raises(Conflict, match="exactly one is permitted"):
        fixture.service.open_or_update(
            fixture.principal,
            fixture.entry(run, band="amber", suffix="duplicate-again"),
            scope=fixture.scope,
        )
