"""Pure cure-period rules for covenant tests (`T-032`).

The original failing test is never mutated.  A cure result is a projection
over that test and any later retests, so the original ``breach`` row and a
passing retest remain independently auditable.  Cure windows are open until
the day after their inclusive end date; a passing retest must itself fall
inside the inclusive window.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Final

from covenant_radar.core.errors import ValidationError

FREQUENCY_WINDOW_DAYS: Final[dict[str, int]] = {
    "monthly": 30,
    "quarterly": 90,
    "half_yearly": 180,
    "annual": 365,
}


class CureState(str, Enum):
    """The lifecycle state derived from one failing test and its retests."""

    OPEN = "open"
    CURED = "cured"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class CureResult:
    """A cure state together with the window and retest that caused it."""

    state: CureState
    cure_ends_on: date | None
    retest: object | None = None

    @property
    def status(self) -> str:
        """String form used by service and presentation layers."""

        return self.state.value

    @property
    def verdict(self) -> str:
        """The persisted/display vocabulary corresponding to this state."""

        if self.state is CureState.OPEN:
            return "breach_cure_open"
        if self.state is CureState.CURED:
            return "cured"
        return "breach_confirmed"

    @property
    def window_end(self) -> date | None:
        """Compatibility-facing name for the cure window end date."""

        return self.cure_ends_on


def validate_cure_period(frequency: str, cure_days: int | None) -> None:
    """Reject a cure window that cannot be reached by its test frequency.

    A positive cure window shorter than the contractual testing interval
    would close before the next scheduled retest could occur.  ``on_event``
    has no fixed interval and therefore cannot be rejected by this rule.
    ``None`` and zero mean that no cure window was configured.
    """

    if not isinstance(frequency, str) or not frequency:
        raise ValidationError("covenant_version.frequency is required.", field="frequency")
    if cure_days is None:
        return
    if isinstance(cure_days, bool) or not isinstance(cure_days, int) or cure_days < 0:
        raise ValidationError(
            "covenant_version.cure_days must be a non-negative integer or null.",
            field="cure_days",
        )
    if cure_days == 0 or frequency == "on_event":
        return
    if frequency not in FREQUENCY_WINDOW_DAYS:
        raise ValidationError(
            f"Cannot validate cure period for unknown frequency {frequency!r}.",
            field="frequency",
        )
    if cure_days < FREQUENCY_WINDOW_DAYS[frequency]:
        raise ValidationError(
            f"The {frequency} testing frequency is longer than the {cure_days}-day cure window; "
            "a cure window must reach a possible retest.",
            field="cure_days",
        )


def cure_state(test: object, retests: Sequence[object], thresholds: object) -> CureResult:
    """Derive ``open``, ``cured`` or ``confirmed`` for a failing test.

    ``thresholds`` is accepted as the current evaluation context.  If it
    exposes ``as_of`` or ``as_of_date`` (either as attributes or mapping
    keys), that date determines whether the window has closed.  Otherwise
    the latest test/retest date is used.  The argument is intentionally
    opaque here: the covenant threshold comparison is performed by
    ``evaluate_covenant``; this function only consumes its pass/fail result.
    """

    if not isinstance(retests, Sequence) or isinstance(retests, str | bytes):
        raise TypeError("retests must be a sequence of covenant-test records.")
    initial_verdict = _verdict(test)
    if initial_verdict not in {"breach", "breach_cure_open"}:
        raise ValueError(
            "cure_state requires a covenant test with verdict 'breach' or 'breach_cure_open'."
        )
    test_date = _test_date(test, "test.as_of_date")
    cure_ends_on = _cure_end(test, test_date)
    if cure_ends_on is None:
        return CureResult(CureState.CONFIRMED, None)

    ordered_retests = sorted(
        (
            (_test_date(retest, "retest.as_of_date"), index, retest)
            for index, retest in enumerate(retests)
        ),
        key=lambda item: (item[0], item[1]),
    )
    for retest_date, _index, retest in ordered_retests:
        if test_date <= retest_date <= cure_ends_on and _verdict(retest) == "pass":
            return CureResult(CureState.CURED, cure_ends_on, retest)

    current_date = _current_date(test_date, ordered_retests, thresholds)
    if current_date <= cure_ends_on:
        return CureResult(CureState.OPEN, cure_ends_on)
    return CureResult(CureState.CONFIRMED, cure_ends_on)


def _verdict(record: object) -> str:
    value = (
        record.get("verdict") if isinstance(record, Mapping) else getattr(record, "verdict", None)
    )
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A covenant test must carry a non-empty verdict.")
    return value.strip().lower().replace("-", "_")


def _test_date(record: object, field_name: str) -> date:
    value = (
        record.get("as_of_date")
        if isinstance(record, Mapping)
        else getattr(record, "as_of_date", None)
    )
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field_name} must be a calendar date.")
    return value


def _cure_end(test: object, test_date: date) -> date | None:
    value = (
        test.get("cure_ends_on")
        if isinstance(test, Mapping)
        else getattr(test, "cure_ends_on", None)
    )
    if value is not None:
        if isinstance(value, datetime) or not isinstance(value, date):
            raise TypeError("test.cure_ends_on must be a calendar date or None.")
        if value < test_date:
            raise ValueError("test.cure_ends_on must not precede test.as_of_date.")
        return value
    cure_days = (
        test.get("cure_days") if isinstance(test, Mapping) else getattr(test, "cure_days", None)
    )
    if cure_days is None:
        return None
    if isinstance(cure_days, bool) or not isinstance(cure_days, int) or cure_days < 0:
        raise ValueError("test.cure_days must be a non-negative integer or None.")
    if cure_days == 0:
        return None
    return test_date + timedelta(days=cure_days)


def _current_date(
    test_date: date,
    retests: Sequence[tuple[date, int, object]],
    thresholds: object,
) -> date:
    configured = _context_date(thresholds)
    if configured is not None:
        return configured
    latest_retest = retests[-1][0] if retests else test_date
    return max(test_date, latest_retest)


def _context_date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    keys = ("as_of_date", "as_of", "current_date")
    for key in keys:
        candidate = value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        if candidate is None:
            continue
        if isinstance(candidate, datetime) or not isinstance(candidate, date):
            raise TypeError(f"thresholds.{key} must be a calendar date when supplied.")
        return candidate
    return None


__all__ = [
    "CureResult",
    "CureState",
    "FREQUENCY_WINDOW_DAYS",
    "cure_state",
    "validate_cure_period",
]
