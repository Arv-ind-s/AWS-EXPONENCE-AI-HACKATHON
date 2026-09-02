"""The explicit case lifecycle state machine.

The case row is evidence of work performed against a warning.  A state may
therefore only move through this table; callers cannot manufacture an
implicit transition by assigning an arbitrary string.  Closing is terminal:
re-escalation is represented by a new case linked to the closed case in its
append-only history.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from covenant_radar.core.errors import ValidationError


class CaseState(StrEnum):
    """The persisted states supported by the case workflow."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    MONITORING = "monitoring"
    ESCALATED = "escalated"
    CLOSED = "closed"


CASE_STATES: Final[tuple[str, ...]] = tuple(state.value for state in CaseState)

# The transition table is intentionally data, not a chain of conditionals.
# Every state-changing caller uses it, and the mapping is immutable at runtime.
PERMITTED_TRANSITIONS: Final[Mapping[CaseState, tuple[CaseState, ...]]] = MappingProxyType(
    {
        CaseState.OPEN: (
            CaseState.IN_PROGRESS,
            CaseState.MONITORING,
            CaseState.ESCALATED,
            CaseState.CLOSED,
        ),
        CaseState.IN_PROGRESS: (
            CaseState.MONITORING,
            CaseState.ESCALATED,
            CaseState.CLOSED,
        ),
        CaseState.MONITORING: (
            CaseState.IN_PROGRESS,
            CaseState.ESCALATED,
            CaseState.CLOSED,
        ),
        CaseState.ESCALATED: (
            CaseState.IN_PROGRESS,
            CaseState.MONITORING,
            CaseState.CLOSED,
        ),
        CaseState.CLOSED: (),
    }
)

_CLOSURE_REASON_MAX_LENGTH: Final[int] = 200


@dataclass(frozen=True, slots=True)
class CaseTransition:
    """A validated state change suitable for a case-history event."""

    from_state: CaseState
    to_state: CaseState

    def __post_init__(self) -> None:
        source = validate_state(self.from_state)
        target = validate_state(self.to_state)
        if target not in PERMITTED_TRANSITIONS[source]:
            raise _transition_error(source, target)
        object.__setattr__(self, "from_state", source)
        object.__setattr__(self, "to_state", target)


def validate_state(value: CaseState | str) -> CaseState:
    """Normalize one persisted state or reject it with its allowed vocabulary."""

    raw = value.value if isinstance(value, CaseState) else value
    if not isinstance(raw, str):
        raise ValidationError(
            f"Case state must be one of {', '.join(CASE_STATES)}.", field="case.state"
        )
    try:
        return CaseState(raw.strip().lower())
    except ValueError as error:
        raise ValidationError(
            f"Case state {raw!r} is invalid; expected one of {', '.join(CASE_STATES)}.",
            field="case.state",
        ) from error


def permitted_transitions(value: CaseState | str) -> tuple[str, ...]:
    """Return the immutable, ordered target vocabulary for ``value``."""

    state = validate_state(value)
    return tuple(target.value for target in PERMITTED_TRANSITIONS[state])


def transition(
    current: CaseState | str,
    target: CaseState | str,
    *,
    closure_reason: str | None = None,
) -> CaseState:
    """Validate and return ``target`` for one lifecycle transition.

    A close operation always requires a non-blank, bounded reason.  The
    reason itself remains in the case row; callers should not put free-text
    closure notes in a general audit payload.
    """

    result = transition_result(current, target, closure_reason=closure_reason)
    return result.to_state


def transition_result(
    current: CaseState | str,
    target: CaseState | str,
    *,
    closure_reason: str | None = None,
) -> CaseTransition:
    """Return a validated transition with the source and destination states."""

    source = validate_state(current)
    destination = validate_state(target)
    if destination not in PERMITTED_TRANSITIONS[source]:
        raise _transition_error(source, destination)
    if destination is CaseState.CLOSED:
        _validate_closure_reason(closure_reason)
    elif closure_reason is not None:
        raise ValidationError(
            "A closure reason is only valid when transitioning a case to closed.",
            field="case.closure_reason",
        )
    return CaseTransition(source, destination)


def _transition_error(source: CaseState, target: CaseState) -> ValidationError:
    allowed = ", ".join(item.value for item in PERMITTED_TRANSITIONS[source]) or "none"
    return ValidationError(
        f"Transition from {source.value!r} to {target.value!r} is not permitted. "
        f"Permitted transitions from {source.value!r}: {allowed}.",
        field="case.state",
    )


def _validate_closure_reason(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("A closure reason is required.", field="case.closure_reason")
    normalized = value.strip()
    if len(normalized) > _CLOSURE_REASON_MAX_LENGTH:
        raise ValidationError(
            f"A closure reason must be at most {_CLOSURE_REASON_MAX_LENGTH} characters.",
            field="case.closure_reason",
        )
    return normalized


__all__ = [
    "CASE_STATES",
    "PERMITTED_TRANSITIONS",
    "CaseState",
    "CaseTransition",
    "permitted_transitions",
    "transition",
    "transition_result",
    "validate_state",
]
