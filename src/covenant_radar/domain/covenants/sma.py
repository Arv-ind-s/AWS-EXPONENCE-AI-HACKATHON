"""Supervisory SMA banding derived from facility account conduct.

The mapping in this module is deliberately small and deterministic.  It is
kept in the domain layer so every adapter (the engine, exports and future
connectors) uses the same boundary behaviour and no adapter can silently
clamp an out-of-range value into a valid supervisory band.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from uuid import UUID


class SmaBand(str, Enum):
    """The closed SMA vocabulary used by the supervisory framework.

    ``NONE`` represents an account that is not overdue, or for which the
    requested conduct is absent.  A missing record must still carry a reason
    in :class:`FacilitySmaDerivation`; it is never treated as current merely
    because both cases have no SMA band.
    """

    NONE = "none"
    SMA_0 = "SMA-0"
    SMA_1 = "SMA-1"
    SMA_2 = "SMA-2"
    BEYOND = "beyond"

    @property
    def severity(self) -> int:
        """Return the ordering used when rolling facilities up to a borrower."""

        return {
            SmaBand.NONE: 0,
            SmaBand.SMA_0: 1,
            SmaBand.SMA_1: 2,
            SmaBand.SMA_2: 3,
            SmaBand.BEYOND: 4,
        }[self]


type ConductIdentifier = str | UUID


@dataclass(frozen=True, slots=True)
class FacilityConductFacts:
    """Persistence-neutral conduct facts consumed by SMA banding."""

    facility_id: ConductIdentifier
    as_of_date: date
    days_past_due: int | None
    source_id: ConductIdentifier | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.facility_id, str | UUID) or (
            isinstance(self.facility_id, str) and not self.facility_id.strip()
        ):
            raise TypeError("facility_id must be a non-empty string or UUID.")
        if isinstance(self.as_of_date, datetime) or not isinstance(self.as_of_date, date):
            raise TypeError("as_of_date must be a calendar date.")
        if self.days_past_due is not None:
            if isinstance(self.days_past_due, bool) or not isinstance(self.days_past_due, int):
                raise TypeError("days_past_due must be an integer or None.")
            if self.days_past_due < 0:
                raise ValueError("days_past_due must not be negative.")
        if self.source_id is not None:
            if not isinstance(self.source_id, str | UUID):
                raise TypeError("source_id must be a string, UUID or None.")
            if isinstance(self.source_id, str) and not self.source_id.strip():
                raise ValueError("source_id must be non-empty text or None.")


@dataclass(frozen=True, slots=True)
class FacilitySmaDerivation:
    """The auditable result for one facility on one conduct date."""

    facility_id: ConductIdentifier
    as_of_date: date
    band: SmaBand
    days_past_due: int | None
    reason: str | None = None
    source_id: ConductIdentifier | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.facility_id, str | UUID) or (
            isinstance(self.facility_id, str) and not self.facility_id.strip()
        ):
            raise TypeError("facility_id must be a non-empty string or UUID.")
        if isinstance(self.as_of_date, datetime) or not isinstance(self.as_of_date, date):
            raise TypeError("as_of_date must be a calendar date.")
        if not isinstance(self.band, SmaBand):
            raise TypeError("band must be a SmaBand.")
        if self.days_past_due is not None:
            if isinstance(self.days_past_due, bool) or not isinstance(self.days_past_due, int):
                raise TypeError("days_past_due must be an integer or None.")
            if self.days_past_due < 0:
                raise ValueError("days_past_due must not be negative.")
            if sma_band(self.days_past_due) is not self.band:
                raise ValueError("band must match the supplied days_past_due.")
        elif self.band is not SmaBand.NONE:
            raise ValueError("A band other than none requires days_past_due.")
        elif self.reason is None:
            raise ValueError("A missing days_past_due value requires a reason.")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason must be non-empty text or None.")
        if self.source_id is not None:
            if not isinstance(self.source_id, str | UUID):
                raise TypeError("source_id must be a string, UUID or None.")
            if isinstance(self.source_id, str) and not self.source_id.strip():
                raise ValueError("source_id must be non-empty text or None.")

    @property
    def sma_band(self) -> SmaBand:
        """Compatibility spelling for consumers that use the contract name."""

        return self.band


@dataclass(frozen=True, slots=True)
class BorrowerSmaDerivation:
    """The worst SMA band and all facility-level derivations for a borrower."""

    borrower_id: ConductIdentifier | None
    as_of_date: date
    band: SmaBand
    facilities: tuple[FacilitySmaDerivation, ...] = field(default_factory=tuple)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.borrower_id is not None and not isinstance(self.borrower_id, str | UUID):
            raise TypeError("borrower_id must be a string, UUID or None.")
        if isinstance(self.as_of_date, datetime) or not isinstance(self.as_of_date, date):
            raise TypeError("as_of_date must be a calendar date.")
        if not isinstance(self.band, SmaBand):
            raise TypeError("band must be a SmaBand.")
        if not isinstance(self.facilities, tuple) or not all(
            isinstance(item, FacilitySmaDerivation) for item in self.facilities
        ):
            raise TypeError("facilities must be a tuple of FacilitySmaDerivation values.")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason must be non-empty text or None.")

    @property
    def sma_band(self) -> SmaBand:
        """Compatibility spelling for consumers that use the contract name."""

        return self.band

    @property
    def worst_facility(self) -> FacilitySmaDerivation | None:
        """Return the facility that determined the borrower band, if any."""

        candidates = [item for item in self.facilities if item.band != SmaBand.NONE]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.band.severity)


MISSING_CONDUCT_REASON = "no facility conduct was recorded for the requested date"
MISSING_DAYS_REASON = "facility conduct has no days_past_due value"


def sma_band(days_past_due: int) -> SmaBand:
    """Map days past due to the exact supervisory SMA band.

    Zero is a valid, non-overdue observation and returns ``SmaBand.NONE``.
    Values beyond 90 days are deliberately represented by ``BEYOND`` rather
    than being clamped to SMA-2.  Negative values are data defects and raise.
    """

    if isinstance(days_past_due, bool) or not isinstance(days_past_due, int):
        raise TypeError("days_past_due must be an integer.")
    if days_past_due < 0:
        raise ValueError("days_past_due must not be negative.")
    if days_past_due == 0:
        return SmaBand.NONE
    if days_past_due <= 30:
        return SmaBand.SMA_0
    if days_past_due <= 60:
        return SmaBand.SMA_1
    if days_past_due <= 90:
        return SmaBand.SMA_2
    return SmaBand.BEYOND


def derive_facility_sma(
    conduct: object | None,
    *,
    facility_id: ConductIdentifier,
    as_of_date: date,
) -> FacilitySmaDerivation:
    """Derive one facility's band without treating absent data as current.

    The input may be a :class:`FacilityConductFacts`, a persistence row, or a
    mapping with the conduct field names.  The service boundary uses this
    adapter-friendly shape while the actual banding remains the pure
    :func:`sma_band` function above.
    """

    _validate_date(as_of_date)
    validated_facility_id = _validated_identifier(facility_id, "facility_id")
    if conduct is None:
        return FacilitySmaDerivation(
            facility_id=validated_facility_id,
            as_of_date=as_of_date,
            band=SmaBand.NONE,
            days_past_due=None,
            reason=MISSING_CONDUCT_REASON,
        )

    conduct_date = _field(conduct, "as_of_date")
    if conduct_date is not None:
        _validate_date(conduct_date, "conduct.as_of_date")
        if conduct_date != as_of_date:
            return FacilitySmaDerivation(
                facility_id=validated_facility_id,
                as_of_date=as_of_date,
                band=SmaBand.NONE,
                days_past_due=None,
                reason=MISSING_CONDUCT_REASON,
                source_id=_source_id(conduct),
            )

    days = _field(conduct, "days_past_due")
    if days is None:
        return FacilitySmaDerivation(
            facility_id=validated_facility_id,
            as_of_date=as_of_date,
            band=SmaBand.NONE,
            days_past_due=None,
            reason=MISSING_DAYS_REASON,
            source_id=_source_id(conduct),
        )
    if isinstance(days, bool) or not isinstance(days, int):
        raise TypeError("conduct.days_past_due must be an integer.")
    return FacilitySmaDerivation(
        facility_id=validated_facility_id,
        as_of_date=as_of_date,
        band=sma_band(days),
        days_past_due=days,
        source_id=_source_id(conduct),
    )


def derive_borrower_sma(
    conduct: Mapping[object, object] | Sequence[object] | None,
    *,
    as_of_date: date,
    borrower_id: ConductIdentifier | None = None,
    facility_ids: Iterable[ConductIdentifier] | None = None,
) -> BorrowerSmaDerivation:
    """Derive facility bands and roll them up to the borrower's worst band.

    ``conduct`` accepts either a mapping of facility id to conduct row or a
    sequence of rows carrying ``facility_id``.  ``facility_ids`` may be
    supplied by the scoped service when the day has no rows; those facilities
    are retained as explicit missing-conduct derivations.
    """

    _validate_date(as_of_date)
    validated_borrower_id = (
        _validated_identifier(borrower_id, "borrower_id") if borrower_id is not None else None
    )
    expected = _unique_identifiers(facility_ids or ())
    records = _normalise_conduct(conduct, as_of_date=as_of_date)

    ordered_ids = list(expected)
    for facility_id, _rows in records.items():
        if facility_id not in ordered_ids:
            ordered_ids.append(facility_id)

    derivations: list[FacilitySmaDerivation] = []
    for facility_id in ordered_ids:
        rows = records.get(facility_id, ())
        selected = _select_conduct(rows, as_of_date)
        derivations.append(
            derive_facility_sma(selected, facility_id=facility_id, as_of_date=as_of_date)
        )

    if not derivations:
        return BorrowerSmaDerivation(
            borrower_id=validated_borrower_id,
            as_of_date=as_of_date,
            band=SmaBand.NONE,
            facilities=(),
            reason=MISSING_CONDUCT_REASON,
        )

    worst = max((item.band for item in derivations), key=lambda item: item.severity)
    reason = _aggregate_missing_reason(derivations)
    return BorrowerSmaDerivation(
        borrower_id=validated_borrower_id,
        as_of_date=as_of_date,
        band=worst,
        facilities=tuple(derivations),
        reason=reason,
    )


def _normalise_conduct(
    conduct: Mapping[object, object] | Sequence[object] | None,
    *,
    as_of_date: date,
) -> dict[ConductIdentifier, tuple[object, ...]]:
    if conduct is None:
        return {}
    if isinstance(conduct, Mapping):
        if _looks_like_record(conduct):
            facility_id = _validated_identifier(
                _field(conduct, "facility_id"), "conduct.facility_id"
            )
            return {facility_id: (conduct,)}
        result: dict[ConductIdentifier, tuple[object, ...]] = {}
        for raw_id, value in conduct.items():
            facility_id = _validated_identifier(raw_id, "facility_id")
            rows = _rows_for_mapping_value(value, as_of_date=as_of_date)
            _validate_rows(rows, facility_id)
            result[facility_id] = rows
        return result
    if isinstance(conduct, str | bytes | bytearray) or not isinstance(conduct, Sequence):
        raise TypeError("conduct must be a mapping, sequence or None.")
    result = {}
    for row in conduct:
        facility_id = _validated_identifier(_field(row, "facility_id"), "conduct.facility_id")
        _validate_rows((row,), facility_id)
        existing = result.get(facility_id, ())
        if any(
            _same_conduct_date(item, row) and _same_requested_date(item, as_of_date)
            for item in existing
        ):
            raise ValueError(
                f"More than one conduct row exists for facility {facility_id!r} on {as_of_date}."
            )
        result[facility_id] = (*existing, row)
    return result


def _rows_for_mapping_value(value: object, *, as_of_date: date) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping) and not _looks_like_record(value):
        if as_of_date in value:
            return _as_rows(value[as_of_date])
        iso_date = as_of_date.isoformat()
        if iso_date in value:
            return _as_rows(value[iso_date])
        if value and all(_is_date_key(key) for key in value):
            return ()
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(value)
    return (value,)


def _as_rows(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(value)
    return (value,)


def _select_conduct(rows: tuple[object, ...], as_of_date: date) -> object | None:
    if not rows:
        return None
    matching = [row for row in rows if _same_requested_date(row, as_of_date)]
    undated = [row for row in rows if _field(row, "as_of_date") is None]
    if len(matching) + len(undated) > 1:
        raise ValueError(f"More than one conduct row exists on {as_of_date}.")
    if matching:
        return matching[0]
    if undated:
        return undated[0]
    return None


def _validate_rows(rows: tuple[object, ...], facility_id: ConductIdentifier) -> None:
    for row in rows:
        row_date = _field(row, "as_of_date")
        if row_date is not None:
            _validate_date(row_date, "conduct.as_of_date")
        row_id = _field(row, "facility_id")
        if row_id is not None:
            validated_row_id = _validated_identifier(row_id, "conduct.facility_id")
            if validated_row_id != facility_id:
                raise ValueError(
                    f"Conduct facility_id {validated_row_id!r} does not match {facility_id!r}."
                )


def _same_conduct_date(left: object, right: object) -> bool:
    left_date = _field(left, "as_of_date")
    right_date = _field(right, "as_of_date")
    return left_date is not None and left_date == right_date


def _same_requested_date(row: object, as_of_date: date) -> bool:
    row_date = _field(row, "as_of_date")
    return row_date is not None and row_date == as_of_date


def _looks_like_record(value: Mapping[object, object]) -> bool:
    return any(
        key in value for key in ("facility_id", "days_past_due", "as_of_date", "overdue_amount")
    )


def _field(owner: object, name: str) -> object | None:
    if isinstance(owner, Mapping):
        return owner.get(name)
    return getattr(owner, name, None)


def _source_id(owner: object) -> ConductIdentifier | None:
    source_id = _field(owner, "source_id")
    if source_id is None:
        return None
    return _validated_identifier(source_id, "conduct.source_id")


def _validated_identifier(value: object, name: str) -> ConductIdentifier:
    if not isinstance(value, str | UUID) or (isinstance(value, str) and not value.strip()):
        raise TypeError(f"{name} must be a non-empty string or UUID.")
    return value.strip() if isinstance(value, str) else value


def _unique_identifiers(values: Iterable[ConductIdentifier]) -> list[ConductIdentifier]:
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("facility_ids must be an iterable of facility identifiers.")
    result: list[ConductIdentifier] = []
    for value in values:
        validated = _validated_identifier(value, "facility_id")
        if validated in result:
            raise ValueError(f"facility_id {validated!r} was supplied more than once.")
        result.append(validated)
    return result


def _aggregate_missing_reason(
    derivations: Sequence[FacilitySmaDerivation],
) -> str | None:
    """Summarize an all-missing facility set without masking mixed data."""

    reasons = [item.reason for item in derivations]
    if not reasons or any(reason is None for reason in reasons):
        return None
    first_reason = reasons[0]
    if first_reason is not None and all(reason == first_reason for reason in reasons):
        return first_reason
    return "facility conduct is incomplete for the requested date"


def _is_date_key(value: object) -> bool:
    if isinstance(value, date):
        return not isinstance(value, datetime)
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_date(value: object, name: str = "as_of_date") -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{name} must be a calendar date.")


__all__ = [
    "BorrowerSmaDerivation",
    "FacilityConductFacts",
    "FacilitySmaDerivation",
    "MISSING_CONDUCT_REASON",
    "MISSING_DAYS_REASON",
    "SmaBand",
    "derive_borrower_sma",
    "derive_facility_sma",
    "sma_band",
]
