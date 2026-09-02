"""Pure evaluation of one covenant against one ratio result.

This module is the implementation of ``C-32``.  It contains no SQLAlchemy,
FastAPI, model-provider or other adapter import.  Persistence is handled by
``services.engine`` after this module has produced the complete, auditable
decision.

The boundary convention is inclusive on the breach side: a ``max`` covenant
breaches at ``value >= threshold`` and a ``min`` covenant breaches at
``value <= threshold``.  Consequently the exact boundary has zero headroom
and a ``breach`` verdict (or ``breach_cure_open`` when a cure period is
configured).  This is the same ``meets or passes the threshold`` convention
used by the forecast crossing stage.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Final
from uuid import UUID

from covenant_radar.domain.covenants.exceptions import ExceptionFacts, WaiverFacts
from covenant_radar.domain.covenants.headroom import signed_headroom
from covenant_radar.domain.ratios.compute import RatioResult
from covenant_radar.domain.ratios.reasons import NotComputableReason

_DIRECTIONS: Final[frozenset[str]] = frozenset({"min", "max"})
_VERDICT_VALUES: Final[frozenset[str]] = frozenset(
    {"pass", "warning", "breach", "breach_cure_open", "stale", "not_computable"}
)
_PERCENT: Final[Decimal] = Decimal("100")


class CovenantVerdict(str, Enum):
    """The closed verdict vocabulary persisted by ``CovenantTest``."""

    PASS = "pass"
    WARNING = "warning"
    BREACH = "breach"
    BREACH_CURE_OPEN = "breach_cure_open"
    STALE = "stale"
    NOT_COMPUTABLE = "not_computable"


class ThresholdSide(str, Enum):
    """The ordinary numeric side on which an observation falls."""

    ABOVE = "above"
    BELOW = "below"
    AT = "at"


@dataclass(frozen=True, slots=True)
class CovenantVersionFacts:
    """Persistence-neutral fields of a covenant version used by the engine."""

    threshold: Decimal
    direction: str
    warning_headroom_pct: Decimal | None = None
    cure_days: int | None = None
    id: UUID | None = None
    covenant_id: UUID | None = None
    version_no: int | None = None

    def __post_init__(self) -> None:
        _validate_decimal(self.threshold, "threshold")
        if self.direction not in _DIRECTIONS:
            raise ValueError("direction must be either 'min' or 'max'.")
        if self.threshold == 0:
            raise ValueError("threshold must not be zero.")
        if self.warning_headroom_pct is not None:
            _validate_decimal(self.warning_headroom_pct, "warning_headroom_pct")
            if self.warning_headroom_pct < 0:
                raise ValueError("warning_headroom_pct must not be negative.")
        if self.cure_days is not None:
            if isinstance(self.cure_days, bool) or not isinstance(self.cure_days, int):
                raise TypeError("cure_days must be a non-negative integer or None.")
            if self.cure_days < 0:
                raise ValueError("cure_days must be a non-negative integer or None.")


@dataclass(frozen=True, slots=True)
class PeriodFacts:
    """The period facts needed for effective terms and staleness decisions.

    ``period_label`` is the canonical ``FYyyQn`` value used by exception
    resolution.  ``as_of_date`` is the date of the test; ``period_end`` is a
    compatibility spelling used by statement adapters when that is the only
    date they carry.  The aliases are normalized in ``__post_init__`` so the
    evaluator has one shape regardless of the adapter that supplied it.
    """

    period_label: str | None = None
    is_complete: bool = True
    last_complete_period: str | None = None
    period_id: UUID | None = None
    as_of_date: date | None = None
    period_end: date | None = None
    fy_label: str | None = None
    last_available_period: str | None = None

    def __post_init__(self) -> None:
        label = self.period_label or self.fy_label
        if label is not None:
            if not isinstance(label, str) or not label.strip():
                raise ValueError("period_label must be non-empty text or None.")
            label = label.strip().upper()
        last = self.last_complete_period or self.last_available_period
        if last is not None:
            if not isinstance(last, str) or not last.strip():
                raise ValueError("last_complete_period must be non-empty text or None.")
            last = last.strip().upper()
        if not isinstance(self.is_complete, bool):
            raise TypeError("is_complete must be a boolean.")
        for value, name in (
            (self.as_of_date, "as_of_date"),
            (self.period_end, "period_end"),
        ):
            if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
                raise TypeError(f"{name} must be a calendar date or None.")
        if self.period_id is not None and not isinstance(self.period_id, UUID):
            raise TypeError("period_id must be a UUID or None.")
        object.__setattr__(self, "period_label", label)
        object.__setattr__(self, "fy_label", label)
        object.__setattr__(self, "last_complete_period", last)
        object.__setattr__(self, "last_available_period", last)

    @property
    def test_date(self) -> date | None:
        """The date the engine should stamp on the resulting test row."""

        return self.as_of_date or self.period_end


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Optional evaluation context shared with cure and later stages.

    Covenant-specific warning thresholds live on ``CovenantVersionFacts``.
    The context intentionally carries only dates here; accepting it in the
    contract keeps the evaluator compatible with the other pure stages while
    preventing a global, unversioned policy value from overriding a signed
    covenant term.
    """

    as_of_date: date | None = None
    current_date: date | None = None

    def __post_init__(self) -> None:
        for value, name in ((self.as_of_date, "as_of_date"), (self.current_date, "current_date")):
            if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
                raise TypeError(f"{name} must be a calendar date or None.")


