"""Pure rules for covenant exceptions and waivers (`T-032`).

Exceptions and waivers are deliberately separate concepts:

* an exception belongs to one covenant version and changes the threshold for
  an inclusive range of financial periods; and
* a waiver belongs to the stable covenant identity and is effective only
  after its approval, for an inclusive calendar-date range.

The resolver functions accept domain facts as well as persistence rows with
the same attributes.  That keeps the rule usable by the engine without
making the domain depend on SQLAlchemy or any other adapter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

_PERIOD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^FY(?P<year>\d{2}|\d{4})Q(?P<quarter>[1-4])$"
)
_APPROVED_STATE: Final[str] = "approved"
_PERIOD_MAX_LENGTH: Final[int] = 20


@dataclass(frozen=True, slots=True)
class ExceptionFacts:
    """The persistence-neutral fields needed to apply one exception.

    ``from_period`` and ``to_period`` are inclusive.  Financial period
    labels are ordered by financial year and quarter, not by their textual
    representation, so ``FY27Q2`` correctly follows ``FY27Q1``.
    """

    from_period: str
    to_period: str
    relaxed_threshold: Decimal | None = None
    id: UUID | None = None
    covenant_version_id: UUID | None = None
    approved_by_id: UUID | None = None

    def __post_init__(self) -> None:
        from_period = normalise_period(self.from_period)
        to_period = normalise_period(self.to_period)
        if period_key(to_period) < period_key(from_period):
            raise ValueError("An exception's to_period must not precede its from_period.")
        object.__setattr__(self, "from_period", from_period)
        object.__setattr__(self, "to_period", to_period)
        if self.relaxed_threshold is not None:
            _validate_decimal(self.relaxed_threshold, "relaxed_threshold")
        _validate_optional_uuid(self.id, "id")
        _validate_optional_uuid(self.covenant_version_id, "covenant_version_id")
        _validate_optional_uuid(self.approved_by_id, "approved_by_id")


@dataclass(frozen=True, slots=True)
class WaiverFacts:
    """The persistence-neutral fields needed to apply one waiver.

    Calendar windows are inclusive at both ends.  A ``None`` ``to_date`` is
    open-ended.  ``state`` is intentionally retained alongside
    ``approved_by_id`` because the state transition is the authoritative
    approval decision; the actor column is its provenance.
    """

    from_date: date
    to_date: date | None = None
    scope: str | None = None
    reason: str | None = None
    id: UUID | None = None
    covenant_id: UUID | None = None
    state: str = "requested"
    approved_by_id: UUID | None = None
    approved: bool | None = None

    def __post_init__(self) -> None:
        _validate_calendar_date(self.from_date, "from_date")
        if self.to_date is not None:
            _validate_calendar_date(self.to_date, "to_date")
            if self.to_date < self.from_date:
                raise ValueError("A waiver's to_date must not precede its from_date.")
        if self.scope is not None and not isinstance(self.scope, str):
            raise TypeError("Waiver scope must be text or None.")
        if not isinstance(self.state, str) or not self.state.strip():
            raise ValueError("A waiver state must be a non-empty string.")
        if self.approved is not None and not isinstance(self.approved, bool):
            raise TypeError("Waiver approved must be boolean or None.")
        _validate_optional_uuid(self.id, "id")
        _validate_optional_uuid(self.covenant_id, "covenant_id")
        _validate_optional_uuid(self.approved_by_id, "approved_by_id")

    @property
    def is_approved(self) -> bool:
        """Whether this waiver's persisted lifecycle says it is approved."""

        return (
            self.approved
            if self.approved is not None
            else self.state.strip().lower() == _APPROVED_STATE
        )


def normalise_period(value: object) -> str:
    """Validate and canonicalise a financial-period label."""

    value = _period_label(value)
    if not isinstance(value, str):
        raise TypeError("Financial period must be a string such as 'FY27Q2'.")
    period = value.strip().upper()
    if len(period) > _PERIOD_MAX_LENGTH or _PERIOD_PATTERN.fullmatch(period) is None:
        raise ValueError("Financial period must use the FYyyQn format, for example FY27Q2.")
    return period


