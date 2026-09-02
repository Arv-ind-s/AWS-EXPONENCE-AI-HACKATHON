"""Integration coverage for T-116's scoped queue and delivery policy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Notification
from covenant_radar.db.scoping import Scope
from covenant_radar.notifications.model import (
    NotificationState,
    NotificationTemplate,
    ScopedValue,
    TemplateRegistry,
    TemplateSlot,
)
from covenant_radar.ports.notifier import DeliveryResult, DeliveryStatus, OutboundMessage
from covenant_radar.services.notifications import NotificationService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Notifier:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(DeliveryStatus.SENT, provider_message_id="local-1")


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []

    def record(
        self,
        event_type: str,
        _subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, {**payload, "actor": actor, "request_id": request_id}))
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = AppUser(
            id=uuid4(),
            username="t116-user",
            email="t116-user@example.test",
            full_name="T116 User",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t116-user",
        )
        self.session.add(self.user)
        self.session.flush()
        self.scope = Scope.from_paths(self.user.id, ["portfolio.allowed"])
        self.notifier = _Notifier()
        self.audit = _Audit()
        self.clock = FixedClock(_NOW)
        self.template = NotificationTemplate(
            name="scope_test",
            subject_template="Alert for {public}",
            body_template="{public}|{inside}|{outside}",
            slots=(
                TemplateSlot("public", str),
                TemplateSlot("inside", str),
                TemplateSlot("outside", str),
            ),
        )
        self.registry = TemplateRegistry((self.template,))
        self.service = NotificationService(
            self.session,
            notifier=self.notifier,
            audit=self.audit,
            clock=self.clock,
            request_id="rq-t116-service",
            template_registry=self.registry,
            scope_resolver=lambda _principal: self.scope,
            permission_resolver=lambda _user_id: (),
        )

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


def test_out_of_scope_content_removed(fixture: _Fixture) -> None:
    queued = fixture.service.queue(
        fixture.template,
        {
            "public": "visible summary",
            "inside": ScopedValue("allowed detail", portfolio_path="portfolio.allowed"),
            "outside": ScopedValue("private detail", portfolio_path="portfolio.other"),
        },
        recipient_id=fixture.user.id,
    )

    assert len(queued) == 1
    assert queued[0].state == NotificationState.PENDING.value
    assert queued[0].payload["outside"] == ""

    outcomes = fixture.service.send_due()

    assert outcomes[0].state is NotificationState.SENT
    assert len(fixture.notifier.messages) == 1
    assert fixture.notifier.messages[0].body == "visible summary|allowed detail|"
    assert "private detail" not in fixture.notifier.messages[0].body


def test_empty_after_filtering_not_sent_and_recorded(fixture: _Fixture) -> None:
    template = NotificationTemplate(
        name="scoped_only",
        subject_template="Scoped notice",
        body_template="{detail}",
        slots=(TemplateSlot("detail", str),),
    )
    service = NotificationService(
        fixture.session,
        notifier=fixture.notifier,
        audit=fixture.audit,
        clock=fixture.clock,
        request_id="rq-t116-empty",
        template_registry=TemplateRegistry((template,)),
        scope_resolver=lambda _principal: fixture.scope,
        permission_resolver=lambda _user_id: (),
    )

    queued = service.queue(
        template,
        {"detail": ScopedValue("not visible", portfolio_path="portfolio.other")},
        recipient_id=fixture.user.id,
    )

    assert queued[0].state == NotificationState.SUPPRESSED.value
    assert queued[0].last_error == "all notification content was removed by recipient scope"
    assert fixture.notifier.messages == []
    assert any(event[0] == "notification_suppressed" for event in fixture.audit.events)


def test_deactivated_recipient_not_sent(fixture: _Fixture) -> None:
    queued = fixture.service.queue(
        fixture.template,
        {"public": "summary", "inside": "detail", "outside": "other"},
        recipient_id=fixture.user.id,
    )
    assert queued[0].state == NotificationState.PENDING.value

    fixture.user.is_active = False
    fixture.session.flush()
    outcomes = fixture.service.send_due()

    row = fixture.session.scalar(select(Notification).where(Notification.id == queued[0].id))
    assert outcomes[0].state is NotificationState.SUPPRESSED
    assert row is not None
    assert row.state == NotificationState.SUPPRESSED.value
    assert row.last_error == "recipient is inactive at delivery time"
    assert fixture.notifier.messages == []
