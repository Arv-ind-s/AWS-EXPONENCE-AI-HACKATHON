"""Pure case-management rules.

Case persistence and authorisation live in :mod:`covenant_radar.services.cases`.
This package contains only the state machine and the T11 service-level
agreement calculation so both the batch path and interactive workflow use the
same rules.
"""

from covenant_radar.domain.cases.lifecycle import (
    CASE_STATES,
    PERMITTED_TRANSITIONS,
    CaseState,
    CaseTransition,
    permitted_transitions,
    transition,
    transition_result,
    validate_state,
)
from covenant_radar.domain.cases.sla import (
    BusinessCalendar,
    ElapsedHoursCalendar,
    SlaDeadline,
    SlaThresholds,
    derive_sla,
    due_at,
    is_overdue,
    sla_hours,
)

__all__ = [
    "CASE_STATES",
    "PERMITTED_TRANSITIONS",
    "BusinessCalendar",
    "CaseState",
    "CaseTransition",
    "ElapsedHoursCalendar",
    "SlaDeadline",
    "SlaThresholds",
    "derive_sla",
    "due_at",
    "is_overdue",
    "permitted_transitions",
    "sla_hours",
    "transition",
    "transition_result",
    "validate_state",
]