@dataclass(frozen=True, slots=True)
class CovenantEvaluation:
    """The complete result of one covenant decision."""

    value: Decimal | None
    threshold_used: Decimal | None
    headroom_pct: Decimal | None
    verdict: str
    exception_applied: ExceptionFacts | None = None
    waiver_applied: WaiverFacts | None = None
    cure_ends_on: date | None = None
    thresholds_compared: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    reason: NotComputableReason | None = None
    reason_context: Mapping[str, str] = field(default_factory=dict)
    stale_reason: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in _VERDICT_VALUES:
            raise ValueError(f"Unknown covenant verdict {self.verdict!r}.")
        if self.verdict == CovenantVerdict.NOT_COMPUTABLE.value and self.reason is None:
            raise ValueError("A not-computable evaluation must carry its enumerated reason.")
        if self.verdict != CovenantVerdict.NOT_COMPUTABLE.value and self.reason is not None:
            raise ValueError("Only a not-computable evaluation may carry a ratio reason.")
        if self.value is None and self.headroom_pct is not None:
            raise ValueError("An evaluation without a value cannot carry headroom.")
        if self.value is not None:
            _validate_decimal(self.value, "value")
        if self.threshold_used is not None:
            _validate_decimal(self.threshold_used, "threshold_used")
        if self.headroom_pct is not None:
            _validate_decimal(self.headroom_pct, "headroom_pct")
        for comparison in self.thresholds_compared:
            _validate_comparison(comparison)

    @property
    def exception(self) -> ExceptionFacts | None:
        """Compatibility spelling for the applied exception."""

        return self.exception_applied

    @property
    def waiver(self) -> WaiverFacts | None:
        """Compatibility spelling for the applied waiver."""

        return self.waiver_applied

    @property
    def cure_end_date(self) -> date | None:
        """Compatibility spelling for the inclusive cure-window end."""

        return self.cure_ends_on

    @property
    def exception_id(self) -> UUID | None:
        """The identifier of the applied exception, when it is persisted."""

        return self.exception_applied.id if self.exception_applied is not None else None

    @property
    def waiver_id(self) -> UUID | None:
        """The identifier of the applied waiver, when it is persisted."""

        return self.waiver_applied.id if self.waiver_applied is not None else None

    @property
    def staleness_reason(self) -> str | None:
        """Compatibility spelling for the missing-period explanation."""

        return self.stale_reason

    @property
    def not_computable_reason(self) -> NotComputableReason | None:
        """The persisted reason for a not-computable result."""

        return self.reason

    @property
    def headroom(self) -> Decimal | None:
        """Compatibility spelling for the signed headroom percentage."""

        return self.headroom_pct


