"""Built-in notification templates.

Templates are code-owned and versioned.  Later administration work may
choose which registered template is active, but a database value never
becomes an executable format string.
"""

from __future__ import annotations

from covenant_radar.notifications.model import NotificationTemplate, TemplateRegistry, TemplateSlot
from covenant_radar.security.permissions import Permission


def _slot(
    name: str,
    expected_type: type[object] | tuple[type[object], ...],
    *,
    required: bool = True,
    required_permission: Permission | str | None = None,
    sensitive: bool = False,
) -> TemplateSlot:
    return TemplateSlot(
        name,
        expected_type,
        required=required,
        required_permission=required_permission,
        sensitive=sensitive,
    )


BAND_CHANGE_TEMPLATE = NotificationTemplate(
    name="band_change",
    subject_template="Covenant Radar: {borrower_reference}",
    body_template="{summary}\n{details}",
    slots=(
        _slot("borrower_reference", str),
        _slot("summary", str, required=False),
        _slot("details", str, required=False),
        # Carried so a band change raised against a case deep-links to that
        # case: `digest.deep_link` reads `case_reference` for a "case"
        # subject and otherwise falls back to the opaque id, which the
        # reference-keyed `/cases/{reference}` route cannot resolve.
        _slot("case_reference", str, required=False),
    ),
)

MORNING_QUEUE_TEMPLATE = NotificationTemplate(
    name="morning_queue",
    subject_template="Covenant Radar morning queue",
    body_template="{summary}\n{entries}",
    slots=(
        _slot("summary", str),
        _slot("entries", str, required=False),
    ),
)

SLA_BREACH_TEMPLATE = NotificationTemplate(
    name="sla_breach",
    subject_template="Covenant Radar SLA breach: {case_reference}",
    body_template="{summary}",
    slots=(_slot("case_reference", str), _slot("summary", str)),
)

CERTIFICATE_DUE_TEMPLATE = NotificationTemplate(
    name="certificate_due",
    subject_template="Covenant Radar certificate reminder: {borrower_reference}",
    body_template="{summary}",
    slots=(_slot("borrower_reference", str), _slot("summary", str)),
)

JOB_FAILURE_TEMPLATE = NotificationTemplate(
    name="job_failure",
    subject_template="Covenant Radar job failure: {job_name}",
    body_template="{summary}",
    slots=(_slot("job_name", str), _slot("summary", str)),
)

SECURITY_ALERT_TEMPLATE = NotificationTemplate(
    name="security_alert",
    subject_template="Covenant Radar security alert",
    body_template="{message}",
    slots=(_slot("message", str),),
    non_suppressible=True,
)

SYSTEM_FAILURE_TEMPLATE = NotificationTemplate(
    name="system_failure",
    subject_template="Covenant Radar system failure",
    body_template="{message}",
    slots=(_slot("message", str),),
    non_suppressible=True,
)

DEFAULT_TEMPLATES = (
    BAND_CHANGE_TEMPLATE,
    MORNING_QUEUE_TEMPLATE,
    SLA_BREACH_TEMPLATE,
    CERTIFICATE_DUE_TEMPLATE,
    JOB_FAILURE_TEMPLATE,
    SECURITY_ALERT_TEMPLATE,
    SYSTEM_FAILURE_TEMPLATE,
)
DEFAULT_TEMPLATE_REGISTRY = TemplateRegistry(DEFAULT_TEMPLATES)


__all__ = [
    "BAND_CHANGE_TEMPLATE",
    "CERTIFICATE_DUE_TEMPLATE",
    "DEFAULT_TEMPLATES",
    "DEFAULT_TEMPLATE_REGISTRY",
    "JOB_FAILURE_TEMPLATE",
    "MORNING_QUEUE_TEMPLATE",
    "SECURITY_ALERT_TEMPLATE",
    "SLA_BREACH_TEMPLATE",
    "SYSTEM_FAILURE_TEMPLATE",
]
