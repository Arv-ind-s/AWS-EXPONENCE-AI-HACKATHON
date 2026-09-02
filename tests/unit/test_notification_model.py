"""Unit coverage for T-116 notification templates and preference policy."""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import uuid4

import pytest

from covenant_radar.notifications.model import (
    NotificationTemplate,
    TemplateRenderError,
    TemplateSlot,
)
from covenant_radar.notifications.preferences import (
    NON_SUPPRESSIBLE_TEMPLATES,
    NotificationPreference,
)
from covenant_radar.ports.notifier import NotificationChannel

pytestmark = pytest.mark.unit


def test_unfilled_slot_refused() -> None:
    template = NotificationTemplate(
        name="slot_test",
        subject_template="Notice",
        body_template="Review {summary}.",
        slots=(TemplateSlot("summary", str),),
    )

    with pytest.raises(TemplateRenderError, match="unfilled slot 'summary'"):
        template.render({})


def test_quiet_hours_defer_not_drop() -> None:
    preference = NotificationPreference(
        user_id=uuid4(),
        template="band_change",
        channel=NotificationChannel.EMAIL,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(6, 0),
    )
    queued_at = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)

    assert preference.deferred_until(queued_at) == datetime(2026, 9, 1, 0, 30, tzinfo=UTC)
    assert preference.schedule_for(queued_at) == datetime(2026, 9, 1, 0, 30, tzinfo=UTC)


def test_non_suppressible_templates_documented() -> None:
    assert NON_SUPPRESSIBLE_TEMPLATES == frozenset({"security_alert", "system_failure"})

    preference = NotificationPreference(
        user_id=uuid4(),
        template="security_alert",
        channel=NotificationChannel.IN_APP,
        enabled=False,
    )

    assert preference.suppressible is False
    assert preference.allows(template_non_suppressible=True) is True