def evaluate_covenant(
    version: CovenantVersionFacts | object,
    ratio: RatioResult | object,
    period: PeriodFacts | object,
    exception: ExceptionFacts | object | None,
    waiver: WaiverFacts | object | None,
    thresholds: Thresholds | object,
) -> CovenantEvaluation:
    """Evaluate one ratio against the contractual threshold.

    The function never lets an arithmetic failure escape.  Validated domain
    facts follow the ordinary decision table; malformed adapter facts return
    a conservative ``not_computable`` result with the closed ratio reason
    ``FORMULA_NOT_COMPUTABLE`` rather than fabricating a value.
    """

    try:
        base_threshold = _decimal_field(version, "threshold")
        direction = _text_field(version, "direction")
        warning_headroom = _optional_decimal_field(version, "warning_headroom_pct")
        cure_days = _optional_non_negative_int(version, "cure_days")
        test_date = _period_date(period)
        complete = _bool_field(period, "is_complete", default=True)

        if direction not in _DIRECTIONS or base_threshold == 0:
            return _invalid_evaluation(base_threshold)

        active_exception = _normalise_exception(exception)
        threshold_used = base_threshold
        if active_exception is not None and active_exception.relaxed_threshold is not None:
            relaxed = active_exception.relaxed_threshold
            if relaxed.is_finite() and relaxed != 0:
                threshold_used = relaxed

        active_waiver = _approved_waiver(waiver)

        ratio_reason = _ratio_reason(ratio)
        if not complete or ratio_reason == NotComputableReason.PERIOD_INCOMPLETE:
            stale_reason = _stale_reason(period)
            return CovenantEvaluation(
                value=None,
                threshold_used=threshold_used,
                headroom_pct=None,
                verdict=CovenantVerdict.STALE.value,
                exception_applied=active_exception,
                waiver_applied=active_waiver,
                reason_context={
                    "last_complete_period": stale_reason.removeprefix("last complete period: ")
                },
                stale_reason=stale_reason,
            )

        ratio_computable = _bool_field(ratio, "computable", default=False)
        if not ratio_computable:
            return CovenantEvaluation(
                value=None,
                threshold_used=threshold_used,
                headroom_pct=None,
                verdict=CovenantVerdict.NOT_COMPUTABLE.value,
                exception_applied=active_exception,
                waiver_applied=active_waiver,
                reason=ratio_reason,
                reason_context=_mapping_field(ratio, "reason_context"),
            )

        value = _decimal_field(ratio, "value")
        if not value.is_finite():
            return _invalid_evaluation(threshold_used)
        headroom = signed_headroom(value, threshold_used, direction)
        comparisons = [
            _comparison("covenant_threshold", threshold_used, value),
        ]

        warning_threshold = _warning_threshold(threshold_used, direction, warning_headroom)
        if warning_threshold is not None:
            comparisons.append(_comparison("warning_threshold", warning_threshold, value))

        intrinsic_outcome = _optional_bool_field(ratio, "outcome")
        if active_waiver is not None:
            verdict = CovenantVerdict.PASS.value
        elif intrinsic_outcome is not None:
            verdict = _verdict_for_condition(intrinsic_outcome, cure_days, test_date)
        elif _at_or_beyond_boundary(value, threshold_used, direction):
            verdict = _breach_verdict(cure_days, test_date)
        elif warning_headroom is not None and headroom <= warning_headroom:
            verdict = CovenantVerdict.WARNING.value
        else:
            verdict = CovenantVerdict.PASS.value

        cure_end = _cure_end(cure_days, test_date) if verdict == "breach_cure_open" else None
        return CovenantEvaluation(
            value=value,
            threshold_used=threshold_used,
            headroom_pct=headroom,
            verdict=verdict,
            exception_applied=active_exception,
            waiver_applied=active_waiver,
            cure_ends_on=cure_end,
            thresholds_compared=tuple(comparisons),
        )
    except (ArithmeticError, KeyError, TypeError, ValueError, AttributeError):
        threshold = _safe_decimal_field(version, "threshold")
        return _invalid_evaluation(threshold)


def _at_or_beyond_boundary(value: Decimal, threshold: Decimal, direction: str) -> bool:
    if direction == "min":
        return value <= threshold
    return value >= threshold


def _breach_verdict(cure_days: int | None, test_date: date | None) -> str:
    if cure_days is not None and cure_days > 0 and test_date is not None:
        return CovenantVerdict.BREACH_CURE_OPEN.value
    return CovenantVerdict.BREACH.value


def _verdict_for_condition(outcome: bool, cure_days: int | None, test_date: date | None) -> str:
    if outcome:
        return CovenantVerdict.PASS.value
    return _breach_verdict(cure_days, test_date)


def _cure_end(cure_days: int | None, test_date: date | None) -> date | None:
    if cure_days is None or cure_days <= 0 or test_date is None:
        return None
    return test_date + timedelta(days=cure_days)


def _warning_threshold(
    threshold: Decimal, direction: str, warning_headroom_pct: Decimal | None
) -> Decimal | None:
    if warning_headroom_pct is None or warning_headroom_pct < 0:
        return None
    fraction = warning_headroom_pct / _PERCENT
    if direction == "min":
        return threshold + abs(threshold) * fraction
    return threshold - abs(threshold) * fraction


