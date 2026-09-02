"""Presentation shaping for the facility master-data screens.

Three screens share this module: the facility list, one facility's detail,
and the book-level insights view.  All three answer the same two questions a
credit officer actually asks of master data — *what do we hold?* and *how has
it moved?* — so the formatting rules (money is ``₹ crore``, utilisation is a
banded percentage, a financial year runs April to March) live here once
rather than three times in Jinja.

Arithmetic stays on :class:`~decimal.Decimal` throughout.  Facility amounts
are persisted as fixed-point ``numeric(18,4)`` and are stored as text on
SQLite (`db/types.py`); converting them to ``float`` for a chart coordinate
would put a rounded number in front of a reader who is looking at a ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from markupsafe import Markup

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.repositories.facility import FacilityBookRow, FacilityListing
from covenant_radar.i18n.formatting import format_indian_number, format_ist_date

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_MONEY_QUANTUM = Decimal("0.01")
_RATE_QUANTUM = Decimal("0.1")
#: Utilisation of a sanctioned limit, banded the way the queue bands headroom.
#: Below 75% is ordinary; 75–90% is worth watching; at or above 90% the
#: borrower has effectively exhausted the limit.
_WATCH_UTILISATION = Decimal("75")
_BREACH_UTILISATION = Decimal("90")
#: A maturity inside this window is the one a monitoring officer must act on.
_NEAR_MATURITY_DAYS = 90
_YEAR_DAYS = 365
#: Widest bar in a breakdown chart, in the chart's own ``viewBox`` units.
_BAR_SPAN = Decimal("100")
_MINIMUM_VISIBLE_BAR = Decimal("0.6")
_UTILISATION_BUCKETS: tuple[tuple[str, str, Decimal, Decimal], ...] = (
    ("under_50", "Up to 50%", _ZERO, Decimal("50")),
    ("50_75", "50% to 75%", Decimal("50"), _WATCH_UTILISATION),
    ("75_90", "75% to 90%", _WATCH_UTILISATION, _BREACH_UTILISATION),
    ("over_90", "Over 90%", _BREACH_UTILISATION, Decimal("1000")),
)


@dataclass(frozen=True, slots=True)
class BookMetric:
    """One headline figure, already formatted, with its supporting detail."""

    key: str
    label: str
    value: str
    unit: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BreakdownRow:
    """One bucket of the book, as both a ledger line and a bar."""

    key: str
    label: str
    count: int
    sanctioned: str
    outstanding: str
    utilisation: str
    band: str
    share: str
    bar: str


@dataclass(frozen=True, slots=True)
class Breakdown:
    """One captioned section of the insights screen."""

    key: str
    title: str
    description: str
    measure_label: str
    rows: tuple[BreakdownRow, ...]


@dataclass(frozen=True, slots=True)
class FacilityBookView:
    """Everything the insights screen renders, ready for Jinja."""

    metrics: tuple[BookMetric, ...]
    breakdowns: tuple[Breakdown, ...]
    facility_count: int
    currency_note: str


# ---- list screen --------------------------------------------------------


def facility_table_rows(
    listings: Sequence[FacilityListing], *, today: date
) -> list[dict[str, object]]:
    """Return ledger rows for the facility list.

    The reference and the borrower are links because they are the two things
    a reader goes on to open; rendering them as markup here is what let the
    screen drop the column of fifty stacked "Open" buttons that used to sit
    underneath the table.
    """
    return [
        {
            "id": listing.facility.reference,
            "reference": _facility_link(listing.facility.reference),
            "borrower": _borrower_cell(listing),
            "facility_type": _humanise(listing.facility.facility_type),
            "limit": _amount(listing.facility.sanctioned_limit),
            "outstanding": _amount(listing.facility.outstanding),
            "utilisation": _utilisation_cell(
                listing.facility.outstanding, listing.facility.sanctioned_limit
            ),
            "maturity": _maturity_cell(listing.facility.maturity_date, today=today),
            "status": _status_cell(listing.facility),
        }
        for listing in listings
    ]


def facility_filter_options(
    values: Sequence[str], *, all_label: str, humanise: bool = True
) -> list[dict[str, str | bool]]:
    """Return select options for one facility filter, "all" first."""
    options: list[dict[str, str | bool]] = [{"value": "", "label": all_label, "disabled": False}]
    options.extend(
        {
            "value": value,
            "label": _humanise(value) if humanise else value,
            "disabled": False,
        }
        for value in values
    )
    return options


# ---- detail screen ------------------------------------------------------


def facility_detail_fields(
    facility: Facility, borrower: Borrower | None, *, today: date
) -> list[dict[str, object]]:
    """Return the full record as label/value pairs, nothing withheld.

    The screen used to show four fields out of fourteen, so the pricing, the
    security, the drawn position and the maturity a reader came for were only
    visible through the API.
    """
    fields: list[dict[str, object]] = [
        _field("borrower", "Borrower", _borrower_value(borrower, facility)),
        _field("facility_type", "Facility type", _humanise(facility.facility_type)),
        _field("currency", "Currency", facility.currency),
        _field("sanctioned_limit", "Sanctioned limit", _amount(facility.sanctioned_limit)),
        _field("drawing_power", "Drawing power", _amount(facility.drawing_power)),
        _field("outstanding", "Outstanding", _amount(facility.outstanding)),
        _field(
            "utilisation",
            "Utilisation",
            _utilisation_text(facility.outstanding, facility.sanctioned_limit),
            band=_utilisation_band(_utilisation(facility.outstanding, facility.sanctioned_limit)),
        ),
        _field("undrawn", "Undrawn", _amount(_undrawn(facility))),
        _field("pricing_bps", "Pricing", _pricing(facility.pricing_bps)),
        _field("security_type", "Security", _humanise(facility.security_type or "")),
        _field("sanction_date", "Sanction date", _date(facility.sanction_date)),
        _field(
            "maturity_date",
            "Maturity date",
            _maturity_text(facility.maturity_date, today=today),
            band=_maturity_band(facility.maturity_date, today=today),
        ),
        _field("effective_from", "Effective from", _date(facility.effective_from)),
        _field("effective_to", "Effective to", _date(facility.effective_to)),
        _field("status", "Status", _status_text(facility), band=_status_band(facility)),
        _field("version", "Version", str(facility.version)),
    ]
    return fields


def facility_revision_rows(
    revisions: Sequence[Facility], *, current_reference: str
) -> list[dict[str, object]]:
    """Return the effective-dated chain as a limit-history ledger.

    The delta column is what makes this a history rather than a list: it
    names the size of each sanctioned-limit revision against the version it
    replaced, which is the number a reviewer is looking for.
    """
    rows: list[dict[str, object]] = []
    previous: Decimal | None = None
    for revision in revisions:
        rows.append(
            {
                "id": f"revision-{revision.reference}",
                "reference": _facility_link(revision.reference)
                if revision.reference != current_reference
                else Markup("<strong>{}</strong>").format(revision.reference),
                "effective_from": _date(revision.effective_from),
                "effective_to": _date(revision.effective_to),
                "limit": _amount(revision.sanctioned_limit),
                "change": _delta(previous, revision.sanctioned_limit),
                "status": _status_text(revision),
            }
        )
        previous = revision.sanctioned_limit
    return rows


# ---- insights screen ----------------------------------------------------


def facility_book_view(rows: Sequence[FacilityBookRow], *, today: date) -> FacilityBookView:
    """Summarise the in-scope book into headline figures and breakdowns."""
    sanctioned = sum((row.sanctioned_limit for row in rows), _ZERO)
    outstanding = sum((row.outstanding or _ZERO for row in rows), _ZERO)
    borrowers = {row.borrower_id for row in rows}
    currencies = sorted({row.currency for row in rows})
    maturing = tuple(
        row
        for row in rows
        if row.maturity_date is not None
        and _ZERO <= Decimal((row.maturity_date - today).days) <= Decimal(_YEAR_DAYS)
    )
    metrics = (
        BookMetric(
            key="facilities",
            label="Facilities",
            value=format_indian_number(Decimal(len(rows))),
            detail=f"across {format_indian_number(Decimal(len(borrowers)))} borrowers",
        ),
        BookMetric(
            key="sanctioned",
            label="Sanctioned limit",
            value=_number(sanctioned),
            unit="₹ crore",
        ),
        BookMetric(
            key="outstanding",
            label="Outstanding",
            value=_number(outstanding),
            unit="₹ crore",
        ),
        BookMetric(
            key="utilisation",
            label="Utilisation",
            value=_percentage_text(_ratio(outstanding, sanctioned)),
            detail=f"{_number(sanctioned - outstanding)} ₹ crore undrawn",
        ),
        BookMetric(
            key="pricing",
            label="Weighted average pricing",
            value=_number(_weighted_pricing(rows)),
            unit="bps",
            detail="weighted by sanctioned limit",
        ),
        BookMetric(
            key="maturing",
            label="Maturing within a year",
            value=format_indian_number(Decimal(len(maturing))),
            detail=(
                f"{_number(sum((row.sanctioned_limit for row in maturing), _ZERO))} ₹ crore"
                " up for renewal"
            ),
        ),
    )
    breakdowns = (
        _vintage_breakdown(rows),
        _maturity_breakdown(rows),
        _type_breakdown(rows),
        _utilisation_breakdown(rows),
    )
    return FacilityBookView(
        metrics=metrics,
        breakdowns=tuple(item for item in breakdowns if item.rows),
        facility_count=len(rows),
        currency_note=", ".join(currencies),
    )


def _vintage_breakdown(rows: Sequence[FacilityBookRow]) -> Breakdown:
    buckets: dict[str, list[FacilityBookRow]] = {}
    for row in rows:
        buckets.setdefault(_financial_year(row.sanction_date), []).append(row)
    return Breakdown(
        key="vintage",
        title="Sanction vintage",
        description=(
            "What the book was sanctioned in each financial year, and how much of "
            "that vintage is still drawn."
        ),
        measure_label="Sanctioned limit",
        rows=_breakdown_rows(buckets),
    )


def _maturity_breakdown(rows: Sequence[FacilityBookRow]) -> Breakdown:
    buckets: dict[str, list[FacilityBookRow]] = {}
    for row in rows:
        label = _financial_year(row.maturity_date) if row.maturity_date else "No maturity date"
        buckets.setdefault(label, []).append(row)
    return Breakdown(
        key="maturity",
        title="Maturity profile",
        description=(
            "When the sanctioned limits fall due for renewal — the roll-off the "
            "book has to refinance, year by year."
        ),
        measure_label="Sanctioned limit maturing",
        rows=_breakdown_rows(buckets),
    )


def _type_breakdown(rows: Sequence[FacilityBookRow]) -> Breakdown:
    buckets: dict[str, list[FacilityBookRow]] = {}
    for row in rows:
        buckets.setdefault(_humanise(row.facility_type), []).append(row)
    return Breakdown(
        key="facility-type",
        title="Mix by facility type",
        description="How the sanctioned book splits by product, and how hard each is worked.",
        measure_label="Sanctioned limit",
        rows=_breakdown_rows(buckets, sort_by_size=True),
    )


def _utilisation_breakdown(rows: Sequence[FacilityBookRow]) -> Breakdown:
    buckets: dict[str, list[FacilityBookRow]] = {
        label: [] for _, label, _, _ in _UTILISATION_BUCKETS
    }
    for row in rows:
        utilisation = _utilisation(row.outstanding, row.sanctioned_limit)
        if utilisation is None:
            continue
        for _key, label, lower, upper in _UTILISATION_BUCKETS:
            if lower <= utilisation < upper:
                buckets[label].append(row)
                break
    ordered = {label: buckets[label] for _, label, _, _ in _UTILISATION_BUCKETS if buckets[label]}
    return Breakdown(
        key="utilisation",
        title="Utilisation distribution",
        description=(
            "Where the drawn position sits against the sanctioned limit. The top "
            "band is the one that turns into an excess."
        ),
        measure_label="Sanctioned limit",
        rows=_breakdown_rows(ordered, preserve_order=True),
    )


def _breakdown_rows(
    buckets: dict[str, list[FacilityBookRow]],
    *,
    sort_by_size: bool = False,
    preserve_order: bool = False,
) -> tuple[BreakdownRow, ...]:
    if not buckets:
        return ()
    totals = {
        label: sum((row.sanctioned_limit for row in items), _ZERO)
        for label, items in buckets.items()
    }
    book_total = sum(totals.values(), _ZERO)
    largest = max(totals.values(), default=_ZERO)
    if preserve_order:
        labels = list(buckets)
    elif sort_by_size:
        labels = sorted(buckets, key=lambda label: (-totals[label], label))
    else:
        labels = sorted(buckets)
    result: list[BreakdownRow] = []
    for label in labels:
        items = buckets[label]
        sanctioned = totals[label]
        outstanding = sum((row.outstanding or _ZERO for row in items), _ZERO)
        utilisation = _ratio(outstanding, sanctioned)
        result.append(
            BreakdownRow(
                key=_slug(label),
                label=label,
                count=len(items),
                sanctioned=_number(sanctioned),
                outstanding=_number(outstanding),
                utilisation=_percentage_text(utilisation),
                band=_utilisation_band(utilisation),
                share=_percentage_text(_ratio(sanctioned, book_total)),
                bar=_bar_length(sanctioned, largest),
            )
        )
    return tuple(result)


# ---- formatting primitives ---------------------------------------------


def _facility_link(reference: str) -> Markup:
    return Markup('<a class="md-link" href="/facilities/{0}">{0}</a>').format(reference)


def _borrower_cell(listing: FacilityListing) -> Markup:
    return Markup(
        '<a class="md-link" href="/borrowers/{reference}/master-data">{reference}</a>'
        '<span class="md-cell__meta">{name}</span>'
    ).format(reference=listing.borrower_reference, name=listing.borrower_legal_name)


def _borrower_value(borrower: Borrower | None, facility: Facility) -> Markup | str:
    if borrower is None:
        return str(facility.borrower_id)
    return Markup(
        '<a class="md-link" href="/borrowers/{reference}/master-data">{reference} — {name}</a>'
    ).format(reference=borrower.reference, name=borrower.legal_name)


def _utilisation_cell(outstanding: Decimal | None, sanctioned: Decimal) -> Markup | str:
    utilisation = _utilisation(outstanding, sanctioned)
    if utilisation is None:
        return "—"
    return _band_markup(_utilisation_band(utilisation), _percentage_text(utilisation))


def _maturity_cell(maturity: date | None, *, today: date) -> Markup | str:
    if maturity is None:
        return "—"
    band = _maturity_band(maturity, today=today)
    rendered = _date(maturity)
    if not band:
        return rendered
    return Markup('{0}<span class="md-cell__meta md-cell__meta--{1}">{2}</span>').format(
        rendered, band, _maturity_note(maturity, today=today)
    )


def _status_cell(facility: Facility) -> Markup:
    return _band_markup(_status_band(facility), _status_text(facility))


def _band_markup(band: str, text: str) -> Markup:
    return Markup('<span class="md-band md-band--{0}">{1}</span>').format(band or "neutral", text)


def _field(name: str, label: str, value: object, *, band: str = "") -> dict[str, object]:
    return {"name": name, "label": label, "value": value, "band": band}


def _amount(value: Decimal | None) -> str:
    return "—" if value is None else _number(value)


def _number(value: Decimal) -> str:
    return format_indian_number(value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def _date(value: date | None) -> str:
    return "—" if value is None else format_ist_date(value)


def _pricing(pricing_bps: int | None) -> str:
    if pricing_bps is None:
        return "—"
    rate = (Decimal(pricing_bps) / _HUNDRED).quantize(_MONEY_QUANTUM)
    return f"{format_indian_number(Decimal(pricing_bps))} bps ({format_indian_number(rate)}%)"


def _undrawn(facility: Facility) -> Decimal | None:
    if facility.outstanding is None:
        return None
    return facility.sanctioned_limit - facility.outstanding


def _delta(previous: Decimal | None, current: Decimal) -> str:
    if previous is None:
        return "Original sanction"
    change = current - previous
    if change == _ZERO:
        return "No change"
    sign = "+" if change > _ZERO else "−"
    share = _ratio(abs(change), previous)
    suffix = f" ({sign}{_percentage_text(share)})" if share is not None else ""
    return f"{sign}{_number(abs(change))}{suffix}"


def _utilisation(outstanding: Decimal | None, sanctioned: Decimal) -> Decimal | None:
    return None if outstanding is None else _ratio(outstanding, sanctioned)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == _ZERO:
        return None
    return (numerator / denominator * _HUNDRED).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _percentage_text(value: Decimal | None) -> str:
    return "—" if value is None else f"{format_indian_number(value)}%"


def _utilisation_text(outstanding: Decimal | None, sanctioned: Decimal) -> str:
    return _percentage_text(_utilisation(outstanding, sanctioned))


def _utilisation_band(utilisation: Decimal | None) -> str:
    if utilisation is None:
        return ""
    if utilisation >= _BREACH_UTILISATION:
        return "breach"
    if utilisation >= _WATCH_UTILISATION:
        return "watch"
    return "headroom"


def _maturity_band(maturity: date | None, *, today: date) -> str:
    if maturity is None:
        return ""
    days = (maturity - today).days
    if days < 0:
        return "breach"
    if days <= _NEAR_MATURITY_DAYS:
        return "watch"
    return ""


def _maturity_note(maturity: date, *, today: date) -> str:
    days = (maturity - today).days
    if days < 0:
        return f"matured {abs(days)} days ago"
    if days == 0:
        return "matures today"
    return f"in {days} days"


def _maturity_text(maturity: date | None, *, today: date) -> str:
    if maturity is None:
        return "—"
    return f"{_date(maturity)} ({_maturity_note(maturity, today=today)})"


def _status_text(facility: Facility) -> str:
    return "Current" if facility.effective_to is None else "Superseded"


def _status_band(facility: Facility) -> str:
    return "headroom" if facility.effective_to is None else "neutral"


def _weighted_pricing(rows: Sequence[FacilityBookRow]) -> Decimal:
    weight = sum((row.sanctioned_limit for row in rows if row.pricing_bps is not None), _ZERO)
    if weight == _ZERO:
        return _ZERO
    weighted = sum(
        (row.sanctioned_limit * Decimal(row.pricing_bps) for row in rows if row.pricing_bps),
        _ZERO,
    )
    return weighted / weight


def _bar_length(value: Decimal, largest: Decimal) -> str:
    """Return a bar width in the chart's own viewBox units.

    A non-zero bucket always keeps a visible sliver, so a small vintage reads
    as "small" rather than as "absent" beside the year that dominates.
    """
    if largest == _ZERO or value == _ZERO:
        return "0"
    length = (value / largest * _BAR_SPAN).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
    return format(max(length, _MINIMUM_VISIBLE_BAR), "f")


def _financial_year(value: date) -> str:
    """Return the Indian financial year a date falls in, e.g. ``FY20``.

    The financial year runs April to March and is labelled by the year it
    ends in, matching `i18n.formatting.format_fy_quarter`.
    """
    ending = value.year + 1 if value.month >= 4 else value.year
    return f"FY{ending % 100:02d}"


def _humanise(value: str) -> str:
    return value.replace("_", " ").strip().capitalize() if value else "—"


def _slug(label: str) -> str:
    cleaned = "".join(
        character.lower() if character.isalnum() else "-" for character in label
    ).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "bucket"


__all__ = [
    "Breakdown",
    "BreakdownRow",
    "BookMetric",
    "FacilityBookView",
    "facility_book_view",
    "facility_detail_fields",
    "facility_filter_options",
    "facility_revision_rows",
    "facility_table_rows",
]
