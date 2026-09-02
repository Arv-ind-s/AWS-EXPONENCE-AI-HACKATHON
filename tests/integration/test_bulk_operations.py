"""Integration coverage for T-139 bulk case operations and exports."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import NotFound
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    Case,
    JobRun,
    Portfolio,
    UserPortfolioScope,
)
from covenant_radar.db.scoping import Scope
from covenant_radar.scheduler.jobs import JobDefinition, JobRegistry, JobRunContext
from covenant_radar.scheduler.ledger import SUCCEEDED
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.bulk import BulkService
from covenant_radar.services.export import ExportNotification, ExportService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

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


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, content: bytes, *, content_hash: str | None = None) -> str:
        actual_hash = hashlib.sha256(content).hexdigest()
        assert content_hash in {None, actual_hash}
        key = f"exports/{len(self.objects) + 1}"
        self.objects[key] = content
        return key

    def get(self, storage_key: str) -> bytes:
        return self.objects[storage_key]


class _Notifications:
    def __init__(self) -> None:
        self.messages: list[ExportNotification] = []

    def notify(self, notification: ExportNotification) -> None:
        self.messages.append(notification)


class _Runner:
    def __init__(self) -> None:
        self.registry = JobRegistry()
        self.submitted: list[tuple[str, dict[str, object]]] = []

    def submit(self, job_name: str, *, trigger: str, **kwargs: object) -> object:
        self.submitted.append((job_name, {"trigger": trigger, **kwargs}))
        definition: JobDefinition = self.registry.get(job_name)
        definition.handler(
            JobRunContext(
                run_id=str(kwargs["run_id"]),
                attempt=1,
                trigger=trigger,
                request_id="rq-t139-worker",
            )
        )
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.principal = Principal.user(
            uuid4(),
            (Permission.VIEW_CASE, Permission.UPDATE_CASE, Permission.EXPORT_EVIDENCE),
        )
        self._add_user(self.principal.id, "desk")
        self.portfolio = Portfolio.create(
            code="T139",
            name="T139 portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t139-portfolio",
        )
        self.session.add(self.portfolio)
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.clock = FixedClock(_NOW)

    def _add_user(
        self, user_id: UUID, suffix: str, *, portfolio: Portfolio | None = None
    ) -> AppUser:
        user = AppUser(
            id=user_id,
            username=f"t139-{suffix}",
            email=f"t139-{suffix}@example.com",
            full_name=f"T139 {suffix.title()}",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t139-user-{suffix}",
        )
        self.session.add(user)
        self.session.flush()
        # An assignable user needs a portfolio grant: `BulkService` refuses an
        # assignee whose grants do not reach the case's portfolio.  The desk
        # user is created before the portfolio exists and simply gets none.
        granted = portfolio if portfolio is not None else getattr(self, "portfolio", None)
        if granted is not None:
            self.session.add(
                UserPortfolioScope(
                    user_id=user_id,
                    portfolio_id=granted.id,
                    include_descendants=True,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=f"rq-t139-scope-{suffix}",
                )
            )
            self.session.flush()
        return user

    def case(
        self,
        reference: str,
        *,
        state: str = "open",
        portfolio: Portfolio | None = None,
    ) -> Case:
        resolved_portfolio = portfolio or self.portfolio
        borrower = Borrower(
            id=new_id(),
            reference=f"B-{reference}",
            legal_name=f"Borrower {reference}",
            portfolio_id=resolved_portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t139-borrower-{reference}",
        )
        case = Case(
            id=new_id(),
            reference=reference,
            borrower_id=borrower.id,
            state=state,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t139-case-{reference}",
        )
        self.session.add_all([borrower, case])
        self.session.flush()
        return case

    def bulk(self, principal: Principal | None = None) -> BulkService:
        return BulkService(
            self.session,
            audit=self.audit,
            clock=self.clock,
            request_id="rq-t139-bulk",
            scope_resolver=lambda _principal: self.scope,
        )

    def export(self, **kwargs: object) -> ExportService:
        return ExportService(
            self.session,
            store=_Store(),
            audit=self.audit,
            clock=self.clock,
            request_id="rq-t139-export",
            scope_resolver=lambda _principal: self.scope,
            **kwargs,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def world() -> _Fixture:
    fixture = _Fixture()
    try:
        yield fixture
    finally:
        fixture.close()


def test_partial_success_reported_per_item(world: _Fixture) -> None:
    open_case = world.case("C139-OPEN")
    closed_case = world.case("C139-CLOSED", state="closed")
    other_case = world.case("C139-OTHER", portfolio=_other_portfolio(world))

    report = world.bulk().execute(
        world.principal,
        (open_case.id, closed_case.id, other_case.id),
        "state",
        value={"state": "in_progress"},
    )

    assert report.requested_count == 3
    assert [item.item_id for item in report.items] == [
        str(open_case.id),
        str(closed_case.id),
        str(other_case.id),
    ]
    assert report.success_count == 1
    assert report.failure_count == 1
    assert report.excluded_count == 1
    assert report.failures[0].reason and "not permitted" in report.failures[0].reason
    assert report.excluded[0].reason and "scope" in report.excluded[0].reason
    assert open_case.state == "in_progress"
    assert closed_case.state == "closed"


def test_selection_by_case_reference_is_accepted(world: _Fixture) -> None:
    """The queue posts references, not UUIDs, so this path must stay open.

    A queue row carries one case handle, and it is the same value the case
    page is addressed by.  Keeping references acceptable here is what lets
    the screen select and act without putting a case UUID in its markup.
    """

    selected = world.case("C139-REF")

    report = world.bulk().execute(
        world.principal,
        (selected.reference,),
        "state",
        value={"state": "in_progress"},
    )

    assert report.success_count == 1
    assert selected.state == "in_progress"


def test_assignee_outside_the_case_portfolio_is_refused(world: _Fixture) -> None:
    """Bulk assign applies the same scope rule as the case-detail screen.

    Restricting the queue's assignee select is not enough on its own: a
    hand-made POST naming any active user's id would otherwise assign a case
    to someone with no authority over its portfolio.
    """

    selected = world.case("C139-SCOPE")
    outsider = world._add_user(uuid4(), "outsider", portfolio=_other_portfolio(world))

    report = world.bulk().execute(
        world.principal,
        (selected.id,),
        "assign",
        value=outsider.id,
    )

    assert report.success_count == 0
    assert report.failure_count == 1
    assert report.failures[0].reason and "portfolio scope" in report.failures[0].reason
    assert selected.assignee_id is None


def test_unpermitted_items_excluded_with_reason(world: _Fixture) -> None:
    selected = world.case("C139-PERM")
    principal = Principal.user(world.principal.id, (Permission.VIEW_CASE,))
    report = world.bulk().execute(principal, (selected.id,), "state", value="monitoring")

    assert report.success_count == 0
    assert report.failure_count == 0
    assert report.excluded_count == 1
    assert report.excluded[0].reason == "Missing permission: UPDATE_CASE."
    assert selected.state == "open"


def test_large_export_queued_and_notified(world: _Fixture) -> None:
    for index in range(3):
        world.case(f"C139-X{index}")
    store = _Store()
    runner = _Runner()
    notifications = _Notifications()
    service = ExportService(
        world.session,
        store=store,
        runner=runner,
        notification_sink=notifications,
        audit=world.audit,
        clock=world.clock,
        async_threshold=2,
        scope_resolver=lambda _principal: world.scope,
    )

    result = service.export_cases(world.principal)

    assert result.queued is True
    assert result.row_count == 3
    assert len(runner.submitted) == 1
    assert len(notifications.messages) == 1
    notification = notifications.messages[0]
    assert notification.recipient_id == world.principal.id
    assert notification.export_id == result.export_id
    assert notification.row_count == 3
    assert store.objects


def test_export_scoped_to_requester(world: _Fixture) -> None:
    visible = world.case("C139-VISIBLE")
    other_portfolio = _other_portfolio(world)
    hidden = world.case("C139-HIDDEN", portfolio=other_portfolio)
    service = world.export()

    result = service.export_cases(
        world.principal,
        case_ids=(visible.id, hidden.id),
        force_async=False,
    )

    assert result.row_count == 1
    assert result.content is not None
    assert visible.reference.encode() in result.content
    assert hidden.reference.encode() not in result.content


def test_per_item_and_summary_events(world: _Fixture) -> None:
    first = world.case("C139-AUDIT-A")
    second = world.case("C139-AUDIT-B")
    assignee = world._add_user(uuid4(), "assignee")

    report = world.bulk().execute(
        world.principal,
        (first.id, second.id),
        "assign",
        value=assignee.id,
    )

    events = [
        event
        for event in world.audit.events
        if event["event_type"] == AuditEventType.CASE_LIFECYCLE_CHANGED.value
    ]
    assert report.success_count == 2
    assert len(events) == 3
    detail_events = [
        event for event in events if event["subject"] != ("bulk_operation", report.operation_id)
    ]
    summary_events = [
        event for event in events if event["subject"] == ("bulk_operation", report.operation_id)
    ]
    assert len(detail_events) == 2
    assert len(summary_events) == 1
    summary = summary_events[0]["payload"]
    assert isinstance(summary, dict)
    assert summary["requested_count"] == 2
    assert summary["outcome_distribution"] == {"succeeded": 2, "failed": 0, "excluded": 0}


def test_download_link_expires(world: _Fixture) -> None:
    export_id = new_id()
    content = b"case_reference\r\nC139-EXPIRED\r\n"
    store = _Store()
    storage_key = store.put(content)
    world.session.add(
        JobRun(
            id=new_id(),
            job_name="exports.bulk.expired",
            run_id=str(export_id),
            trigger="bulk-export",
            started_at=_NOW - timedelta(hours=2),
            finished_at=_NOW - timedelta(hours=1),
            state=SUCCEEDED,
            attempt=1,
            metrics={
                "format": "csv",
                "row_count": 1,
                "filter": {},
                "storage_key": storage_key,
                "content_hash": hashlib.sha256(content).hexdigest(),
                "expires_at": (_NOW - timedelta(minutes=1)).isoformat(),
            },
            created_at=_NOW - timedelta(hours=2),
            updated_at=_NOW - timedelta(hours=1),
            created_by_id=world.principal.id,
            updated_by_id=world.principal.id,
            request_id="rq-t139-expired",
        )
    )
    world.session.flush()
    service = ExportService(
        world.session,
        store=store,
        clock=world.clock,
        audit=world.audit,
    )

    result = service.status(world.principal, export_id)

    assert result.state == "expired"
    assert result.download_url is None
    with pytest.raises(NotFound, match="expired"):
        service.download(world.principal, export_id)


def _other_portfolio(world: _Fixture) -> Portfolio:
    portfolio = Portfolio.create(
        code=f"T139-{uuid4().hex[:6]}",
        name="T139 hidden portfolio",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t139-hidden-portfolio",
    )
    world.session.add(portfolio)
    world.session.flush()
    return portfolio
