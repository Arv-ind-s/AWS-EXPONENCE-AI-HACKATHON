"""Integration coverage for T-117 email bundling and degraded delivery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import UUID, uuid4

import pytest

from covenant_radar.db.models import Notification
from covenant_radar.notifications.digest import DigestAssembler, DigestWindow
from covenant_radar.notifications.email import DigestTemplateRenderer, EmailNotifier
from covenant_radar.notifications.model import NotificationState
from covenant_radar.ports.notifier import (
    DeliveryResult,
    DeliveryStatus,
    NotificationChannel,
    OutboundMessage,
)

pytestmark = pytest.mark.integration

_START = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_END = _START + timedelta(days=1)
_USER_ID = uuid4()


def _notification(index: int, *, recipient_id: UUID = _USER_ID) -> Notification:
    occurred = _START + timedelta(minutes=index)
    return Notification(
        id=uuid4(),
        recipient_id=recipient_id,
        channel=NotificationChannel.EMAIL.value,
        template="band_change",
        subject_type="borrower",
        subject_id=uuid4(),
        payload={
            "borrower_reference": f"BOR-{index:04d}",
            "summary": f"Borrower {index} moved to act.",
            "details": "Projected covenant crossing remains within 60 days.",
        },
        state=NotificationState.PENDING.value,
        scheduled_for=occurred,
        attempts=0,
        created_at=occurred,
        updated_at=occurred,
        request_id=f"rq-t117-{index}",
    )


class _SmtpCapture:
    messages: list[EmailMessage] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.messages = self.__class__.messages

    def ehlo(self) -> None:
        return None

    def starttls(self) -> None:
        return None

    def send_message(self, message: EmailMessage) -> dict[object, tuple[int, bytes]]:
        self.messages.append(message)
        return {}

    def quit(self) -> None:
        return None


class _InApp:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(DeliveryStatus.SENT, provider_message_id="inapp-1")


def test_hundred_changes_one_email() -> None:
    _SmtpCapture.messages.clear()
    rows = [_notification(index) for index in range(100)]
    assembler = DigestAssembler(base_url="https://radar.example.test")

    digests = assembler.assemble(
        rows,
        window_start=_START,
        window_end=_END,
        recipient_emails={_USER_ID: "risk@example.test"},
    )

    assert len(digests) == 1
    assert len(digests[0].entries) == 100
    notifier = EmailNotifier(
        smtp_host="smtp.example.test",
        smtp_sender="radar@example.test",
        recipient_resolver={_USER_ID: "risk@example.test"},
        smtp_factory=_SmtpCapture,
    )
    result = notifier.send_digest(digests[0])

    assert result.status is DeliveryStatus.SENT
    assert len(_SmtpCapture.messages) == 1
    assert _SmtpCapture.messages[0].get_content_maintype() == "multipart"


def test_empty_digest_not_sent() -> None:
    _SmtpCapture.messages.clear()
    assembler = DigestAssembler()

    assert (
        assembler.assemble(
            (),
            window=DigestWindow(_START, _END),
            recipient_emails={_USER_ID: "risk@example.test"},
        )
        == ()
    )
    assert _SmtpCapture.messages == []


def test_smtp_unconfigured_queues_and_tells_admin() -> None:
    in_app = _InApp()
    digest = DigestAssembler().assemble(
        [_notification(1)],
        window_start=_START,
        window_end=_END,
        recipient_emails={_USER_ID: "risk@example.test"},
    )[0]
    notifier = EmailNotifier(fallback_notifier=in_app)

    result = notifier.send_digest(digest)

    assert notifier.capability.configured is False
    assert "notifications.smtp_host" in notifier.configuration_notice
    assert result.status is DeliveryStatus.SENT
    assert len(in_app.messages) == 1
    assert in_app.messages[0].channel is NotificationChannel.IN_APP


class _HtmlFailureRenderer(DigestTemplateRenderer):
    def render_html(self, _digest: object) -> str:
        raise RuntimeError("html template failure")


def test_html_failure_still_sends_plain_text() -> None:
    _SmtpCapture.messages.clear()
    digest = DigestAssembler().assemble(
        [_notification(2)],
        window_start=_START,
        window_end=_END,
        recipient_emails={_USER_ID: "risk@example.test"},
    )[0]
    notifier = EmailNotifier(
        smtp_host="smtp.example.test",
        smtp_sender="radar@example.test",
        recipient_resolver={_USER_ID: "risk@example.test"},
        renderer=_HtmlFailureRenderer(),
        smtp_factory=_SmtpCapture,
    )

    result = notifier.send_digest(digest)

    assert result.status is DeliveryStatus.SENT
    message = _SmtpCapture.messages[-1]
    assert message.get_content_maintype() == "text"
    assert "Borrower 2 moved to act." in message.get_content()


def test_deep_links_resolve() -> None:
    digest = DigestAssembler(base_url="https://radar.example.test").assemble(
        [_notification(3)],
        window_start=_START,
        window_end=_END,
        recipient_emails={_USER_ID: "risk@example.test"},
    )[0]

    assert digest.entries[0].deep_link == "https://radar.example.test/borrowers/BOR-0003"
    assert UUID(str(digest.entries[0].subject_id))