def period_key(value: object) -> tuple[int, int]:
    """Return a chronological key for a canonical or canonicalisable label."""

    period = normalise_period(value)
    match = _PERIOD_PATTERN.fullmatch(period)
    if match is None:  # pragma: no cover - normalise_period already rejects it
        raise ValueError(f"Invalid financial period: {value!r}.")
    year = int(match.group("year"))
    if len(match.group("year")) == 2:
        year += 2000
    return year, int(match.group("quarter"))


def validate_exception_window(from_period: str, to_period: str) -> tuple[str, str]:
    """Validate one inclusive exception window and return its labels."""

    start = normalise_period(from_period)
    end = normalise_period(to_period)
    if period_key(end) < period_key(start):
        raise ValueError("An exception's to_period must not precede its from_period.")
    return start, end


def exception_windows_overlap(
    from_period_a: str,
    to_period_a: str,
    from_period_b: str,
    to_period_b: str,
) -> bool:
    """Return whether two inclusive financial-period windows intersect."""

    start_a, end_a = validate_exception_window(from_period_a, to_period_a)
    start_b, end_b = validate_exception_window(from_period_b, to_period_b)
    return period_key(start_a) <= period_key(end_b) and period_key(start_b) <= period_key(end_a)


def validate_no_overlapping_exceptions(
    from_period: str,
    to_period: str,
    existing: Iterable[object],
) -> None:
    """Reject a candidate that overlaps any existing exception window."""

    start, end = validate_exception_window(from_period, to_period)
    for current in existing:
        facts = to_exception_facts(current)
        if exception_windows_overlap(start, end, facts.from_period, facts.to_period):
            raise ValueError(
                f"Exception window {start} to {end} overlaps the existing window "
                f"{facts.from_period} to {facts.to_period}."
            )


def resolve_exception(version: object, period: str) -> ExceptionFacts | None:
    """Return the exception in force for ``version`` and ``period``.

    The version is expected to expose ``exceptions`` (or the compatibility
    spelling ``covenant_exceptions``).  Invalid or ambiguous persisted data
    is rejected rather than silently selecting a different threshold.  The
    registry prevents overlap, so a deterministic first match is safe here;
    sorting makes the result stable for adapter implementations that do not
    preserve query order.
    """

    requested_period = period_key(period)
    version_id = _optional_uuid_attribute(version, "id")
    candidates = _related_values(version, "exceptions", "covenant_exceptions")
    matching: list[ExceptionFacts] = []
    for candidate in candidates:
        facts = to_exception_facts(candidate)
        candidate_version_id = facts.covenant_version_id
        if version_id is not None and candidate_version_id is not None:
            if candidate_version_id != version_id:
                continue
        if period_key(facts.from_period) <= requested_period <= period_key(facts.to_period):
            matching.append(facts)
    if not matching:
        return None
    if len(matching) > 1:
        raise ValueError(
            f"More than one exception is in force for financial period {period!r}; "
            "overlapping exception windows must be corrected before testing."
        )
    matching.sort(
        key=lambda item: (period_key(item.from_period), period_key(item.to_period), str(item.id))
    )
    return matching[0]


def resolve_waiver(covenant: object, as_of: date) -> WaiverFacts | None:
    """Return the approved waiver in force on the inclusive calendar date."""

    _validate_calendar_date(as_of, "as_of")
    covenant_id = _optional_uuid_attribute(covenant, "id")
    candidates = _related_values(covenant, "waivers", "covenant_waivers")
    matching: list[WaiverFacts] = []
    for candidate in candidates:
        facts = to_waiver_facts(candidate)
        if covenant_id is not None and facts.covenant_id is not None:
            if facts.covenant_id != covenant_id:
                continue
        if not facts.is_approved:
            continue
        if facts.from_date <= as_of and (facts.to_date is None or as_of <= facts.to_date):
            matching.append(facts)
    if not matching:
        return None
    matching.sort(
        key=lambda item: (
            item.from_date,
            item.to_date is None,
            item.to_date or date.max,
            str(item.id),
        ),
        reverse=True,
    )
    return matching[0]


def to_exception_facts(value: object) -> ExceptionFacts:
    """Convert a domain fact or an adapter row to the resolver's fact shape."""

    if isinstance(value, ExceptionFacts):
        return value
    return ExceptionFacts(
        from_period=_required_string_attribute(value, "from_period"),
        to_period=_required_string_attribute(value, "to_period"),
        relaxed_threshold=_optional_decimal_attribute(value, "relaxed_threshold"),
        id=_optional_uuid_attribute(value, "id"),
        covenant_version_id=_optional_uuid_attribute(value, "covenant_version_id"),
        approved_by_id=_optional_uuid_attribute(value, "approved_by_id"),
    )


