"""CRILC-shaped export construction: pure, persistence-neutral domain logic.

`spec §2.1`'s Prudential Framework requires a monthly CRILC extract and a
weekly default report for aggregate borrower exposure at or above ₹5 crore
(`spec §R-31`). This module contains no database or framework import: the
service boundary (`services/reporting.py`) supplies persistence-neutral
facts, and this module deterministically derives the report those facts
produce. That split is what makes `spec §R-31.c`'s reproducibility check —
"a report regenerated for a past date reproduces the original byte-for-byte
for all non-timestamp content" — checkable at all: the report content is a
pure function of its inputs, with no wall-clock value anywhere in it.

The published layout is versioned *data* (`reporting/layouts/crilc/*.json`),
not a Python literal, so a layout change is a new file rather than an
indistinguishable code edit — `layouts/crilc/__init__.py` loads it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from covenant_radar.domain.covenants.sma import (
    BorrowerSmaDerivation,
    ConductIdentifier,
    FacilitySmaDerivation,
    SmaBand,
    derive_borrower_sma,
)

#: `spec §2.1`'s Prudential Framework: CRILC covers aggregate exposure of
#: ₹5 crore (1 crore = 10,000,000) or more. This is RBI's own fixed
#: reporting boundary, not a tunable business policy, so — exactly like
#: `domain/covenants/sma.py`'s 30/60/90-day cutoffs — it is a named
#: constant here rather than a `config.thresholds` entry subject to
#: maker-checker approval.
CRILC_AGGREGATE_EXPOSURE_THRESHOLD: Final[Decimal] = Decimal("50000000")

#: The Prudential Framework's SMA-2 (61-90 days overdue) and beyond is
#: "default" for the weekly default report.
_DEFAULT_BAND_FLOOR_SEVERITY: Final[int] = SmaBand.SMA_2.severity

_FIELD_TYPES: Final[frozenset[str]] = frozenset({"string", "decimal", "integer", "date"})
_FIELD_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]*")
_CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z]{3}")


class CrilcReportType(str, Enum):
    """The two CRILC-shaped exports `spec §R-31` names."""

    MONTHLY = "crilc_monthly"
    WEEKLY_DEFAULT = "crilc_weekly_default"


@dataclass(frozen=True, slots=True)
class CrilcLayoutField:
    """One published column: its name, shape and whether it may be absent."""

    name: str
    label: str
    data_type: str
    required: bool = True
    max_length: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _FIELD_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(f"CrilcLayoutField.name must be snake_case, got {self.name!r}.")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("CrilcLayoutField.label must be non-empty text.")
        if self.data_type not in _FIELD_TYPES:
            raise ValueError(
                f"CrilcLayoutField.data_type must be one of {sorted(_FIELD_TYPES)}, "
                f"got {self.data_type!r}."
            )
        if not isinstance(self.required, bool):
            raise TypeError("CrilcLayoutField.required must be a bool.")
        if self.max_length is not None:
            if self.data_type != "string":
                raise ValueError("CrilcLayoutField.max_length only applies to string fields.")
            if (
                isinstance(self.max_length, bool)
                or not isinstance(self.max_length, int)
                or self.max_length <= 0
            ):
                raise ValueError("CrilcLayoutField.max_length must be a positive integer.")

    def validate_value(self, value: object) -> bool:
        """Whether `value` matches this field's declared shape.

        `None` is valid exactly when the field is optional. A required
        field with no value is the caller's exceptions-section case
        (`build_crilc_report`'s job), not a shape failure this method
        reports — that split is what lets a layout be validated without
        knowing which borrower produced which row.
        """
        if value is None:
            return not self.required
        if self.data_type == "string":
            return isinstance(value, str) and (
                self.max_length is None or len(value) <= self.max_length
            )
        if self.data_type == "decimal":
            return isinstance(value, Decimal) and value.is_finite()
        if self.data_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if self.data_type == "date":
            return isinstance(value, date) and not isinstance(value, datetime)
        # Unreachable: __post_init__ already restricts data_type to _FIELD_TYPES.
        raise AssertionError(f"Unhandled CRILC field data_type {self.data_type!r}.")


@dataclass(frozen=True, slots=True)
class CrilcLayout:
    """One versioned, published field set for one report type."""

    report_type: CrilcReportType
    version: int
    effective_from: date
    fields: tuple[CrilcLayoutField, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.report_type, CrilcReportType):
            raise TypeError("CrilcLayout.report_type must be a CrilcReportType.")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("CrilcLayout.version must be a positive integer.")
        if isinstance(self.effective_from, datetime) or not isinstance(self.effective_from, date):
            raise TypeError("CrilcLayout.effective_from must be a calendar date.")
        if not isinstance(self.fields, tuple) or not self.fields:
            raise ValueError("CrilcLayout.fields must be a non-empty tuple.")
        if not all(isinstance(item, CrilcLayoutField) for item in self.fields):
            raise TypeError("CrilcLayout.fields must contain CrilcLayoutField values.")
        names = [field_def.name for field_def in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("CrilcLayout.fields must have unique names.")

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field_def.name for field_def in self.fields)

    def field(self, name: str) -> CrilcLayoutField:
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(f"CRILC layout has no field named {name!r}.")

    def validate_row(self, row: Mapping[str, object]) -> tuple[str, ...]:
        """Return the sorted field names where `row` fails this layout's shape.

        An empty result means `row` carries exactly this layout's fields,
        each holding a value of its declared type (or `None`, for an
        optional field) — `spec §R-31.a`'s "validates against the
        published layout" made checkable on any candidate row.
        """
        if not isinstance(row, Mapping):
            raise TypeError("validate_row requires a mapping.")
        expected = set(self.field_names)
        actual = set(row)
        failures = actual - expected
        failures |= expected - actual
        for field_def in self.fields:
            if field_def.name in row and not field_def.validate_value(row[field_def.name]):
                failures.add(field_def.name)
        return tuple(sorted(failures))


@dataclass(frozen=True, slots=True)
class CrilcFacilityFacts:
    """A persistence-neutral, effective-dated facility exposure fact."""

    facility_id: ConductIdentifier
    reference: str
    sanctioned_limit: Decimal
    currency: str
    outstanding: Decimal | None
    effective_from: date
    effective_to: date | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.facility_id, "facility_id")
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("CrilcFacilityFacts.reference must be non-empty text.")
        if not isinstance(self.sanctioned_limit, Decimal) or not self.sanctioned_limit.is_finite():
            raise TypeError("CrilcFacilityFacts.sanctioned_limit must be a finite Decimal.")
        if self.sanctioned_limit < 0:
            raise ValueError("CrilcFacilityFacts.sanctioned_limit must not be negative.")
        if not isinstance(self.currency, str) or not _CURRENCY_PATTERN.fullmatch(self.currency):
            raise ValueError("CrilcFacilityFacts.currency must be a 3-letter uppercase ISO code.")
        if self.outstanding is not None:
            if not isinstance(self.outstanding, Decimal) or not self.outstanding.is_finite():
                raise TypeError("CrilcFacilityFacts.outstanding must be a finite Decimal or None.")
            if self.outstanding < 0:
                raise ValueError("CrilcFacilityFacts.outstanding must not be negative.")
        if isinstance(self.effective_from, datetime) or not isinstance(self.effective_from, date):
            raise TypeError("CrilcFacilityFacts.effective_from must be a calendar date.")
        if self.effective_to is not None:
            if isinstance(self.effective_to, datetime) or not isinstance(self.effective_to, date):
                raise TypeError("CrilcFacilityFacts.effective_to must be a calendar date or None.")
            if self.effective_to <= self.effective_from:
                raise ValueError("CrilcFacilityFacts.effective_to must be after effective_from.")

    def is_effective_on(self, as_of_date: date) -> bool:
        return self.effective_from <= as_of_date and (
            self.effective_to is None or as_of_date < self.effective_to
        )


@dataclass(frozen=True, slots=True)
class CrilcBorrowerFacts:
    """A persistence-neutral borrower and its effective-dated facilities."""

    borrower_id: ConductIdentifier
    reference: str
    legal_name: str
    industry_code: str | None
    constitution: str | None
    facilities: tuple[CrilcFacilityFacts, ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.borrower_id, "borrower_id")
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("CrilcBorrowerFacts.reference must be non-empty text.")
        if not isinstance(self.legal_name, str) or not self.legal_name.strip():
            raise ValueError("CrilcBorrowerFacts.legal_name must be non-empty text.")
        if self.industry_code is not None and (
            not isinstance(self.industry_code, str) or not self.industry_code.strip()
        ):
            raise ValueError("CrilcBorrowerFacts.industry_code must be non-empty text or None.")
        if self.constitution is not None and (
            not isinstance(self.constitution, str) or not self.constitution.strip()
        ):
            raise ValueError("CrilcBorrowerFacts.constitution must be non-empty text or None.")
        if not isinstance(self.facilities, tuple) or not all(
            isinstance(item, CrilcFacilityFacts) for item in self.facilities
        ):
            raise TypeError("CrilcBorrowerFacts.facilities must be a tuple of CrilcFacilityFacts.")

    def effective_facilities(self, as_of_date: date) -> tuple[CrilcFacilityFacts, ...]:
        return tuple(item for item in self.facilities if item.is_effective_on(as_of_date))


@dataclass(frozen=True, slots=True)
class CrilcConductFacts:
    """One facility's account-conduct snapshot, shaped like `FacilityConduct`.

    Kept independent of `domain.covenants.sma.FacilityConductFacts` because
    this report also needs `overdue_amount`, which that dataclass does not
    carry; it is still duck-type compatible with `derive_borrower_sma`,
    which reads fields by name rather than by concrete type.
    """

    facility_id: ConductIdentifier
    as_of_date: date
    days_past_due: int | None
    overdue_amount: Decimal | None = None
    source_id: ConductIdentifier | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.facility_id, "facility_id")
        if isinstance(self.as_of_date, datetime) or not isinstance(self.as_of_date, date):
            raise TypeError("CrilcConductFacts.as_of_date must be a calendar date.")
        if self.days_past_due is not None:
            if isinstance(self.days_past_due, bool) or not isinstance(self.days_past_due, int):
                raise TypeError("CrilcConductFacts.days_past_due must be an integer or None.")
            if self.days_past_due < 0:
                raise ValueError("CrilcConductFacts.days_past_due must not be negative.")
        if self.overdue_amount is not None:
            if (
                not isinstance(self.overdue_amount, Decimal)
                or not self.overdue_amount.is_finite()
            ):
                raise TypeError(
                    "CrilcConductFacts.overdue_amount must be a finite Decimal or None."
                )
            if self.overdue_amount < 0:
                raise ValueError("CrilcConductFacts.overdue_amount must not be negative.")
        if self.source_id is not None:
            _validate_identifier(self.source_id, "source_id")


@dataclass(frozen=True, slots=True)
class CrilcException:
    """One borrower held out of the report because required data is absent."""

    borrower_reference: str
    missing_fields: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.borrower_reference, str) or not self.borrower_reference.strip():
            raise ValueError("CrilcException.borrower_reference must be non-empty text.")
        if not isinstance(self.missing_fields, tuple) or not self.missing_fields:
            raise ValueError("CrilcException.missing_fields must be a non-empty tuple.")
        if not all(isinstance(item, str) and item for item in self.missing_fields):
            raise TypeError("CrilcException.missing_fields must contain non-empty strings.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("CrilcException.reason must be non-empty text.")


@dataclass(frozen=True, slots=True)
class CrilcReconciliation:
    """Counts that must add up: every considered borrower falls into
    exactly one bucket, so the reconciliation can never silently drop one."""

    total_considered: int
    included: int
    excluded_below_threshold: int
    excluded_not_in_default: int
    exceptions: int

    def __post_init__(self) -> None:
        for name in (
            "total_considered",
            "included",
            "excluded_below_threshold",
            "excluded_not_in_default",
            "exceptions",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"CrilcReconciliation.{name} must be a non-negative integer.")
        computed = (
            self.included
            + self.excluded_below_threshold
            + self.excluded_not_in_default
            + self.exceptions
        )
        if computed != self.total_considered:
            raise ValueError(
                "CrilcReconciliation does not add up: "
                f"{self.included} included + {self.excluded_below_threshold} excluded-below-"
                f"threshold + {self.excluded_not_in_default} excluded-not-in-default + "
                f"{self.exceptions} exceptions = {computed}, expected {self.total_considered}."
            )

    def as_dict(self) -> dict[str, int]:
        return {
            "total_considered": self.total_considered,
            "included": self.included,
            "excluded_below_threshold": self.excluded_below_threshold,
            "excluded_not_in_default": self.excluded_not_in_default,
            "exceptions": self.exceptions,
        }


@dataclass(frozen=True, slots=True)
class CrilcReport:
    """The complete, deterministic result of one CRILC-shaped generation."""

    report_type: CrilcReportType
    as_of_date: date
    layout: CrilcLayout
    rows: tuple[Mapping[str, object], ...]
    exceptions: tuple[CrilcException, ...]
    reconciliation: CrilcReconciliation

    def __post_init__(self) -> None:
        if not isinstance(self.report_type, CrilcReportType):
            raise TypeError("CrilcReport.report_type must be a CrilcReportType.")
        if isinstance(self.as_of_date, datetime) or not isinstance(self.as_of_date, date):
            raise TypeError("CrilcReport.as_of_date must be a calendar date.")
        if not isinstance(self.layout, CrilcLayout):
            raise TypeError("CrilcReport.layout must be a CrilcLayout.")
        if self.layout.report_type is not self.report_type:
            raise ValueError("CrilcReport.layout does not match report_type.")
        object.__setattr__(
            self, "rows", tuple(MappingProxyType(dict(row)) for row in self.rows)
        )
        for row in self.rows:
            failures = self.layout.validate_row(row)
            if failures:
                raise ValueError(
                    f"CrilcReport row for {row.get('borrower_reference')!r} fails its layout "
                    f"shape: {', '.join(failures)}."
                )
        if not isinstance(self.exceptions, tuple) or not all(
            isinstance(item, CrilcException) for item in self.exceptions
        ):
            raise TypeError("CrilcReport.exceptions must be a tuple of CrilcException values.")
        if not isinstance(self.reconciliation, CrilcReconciliation):
            raise TypeError("CrilcReport.reconciliation must be a CrilcReconciliation.")
        if self.reconciliation.included != len(self.rows):
            raise ValueError("CrilcReport.reconciliation.included does not match the row count.")
        if self.reconciliation.exceptions != len(self.exceptions):
            raise ValueError(
                "CrilcReport.reconciliation.exceptions does not match the exceptions count."
            )

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def as_dict(self) -> dict[str, object]:
        return {
            "report_type": self.report_type.value,
            "as_of_date": self.as_of_date.isoformat(),
            "layout_version": self.layout.version,
            "fields": list(self.layout.field_names),
            "rows": [
                {name: _json_safe_value(row[name]) for name in self.layout.field_names}
                for row in self.rows
            ],
            "exceptions": [
                {
                    "borrower_reference": item.borrower_reference,
                    "missing_fields": list(item.missing_fields),
                    "reason": item.reason,
                }
                for item in self.exceptions
            ],
            "reconciliation": self.reconciliation.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        """Deterministic content bytes, with no wall-clock artefact, for hashing.

        `spec §R-31.c` requires byte-for-byte reproduction "for all
        non-timestamp content"; there is no timestamp anywhere in this
        payload, so an identical `(borrowers, conduct, as_of_date, layout)`
        input always reproduces identical bytes.
        """
        return _canonical_json_bytes(self.as_dict())

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def to_csv_bytes(self) -> bytes:
        """Render the included rows as deterministic, layout-ordered CSV."""
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream, fieldnames=list(self.layout.field_names), lineterminator="\n"
        )
        writer.writeheader()
        for row in self.rows:
            writer.writerow({name: _csv_value(row[name]) for name in self.layout.field_names})
        return stream.getvalue().encode("utf-8")


def build_crilc_report(
    borrowers: Sequence[CrilcBorrowerFacts],
    *,
    report_type: CrilcReportType,
    as_of_date: date,
    layout: CrilcLayout,
    conduct_by_facility: Mapping[object, object] | None = None,
    threshold: Decimal = CRILC_AGGREGATE_EXPOSURE_THRESHOLD,
) -> CrilcReport:
    """Deterministically derive one CRILC-shaped report from stored facts.

    Every borrower is classified into exactly one of four buckets —
    included, excluded below threshold, excluded as not-in-default (weekly
    report only), or an exception for missing required data — so
    `CrilcReconciliation` always adds up back to the input count
    (`spec §R-31`'s "every case": a below-threshold facility excluded and
    counted; a borrower with missing required data listed in exceptions
    rather than omitted or defaulted).
    """
    if not isinstance(report_type, CrilcReportType):
        raise TypeError("build_crilc_report requires a CrilcReportType.")
    if isinstance(as_of_date, datetime) or not isinstance(as_of_date, date):
        raise TypeError("build_crilc_report requires a calendar date.")
    if not isinstance(layout, CrilcLayout):
        raise TypeError("build_crilc_report requires a CrilcLayout.")
    if layout.report_type is not report_type:
        raise ValueError("The supplied layout does not match report_type.")
    if not isinstance(threshold, Decimal) or not threshold.is_finite() or threshold <= 0:
        raise ValueError("build_crilc_report threshold must be a positive, finite Decimal.")
    if isinstance(borrowers, str | bytes | bytearray) or not isinstance(borrowers, Sequence):
        raise TypeError("build_crilc_report requires a sequence of CrilcBorrowerFacts.")
    if not all(isinstance(item, CrilcBorrowerFacts) for item in borrowers):
        raise TypeError("build_crilc_report borrowers must all be CrilcBorrowerFacts.")

    ordered = sorted(borrowers, key=lambda item: item.reference)
    seen_references: set[str] = set()
    for borrower in ordered:
        if borrower.reference in seen_references:
            raise ValueError(
                f"Duplicate borrower reference in CRILC input: {borrower.reference!r}."
            )
        seen_references.add(borrower.reference)

    rows: list[dict[str, object]] = []
    exceptions: list[CrilcException] = []
    excluded_below_threshold = 0
    excluded_not_in_default = 0

    for borrower in ordered:
        effective_facilities = borrower.effective_facilities(as_of_date)
        aggregate_exposure = sum(
            (item.sanctioned_limit for item in effective_facilities), Decimal("0")
        )
        if aggregate_exposure < threshold:
            excluded_below_threshold += 1
            continue

        facility_ids = tuple(item.facility_id for item in effective_facilities)
        derivation = derive_borrower_sma(
            conduct_by_facility or {},
            as_of_date=as_of_date,
            borrower_id=borrower.borrower_id,
            facility_ids=facility_ids,
        )
        band_known = derivation.reason is None
        in_default = band_known and derivation.band.severity >= _DEFAULT_BAND_FLOOR_SEVERITY

        if report_type is CrilcReportType.WEEKLY_DEFAULT and band_known and not in_default:
            excluded_not_in_default += 1
            continue

        candidate = _candidate_row(
            borrower=borrower,
            effective_facilities=effective_facilities,
            aggregate_exposure=aggregate_exposure,
            derivation=derivation,
            band_known=band_known,
            conduct_by_facility=conduct_by_facility,
            as_of_date=as_of_date,
        )
        row = {name: candidate[name] for name in layout.field_names}
        missing = tuple(
            sorted(
                field_def.name
                for field_def in layout.fields
                if field_def.required and row[field_def.name] is None
            )
        )
        if missing:
            reason = derivation.reason or "Required field(s) are missing for the published layout."
            exceptions.append(
                CrilcException(
                    borrower_reference=borrower.reference, missing_fields=missing, reason=reason
                )
            )
            continue
        rows.append(row)

    reconciliation = CrilcReconciliation(
        total_considered=len(ordered),
        included=len(rows),
        excluded_below_threshold=excluded_below_threshold,
        excluded_not_in_default=excluded_not_in_default,
        exceptions=len(exceptions),
    )
    return CrilcReport(
        report_type=report_type,
        as_of_date=as_of_date,
        layout=layout,
        rows=tuple(rows),
        exceptions=tuple(exceptions),
        reconciliation=reconciliation,
    )


def _candidate_row(
    *,
    borrower: CrilcBorrowerFacts,
    effective_facilities: tuple[CrilcFacilityFacts, ...],
    aggregate_exposure: Decimal,
    derivation: BorrowerSmaDerivation,
    band_known: bool,
    conduct_by_facility: Mapping[object, object] | None,
    as_of_date: date,
) -> dict[str, object]:
    currencies = {item.currency for item in effective_facilities}
    currency_value = next(iter(currencies)) if len(currencies) == 1 else None
    outstanding_values = [item.outstanding for item in effective_facilities]
    outstanding_amount: Decimal | None
    if outstanding_values and all(value is not None for value in outstanding_values):
        outstanding_amount = sum(
            (value for value in outstanding_values if value is not None), Decimal("0")
        )
    else:
        outstanding_amount = None
    worst = derivation.worst_facility if band_known else None
    days_past_due = (worst.days_past_due if worst is not None else 0) if band_known else None
    overdue_amount = _overdue_amount(worst, conduct_by_facility) if band_known else None
    sma_band_value = derivation.band.value if band_known else None
    return {
        "as_of_date": as_of_date,
        "borrower_reference": borrower.reference,
        "legal_name": borrower.legal_name,
        "industry_code": borrower.industry_code,
        "constitution": borrower.constitution,
        "aggregate_exposure_amount": aggregate_exposure,
        "currency": currency_value,
        "facility_count": len(effective_facilities),
        "outstanding_amount": outstanding_amount,
        "sma_band": sma_band_value,
        "days_past_due": days_past_due,
        "overdue_amount": overdue_amount,
    }


def _overdue_amount(
    worst: FacilitySmaDerivation | None,
    conduct_by_facility: Mapping[object, object] | None,
) -> Decimal | None:
    if worst is None:
        return Decimal("0")
    if not conduct_by_facility:
        return None
    raw = conduct_by_facility.get(worst.facility_id)
    if raw is None:
        return None
    value = (
        raw.get("overdue_amount")
        if isinstance(raw, Mapping)
        else getattr(raw, "overdue_amount", None)
    )
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError("conduct.overdue_amount must be a Decimal.")
    return value


def _json_safe_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    raise TypeError(f"CRILC report values must be JSON-safe scalars, got {type(value).__name__}.")


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _validate_identifier(value: object, name: str) -> None:
    if not isinstance(value, str | UUID) or (isinstance(value, str) and not value.strip()):
        raise TypeError(f"{name} must be a non-empty string or UUID.")


__all__ = [
    "CRILC_AGGREGATE_EXPOSURE_THRESHOLD",
    "CrilcBorrowerFacts",
    "CrilcConductFacts",
    "CrilcException",
    "CrilcFacilityFacts",
    "CrilcLayout",
    "CrilcLayoutField",
    "CrilcReconciliation",
    "CrilcReport",
    "CrilcReportType",
    "build_crilc_report",
]