def _comparison(name: str, threshold: Decimal, observed: Decimal) -> Mapping[str, object]:
    if observed == threshold:
        side = ThresholdSide.AT.value
    elif observed > threshold:
        side = ThresholdSide.ABOVE.value
    else:
        side = ThresholdSide.BELOW.value
    return {"name": name, "value": threshold, "observed": observed, "side": side}


def _validate_comparison(comparison: Mapping[str, object]) -> None:
    required = {"name", "value", "observed", "side"}
    if set(comparison) != required:
        raise ValueError("Every threshold comparison must contain name, value, observed and side.")
    if comparison["side"] not in {side.value for side in ThresholdSide}:
        raise ValueError("A threshold comparison side must be above, below or at.")


def _invalid_evaluation(threshold: Decimal | None) -> CovenantEvaluation:
    return CovenantEvaluation(
        value=None,
        threshold_used=threshold,
        headroom_pct=None,
        verdict=CovenantVerdict.NOT_COMPUTABLE.value,
        reason=NotComputableReason.FORMULA_NOT_COMPUTABLE,
        reason_context={"reason": "invalid covenant evaluation facts"},
    )


def _stale_reason(period: object) -> str:
    last = _field(period, "last_complete_period") or _field(period, "last_available_period")
    if isinstance(last, str) and last.strip():
        return f"last complete period: {last.strip().upper()}"
    return "last complete period: none available"


def _ratio_reason(ratio: object) -> NotComputableReason:
    value = _field(ratio, "reason")
    if isinstance(value, NotComputableReason):
        return value
    if isinstance(value, str):
        try:
            return NotComputableReason(value)
        except ValueError:
            pass
    return NotComputableReason.FORMULA_NOT_COMPUTABLE


def _normalise_exception(value: object | None) -> ExceptionFacts | None:
    if value is None:
        return None
    if isinstance(value, ExceptionFacts):
        return value
    from covenant_radar.domain.covenants.exceptions import to_exception_facts

    return to_exception_facts(value)


def _approved_waiver(value: object | None) -> WaiverFacts | None:
    if value is None:
        return None
    if isinstance(value, WaiverFacts):
        return value if value.is_approved else None
    from covenant_radar.domain.covenants.exceptions import to_waiver_facts

    facts = to_waiver_facts(value)
    return facts if facts.is_approved else None


def _period_date(period: object) -> date | None:
    for name in ("as_of_date", "period_end", "test_date"):
        value = _field(period, name)
        if value is not None:
            if isinstance(value, datetime) or not isinstance(value, date):
                raise TypeError(f"period.{name} must be a calendar date or None.")
            return value
    if isinstance(period, date) and not isinstance(period, datetime):
        return period
    return None


def _field(owner: object, name: str) -> object | None:
    if isinstance(owner, Mapping):
        return owner.get(name)
    return getattr(owner, name, None)


def _text_field(owner: object, name: str) -> str:
    value = _field(owner, name)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be non-empty text.")
    return value.strip().lower()


def _decimal_field(owner: object, name: str) -> Decimal:
    value = _field(owner, name)
    _validate_decimal(value, name)
    if not isinstance(value, Decimal):  # narrowed explicitly for static checkers
        raise TypeError(f"{name} must be a Decimal.")
    return value


def _safe_decimal_field(owner: object, name: str) -> Decimal | None:
    value = _field(owner, name)
    return value if isinstance(value, Decimal) and value.is_finite() else None


def _optional_decimal_field(owner: object, name: str) -> Decimal | None:
    value = _field(owner, name)
    if value is None:
        return None
    _validate_decimal(value, name)
    if not isinstance(value, Decimal):  # narrowed explicitly for static checkers
        raise TypeError(f"{name} must be a Decimal.")
    return value


def _validate_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal.")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite.")


def _optional_non_negative_int(owner: object, name: str) -> int | None:
    value = _field(owner, name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None.")
    return value


def _bool_field(owner: object, name: str, *, default: bool) -> bool:
    value = _field(owner, name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean.")
    return value


def _optional_bool_field(owner: object, name: str) -> bool | None:
    value = _field(owner, name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean or None.")
    return value


def _mapping_field(owner: object, name: str) -> Mapping[str, str]:
    value = _field(owner, name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


__all__ = [
    "CovenantEvaluation",
    "CovenantVerdict",
    "CovenantVersionFacts",
    "PeriodFacts",
    "ThresholdSide",
    "Thresholds",
    "evaluate_covenant",
]