def to_waiver_facts(value: object) -> WaiverFacts:
    """Convert a domain fact or an adapter row to the resolver's fact shape."""

    if isinstance(value, WaiverFacts):
        return value
    return WaiverFacts(
        from_date=_required_date_attribute(value, "from_date"),
        to_date=_optional_date_attribute(value, "to_date"),
        scope=_optional_string_attribute(value, "scope"),
        reason=_optional_string_attribute(value, "reason"),
        id=_optional_uuid_attribute(value, "id"),
        covenant_id=_optional_uuid_attribute(value, "covenant_id"),
        state=_required_string_attribute(value, "state", default="requested"),
        approved_by_id=_optional_uuid_attribute(value, "approved_by_id"),
        approved=_optional_bool_attribute(value, "approved"),
    )


def _related_values(owner: object, *names: str) -> tuple[object, ...]:
    for name in names:
        if isinstance(owner, Mapping):
            if name not in owner:
                continue
            value = owner[name]
        else:
            sentinel = object()
            value = getattr(owner, name, sentinel)
            if value is sentinel:
                continue
        if value is None:
            return ()
        if isinstance(value, Iterable) and not isinstance(value, str | bytes | Mapping):
            return tuple(value)
        raise TypeError(f"{name} must be an iterable of records.")
    return ()


def _required_attribute(owner: object, name: str) -> object:
    value = _attribute(owner, name)
    if value is None:
        raise ValueError(f"A persisted record is missing required field {name!r}.")
    return value


def _attribute(owner: object, name: str, default: object = None) -> object:
    if isinstance(owner, Mapping):
        return owner.get(name, default)
    return getattr(owner, name, default)


def _period_label(value: object) -> object:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("fy_label", "period", "label"):
            candidate = value.get(key)
            if candidate is not None:
                return candidate
    for name in ("fy_label", "period", "label"):
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return value


def _required_string_attribute(owner: object, name: str, default: str | None = None) -> str:
    value = _attribute(owner, name, default)
    if value is None:
        raise ValueError(f"A persisted record is missing required field {name!r}.")
    if not isinstance(value, str):
        raise TypeError(f"A persisted record field {name!r} must be a string.")
    return value


def _required_date_attribute(owner: object, name: str) -> date:
    value = _required_attribute(owner, name)
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"A persisted record field {name!r} must be a calendar date.")
    return value


def _optional_string_attribute(owner: object, name: str) -> str | None:
    value = _attribute(owner, name)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"A persisted record field {name!r} must be a string or None.")
    return value


def _optional_date_attribute(owner: object, name: str) -> date | None:
    value = _attribute(owner, name)
    if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
        raise TypeError(f"A persisted record field {name!r} must be a date or None.")
    return value


def _optional_decimal_attribute(owner: object, name: str) -> Decimal | None:
    value = _attribute(owner, name)
    if value is not None:
        if not isinstance(value, Decimal):
            raise TypeError(f"A persisted record field {name!r} must be a Decimal or None.")
        _validate_decimal(value, name)
    return value


def _optional_bool_attribute(owner: object, name: str) -> bool | None:
    value = _attribute(owner, name)
    if value is not None and not isinstance(value, bool):
        raise TypeError(f"A persisted record field {name!r} must be a boolean or None.")
    return value


def _optional_uuid_attribute(owner: object, name: str) -> UUID | None:
    value = owner.get(name) if isinstance(owner, Mapping) else getattr(owner, name, None)
    if value is None:
        return None
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID when supplied.")
    return value


def _validate_optional_uuid(value: UUID | None, field_name: str) -> None:
    if value is not None and not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID or None.")


def _validate_decimal(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{field_name} must be a finite Decimal or None.")


def _validate_calendar_date(value: date, field_name: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field_name} must be a calendar date.")


__all__ = [
    "ExceptionFacts",
    "WaiverFacts",
    "exception_windows_overlap",
    "normalise_period",
    "period_key",
    "resolve_exception",
    "resolve_waiver",
    "to_exception_facts",
    "to_waiver_facts",
    "validate_exception_window",
    "validate_no_overlapping_exceptions",
]
