"""Integration coverage for T-118 signed webhook delivery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Notification
from covenant_radar.notifications.model import NotificationState
from covenant_radar.notifications.templates import BAND_CHANGE_TEMPLATE, DEFAULT_TEMPLATE_REGISTRY
from covenant_radar.notifications.webhook import (
    WebhookEndpoint,
    WebhookEndpointRegistry,
    WebhookNotifier,
    validate_webhook_payload,
    verify_signature,
)
from covenant_radar.ports.notifier import (
    DeliveryStatus,
    NotificationChannel,
    OutboundMessage,
)
from covenant_radar.services.notifications import NotificationService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_SECRET = "test-webhook-signing-secret"


class _Fixture:
    def __init__(self, handler: httpx.MockTransport) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = AppUser(
            id=uuid4(),
            username="t118-user",
            email="t118-user@example.test",
            full_name="T118 User",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t118-user",
        )
        self.session.add(self.user)
        self.session.flush()
        self.clock = FixedClock(_NOW)
        self.endpoints = WebhookEndpointRegistry(
            {self.user.id: WebhookEndpoint("http://localhost/hook", endpoint_id="bank-core")}
        )
        self.client = httpx.Client(transport=handler)
        self.notifier = WebhookNotifier(
            signing_secret=_SECRET,
            endpoints=self.endpoints,
            clock=self.clock,
            http_client=self.client,
        )
        self.service = NotificationService(
            self.session,
            notifier=self.notifier,
            clock=self.clock,
            request_id="rq-t118-service",
            template_registry=DEFAULT_TEMPLATE_REGISTRY,
            scope_resolver=lambda principal: _empty_scope(principal.id),
            permission_resolver=lambda _user_id: (),
            retry_base_seconds=30,
            max_attempts=3,
        )

    def queue(self) -> Notification:
        rows = self.service.queue(
            BAND_CHANGE_TEMPLATE,
            {
                "borrower_reference": "BOR-118",
                "summary": "Covenant moved to act.",
                "details": "Projected crossing is within 60 days.",
            },
            recipient_id=self.user.id,
            channel=NotificationChannel.WEBHOOK,
        )
        return rows[0]

    def close(self) -> None:
        self.notifier.close()
        self.session.close()
        self.engine.dispose()


def _empty_scope(user_id: UUID):
    from covenant_radar.db.scoping import Scope

    return Scope.from_paths(user_id, [])


def _fixture(handler: httpx.MockTransport) -> _Fixture:
    return _Fixture(handler)


def test_three_failures_dead_letter_and_alert() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, request=request)

    fixture = _fixture(httpx.MockTransport(handler))
    alerts: list[tuple[UUID, str]] = []
    fixture.service.dead_letter_alert = lambda row, reason: alerts.append((row.id, reason))
    try:
        row = fixture.queue()
        fixture.service.send_due(now=_NOW)
        fixture.service.send_due(now=_NOW + timedelta(seconds=30))
        fixture.service.send_due(now=_NOW + timedelta(seconds=90))

        stored = fixture.session.scalar(select(Notification).where(Notification.id == row.id))
        dead_letters = fixture.service.list_dead_letters()
        assert stored is not None
        assert stored.state == NotificationState.DEAD_LETTERED.value
        assert stored.attempts == 3
        assert len(calls) == 3
        assert len(dead_letters) == 1
        assert alerts == [(row.id, "webhook endpoint returned HTTP 503")]
    finally:
        fixture.close()


def test_manual_replay_does_not_duplicate() -> None:
    calls: list[httpx.Request] = []
    available = False

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503 if not available else 204, request=request)

    fixture = _fixture(httpx.MockTransport(handler))
    try:
        row = fixture.queue()
        fixture.service.send_due(now=_NOW)
        fixture.service.send_due(now=_NOW + timedelta(seconds=30))
        fixture.service.send_due(now=_NOW + timedelta(seconds=90))

        available = True
        replayed = fixture.service.replay_dead_letter(row.id)
        again = fixture.service.replay_dead_letter(row.id)

        assert replayed.state is NotificationState.SENT
        assert again.state is NotificationState.SENT
        assert len(calls) == 4
        assert fixture.service.list_dead_letters() == ()
    finally:
        fixture.close()


def test_signature_verifies_with_documented_procedure() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202, request=request)

    fixture = _fixture(httpx.MockTransport(handler))
    try:
        result = fixture.notifier.send(_message(fixture.user.id))
        assert result.status is DeliveryStatus.SENT
        request = captured[0]
        assert verify_signature(
            request.content,
            request.headers["X-Covenant-Radar-Timestamp"],
            request.headers["X-Covenant-Radar-Signature"],
            _SECRET,
            now=_NOW,
        )
        assert not verify_signature(
            request.content + b"tampered",
            request.headers["X-Covenant-Radar-Timestamp"],
            request.headers["X-Covenant-Radar-Signature"],
            _SECRET,
            now=_NOW,
        )
    finally:
        fixture.close()


def test_payload_has_no_personal_field() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204, request=request)

    fixture = _fixture(httpx.MockTransport(handler))
    try:
        result = fixture.notifier.send(
            _message(fixture.user.id, payload={"email": "secret@example.test"})
        )
        assert result.status is DeliveryStatus.DEAD_LETTERED
        assert "personal-class field" in (result.error or "")
        assert captured == []
        with pytest.raises(ValueError, match="personal-class field"):
            validate_webhook_payload({"director_name": "Private Person"})
    finally:
        fixture.close()


def test_removed_endpoint_drains_with_reason() -> None:
    fixture = _fixture(httpx.MockTransport(lambda request: httpx.Response(204, request=request)))
    try:
        row = fixture.queue()
        assert fixture.endpoints.remove(fixture.user.id)
        outcome = fixture.service.send_due()
        stored = fixture.session.scalar(select(Notification).where(Notification.id == row.id))

        assert outcome[0].state is NotificationState.DEAD_LETTERED
        assert stored is not None
        assert stored.last_error == "webhook endpoint was removed or is disabled"
    finally:
        fixture.close()


def test_slow_endpoint_does_not_block_pipeline() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("receiver did not respond", request=request)

    fixture = _fixture(httpx.MockTransport(handler))
    try:
        row = fixture.queue()
        outcome = fixture.service.send_due(now=_NOW)[0]
        stored = fixture.session.scalar(select(Notification).where(Notification.id == row.id))

        assert outcome.result is not None
        assert outcome.result.status is DeliveryStatus.RETRY
        assert outcome.result.error == "webhook request timed out"
        assert stored is not None
        assert stored.state == NotificationState.PENDING.value
        assert stored.scheduled_for == _NOW + timedelta(seconds=30)
        assert len(calls) == 1
    finally:
        fixture.close()


def _message(
    recipient_id: UUID,
    *,
    payload: dict[str, object] | None = None,
) -> OutboundMessage:
    return OutboundMessage(
        recipient_id=recipient_id,
        channel=NotificationChannel.WEBHOOK,
        template="band_change",
        subject="Covenant Radar: BOR-118",
        body="Covenant moved to act.",
        payload=payload
        or {
            "borrower_reference": "BOR-118",
            "summary": "Covenant moved to act.",
            "details": "Projected crossing is within 60 days.",
        },
        scheduled_for=_NOW,
    )
