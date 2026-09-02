"""Read model for the borrower case file's financials tab.

The question this panel answers is not "what did the borrower file?" — the
statements screen already answers that — but "which filed figure moved the
covenant?". A relationship manager looking at a leverage breach can already
see the ratio and the threshold on the covenant tab; what they cannot see
there is whether the ratio moved because debt was raised or because net
worth was written down, and those two facts call for entirely different
conversations with the borrower.

So this module reads two things and joins them on the one column that
already relates them: `covenant_test.period_id`. The engine recorded, per
financial period, the value it computed and the verdict it reached; the
import recorded, per financial period, the lines that value was computed
from. Presenting them side by side is a join, not a second opinion.

**Nothing here recomputes a covenant.** Every covenanted value, threshold,
headroom and verdict is read from the persisted `covenant_test` row, exactly
as `view_models/borrower.py` does — this module never compares a value to a
threshold and decides for itself. The one place arithmetic happens is
`_context_ratios`, for the handful of *uncovenanted* indicators (Debt/EBITDA,
DSCR, EBITDA margin) that no agreement contains and no engine therefore
tests; those are computed through the domain ratio library's own pure
formulas (`domain/ratios/compute.py`), never by arithmetic written here, and
are labelled as indicative wherever they are shown so they can never be read
as a contractual position.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Literal
from uuid import UUID

from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.statements import FinancialPeriod, StatementLineValue
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.ratios.compute import compute_ratio
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.statements.chart import ChartError, default_chart
from covenant_radar.i18n.formatting import format_indian_number
from covenant_radar.web.svg.financials import SeriesPoint, render_series_svg

#: How many filed periods the panel shows. Eight quarters is two full years —
#: long enough for a trend to be a trend rather than a wobble, short enough
#: that every column still fits a table a reader can scan without scrolling.
MAX_PERIODS: Final[int] = 8

NO_STATEMENTS: Final[str] = (
    "No financial statements have been filed for this borrower yet. This panel populates "
    "once a statement import completes."
)
NO_COVENANT_HISTORY: Final[str] = (
    "No covenant test is recorded against these periods, so covenant movement cannot be "
    "attributed to them."
)
NOT_FILED: Final[str] = "Not filed"

Movement = Literal["up", "down", "flat", "unknown"]
Tone = Literal["adverse", "favourable", "neutral"]

#: The lines the panel presents, in reading order, grouped by statement.
#: `current_assets` is here despite not being one of the headline figures
#: because it is the current ratio's numerator: showing current liabilities
#: without it would leave the liquidity covenant the one covenant on the
#: screen whose movement could not be explained from the lines beside it.
_LINE_ORDER: Final[tuple[str, ...]] = (
    "revenue",
    "ebitda",
    "ebit",
    "finance_cost",
    "tangible_net_worth",
    "total_debt",
    "current_assets",
    "current_liabilities",
    "cash_flow_debt_service",
)

_STATEMENT_LABELS: Final[Mapping[str, str]] = {
    "profit_and_loss": "Profit and loss",
    "balance_sheet": "Balance sheet",
    "cash_flow": "Cash flow",
    "ownership": "Ownership",
}

#: Which way each line has to move before a credit reader should worry. This
#: is a presentation judgement about emphasis only — it colours a chip and
#: chooses a verb. It never changes a number, and it is never consulted for a
#: covenant verdict, which comes from the stored test in every case.
_ADVERSE_WHEN_RISING: Final[frozenset[str]] = frozenset(
    {"finance_cost", "total_debt", "current_liabilities"}
)

#: A short reading of what each line is *for*, so the panel explains its own
#: rows rather than assuming every reader knows which covenant a line feeds.
_LINE_NOTES: Final[Mapping[str, str]] = {
    "revenue": "Top line for the period. Sets the scale every margin is read against.",
    "ebitda": "Earnings before interest, tax, depreciation and amortisation.",
    "ebit": "Operating earnings. The numerator of interest cover.",
    "finance_cost": "Interest and financing charges. The denominator of interest cover and DSCR.",
    "tangible_net_worth": "Net worth excluding intangibles. The denominator of leverage.",
    "total_debt": "Short- and long-term borrowings. The numerator of leverage.",
    "current_assets": "The numerator of the current ratio.",
    "current_liabilities": "The denominator of the current ratio.",
    "cash_flow_debt_service": "Cash generated and available to service debt. DSCR's numerator.",
}

#: Numerator and denominator lines per ratio code, for movement attribution.
#:
#: Deliberately declared rather than parsed out of `RatioDefinition.
#: formula_text`: that field is a human-readable string whose shape the
#: library is free to change, and a screen that silently mis-attributes a
#: breach to the wrong side of a fraction because a formula was reworded is
#: worse than one that quietly declines to attribute it at all. A ratio
#: absent from this mapping still renders in full — it simply carries no
#: attribution sentence.
_RATIO_TERMS: Final[Mapping[str, tuple[str, str]]] = {
    "leverage_ratio": ("total_debt", "tangible_net_worth"),
    "interest_coverage_ratio": ("ebit", "finance_cost"),
    "current_ratio": ("current_assets", "current_liabilities"),
    "fixed_charge_coverage_ratio": ("ebitda", "finance_cost"),
    "debt_to_ebitda": ("total_debt", "ebitda"),
    "dscr": ("cash_flow_debt_service", "finance_cost"),
    "ebitda_margin": ("ebitda", "revenue"),
    "tol_tnw": ("total_liabilities", "tangible_net_worth"),
}

#: The uncovenanted indicators the panel adds beside the borrower's actual
#: covenants, with the reason each earns its place. They are computed, never
#: read from a test row, because no covenant tests them — see the module
#: docstring on why that is the one exception to the read-only rule.
_CONTEXT_RATIOS: Final[tuple[tuple[str, str], ...]] = (
    (
        "debt_to_ebitda",
        "The leverage measure most lenders syndicate on, shown beside the covenanted "
        "net-worth-based one so a divergence between the two is visible.",
    ),
    (
        "dscr",
        "Whether cash actually generated covers the financing charge — the coverage "
        "question interest cover answers on an accrual basis.",
    ),
    (
        "ebitda_margin",
        "Earnings quality behind the coverage ratios. A covenant held by margin "
        "compression reads differently from one held by a volume fall.",
    ),
)

_VERDICT_LABELS: Final[Mapping[str, str]] = {
    "pass": "Pass",
    "warning": "Warning",
    "breach": "Breach",
    "breach_cure_open": "Breach — cure open",
    "stale": "Stale",
    "not_computable": "Not computable",
}
_BREACHING_VERDICTS: Final[frozenset[str]] = frozenset({"breach", "breach_cure_open"})

_RATIO_QUANTUM: Final[Decimal] = Decimal("0.01")
_AMOUNT_QUANTUM: Final[Decimal] = Decimal("0.01")
_PERCENT_QUANTUM: Final[Decimal] = Decimal("0.1")
#: Below this, a period-on-period move is noise and is reported as flat
#: rather than given a direction arrow it does not deserve.
_FLAT_THRESHOLD_PCT: Final[Decimal] = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class PeriodColumnView:
    """One filed financial period — a column of the financials table."""

    period_id: UUID
    label: str
    ends_on: date
    period_type: str
    is_audited: bool
    is_complete: bool
    is_latest: bool


@dataclass(frozen=True, slots=True)
class LineCellView:
    """One line's value in one period, or its stated absence."""

    display: str
    is_filed: bool


@dataclass(frozen=True, slots=True)
class FinancialLineView:
    """One statement line across every filed period, with its trend."""

    code: str
    label: str
    statement_label: str
    note: str
    cells: tuple[LineCellView, ...]
    latest_display: str
    change_display: str
    movement: Movement
    tone: Tone
    sparkline_svg: Markup | None
    feeds_display: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "cells", tuple(self.cells))
        if not self.code.strip() or not self.label.strip():
            raise ValueError("A financial line view requires a code and a label.")
        if self.movement not in {"up", "down", "flat", "unknown"}:
            raise ValueError(f"Unsupported line movement: {self.movement!r}.")
        if self.tone not in {"adverse", "favourable", "neutral"}:
            raise ValueError(f"Unsupported line tone: {self.tone!r}.")


@dataclass(frozen=True, slots=True)
class RatioPointView:
    """One period's position for one ratio."""

    period_label: str
    value_display: str
    verdict: str
    verdict_display: str
    headroom_display: str
    is_breach: bool


@dataclass(frozen=True, slots=True)
class RatioTrendView:
    """One ratio across the filed periods, with what moved it.

    `is_covenanted` separates the two kinds of row this panel shows, and the
    template must keep them visually distinct: a covenanted row's every
    figure is a stored engine verdict, an uncovenanted row's is an indicative
    calculation with no contractual force whatsoever.
    """

    code: str
    name: str
    is_covenanted: bool
    covenant_reference: str
    facility_reference: str
    formula_display: str
    unit: str
    obligation_display: str
    latest_display: str
    latest_verdict: str
    latest_verdict_display: str
    latest_headroom_display: str
    points: tuple[RatioPointView, ...]
    chart_svg: Markup | None
    attribution: str
    purpose: str
    first_breach_label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))
        if not self.code.strip() or not self.name.strip():
            raise ValueError("A ratio trend view requires a code and a name.")


@dataclass(frozen=True, slots=True)
class FinancialsPanelView:
    """The financials tab for one borrower."""

    state: Literal["rest", "empty"]
    periods: tuple[PeriodColumnView, ...]
    lines: tuple[FinancialLineView, ...]
    covenanted_ratios: tuple[RatioTrendView, ...]
    context_ratios: tuple[RatioTrendView, ...]
    headline: str
    empty_message: str = NO_STATEMENTS
    currency_note: str = "All amounts in ₹ crore, as filed."

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", tuple(self.periods))
        object.__setattr__(self, "lines", tuple(self.lines))
        object.__setattr__(self, "covenanted_ratios", tuple(self.covenanted_ratios))
        object.__setattr__(self, "context_ratios", tuple(self.context_ratios))
        if self.state not in {"rest", "empty"}:
            raise ValueError(f"Unsupported financials panel state: {self.state!r}.")


def build_financials_panel(
    session: Session,
    borrower_id: UUID,
    *,
    scope: Scope,
) -> FinancialsPanelView:
    """Assemble the financials panel from scoped, persisted records only."""

    periods = _filed_periods(session, borrower_id, scope)
    if not periods:
        return FinancialsPanelView(
            state="empty",
            periods=(),
            lines=(),
            covenanted_ratios=(),
            context_ratios=(),
            headline="",
        )

    lines_by_period = _line_values(session, periods, scope)
    tests_by_period = _covenant_tests(session, borrower_id, periods, scope)

    columns = tuple(
        PeriodColumnView(
            period_id=period.id,
            label=period.fy_label,
            ends_on=period.period_end,
            period_type=period.period_type.replace("_", " "),
            is_audited=period.is_audited,
            is_complete=period.is_complete,
            is_latest=index == len(periods) - 1,
        )
        for index, period in enumerate(periods)
    )
    line_views = _line_views(columns, lines_by_period, tests_by_period)
    covenanted = _covenanted_ratios(columns, lines_by_period, tests_by_period)
    context = _context_ratios(columns, lines_by_period)
    return FinancialsPanelView(
        state="rest",
        periods=columns,
        lines=line_views,
        covenanted_ratios=covenanted,
        context_ratios=context,
        headline=_headline(columns, covenanted),
    )


# ---------------------------------------------------------------------------
# Scoped reads
# ---------------------------------------------------------------------------


def _filed_periods(
    session: Session, borrower_id: UUID, scope: Scope
) -> tuple[FinancialPeriod, ...]:
    """Return the standing filings, oldest first, most recent `MAX_PERIODS`.

    `superseded_by_id IS NULL` is the filter that matters: a restatement
    (`db/models/statements.py`) writes a *new* period row and chains the one
    it replaced, so filtering on the version number would show the original
    filing rather than the figures the covenant is now tested against.
    """

    statement = (
        select(FinancialPeriod)
        .join(Borrower, Borrower.id == FinancialPeriod.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            FinancialPeriod.borrower_id == borrower_id,
            FinancialPeriod.superseded_by_id.is_(None),
            scope.predicate(Portfolio.path),
        )
        .order_by(
            FinancialPeriod.period_end.desc(),
            FinancialPeriod.version.desc(),
            FinancialPeriod.id.desc(),
        )
        .limit(MAX_PERIODS)
    )
    rows = tuple(session.execute(statement).scalars().all())
    return tuple(reversed(rows))


def _line_values(
    session: Session, periods: Sequence[FinancialPeriod], scope: Scope
) -> dict[UUID, dict[str, Decimal]]:
    """Read every filed line for these periods in one scoped batch.

    The portfolio predicate is carried here even though `_filed_periods`
    already resolved these ids under the same scope — the read model's own
    rule (`view_models/borrower.py`) is that the row-level access rule stays
    visible at every query boundary rather than being inherited from a
    caller that a later refactor might change.
    """

    period_ids = [period.id for period in periods]
    if not period_ids:
        return {}
    statement = (
        select(StatementLineValue)
        .join(FinancialPeriod, FinancialPeriod.id == StatementLineValue.period_id)
        .join(Borrower, Borrower.id == FinancialPeriod.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            StatementLineValue.period_id.in_(period_ids),
            scope.predicate(Portfolio.path),
        )
    )
    grouped: dict[UUID, dict[str, Decimal]] = {period_id: {} for period_id in period_ids}
    for value in session.execute(statement).scalars().all():
        grouped.setdefault(value.period_id, {})[value.line_code] = value.value
    return grouped


@dataclass(frozen=True, slots=True)
class _TestFacts:
    """One stored covenant test, reduced to what this panel displays."""

    covenant_reference: str
    covenant_name: str
    facility_reference: str
    definition_ref: str
    direction: str
    unit: str
    value: Decimal | None
    threshold_used: Decimal | None
    headroom_pct: Decimal | None
    verdict: str


def _covenant_tests(
    session: Session,
    borrower_id: UUID,
    periods: Sequence[FinancialPeriod],
    scope: Scope,
) -> dict[UUID, dict[str, _TestFacts]]:
    """Read every covenant test taken against these periods, by period then code.

    Keyed on `definition_ref` rather than covenant id so the panel groups by
    the *ratio* a reader recognises ("leverage") across whatever covenant
    references and facility the bank happens to have booked it under. Where a
    borrower holds the same ratio on two facilities, the later-computed test
    wins the row, and the ledger on the covenants tab remains the per-facility
    record — this panel is about movement in a ratio, not a facility roll-up.
    """

    period_ids = [period.id for period in periods]
    if not period_ids:
        return {}
    statement = (
        select(CovenantTest, Covenant, CovenantVersion, Facility)
        .select_from(CovenantTest)
        .join(CovenantVersion, CovenantVersion.id == CovenantTest.covenant_version_id)
        .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
        .join(Facility, Facility.id == Covenant.facility_id)
        .join(Borrower, Borrower.id == Facility.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            CovenantTest.period_id.in_(period_ids),
            Borrower.id == borrower_id,
            Covenant.is_active.is_(True),
            CovenantVersion.definition_ref.is_not(None),
            scope.predicate(Portfolio.path),
        )
        .order_by(CovenantTest.computed_at, CovenantTest.id)
    )
    grouped: dict[UUID, dict[str, _TestFacts]] = {}
    for test, covenant, version, facility in session.execute(statement).tuples().all():
        if test.period_id is None or version.definition_ref is None:
            continue
        grouped.setdefault(test.period_id, {})[version.definition_ref] = _TestFacts(
            covenant_reference=covenant.reference,
            covenant_name=covenant.name,
            facility_reference=facility.reference,
            definition_ref=version.definition_ref,
            direction=version.direction,
            unit=version.unit,
            value=test.value,
            threshold_used=test.threshold_used,
            headroom_pct=test.headroom_pct,
            verdict=test.verdict,
        )
    return grouped


# ---------------------------------------------------------------------------
# Statement lines
# ---------------------------------------------------------------------------


def _line_views(
    columns: Sequence[PeriodColumnView],
    lines_by_period: Mapping[UUID, Mapping[str, Decimal]],
    tests_by_period: Mapping[UUID, Mapping[str, _TestFacts]],
) -> tuple[FinancialLineView, ...]:
    feeds = _lines_to_covenants(tests_by_period)
    views: list[FinancialLineView] = []
    for code in _LINE_ORDER:
        series = tuple(lines_by_period.get(column.period_id, {}).get(code) for column in columns)
        if all(value is None for value in series):
            # A line no filing ever carried is omitted rather than shown as a
            # row of dashes: an empty row reads as a data-quality problem when
            # it usually means the bank's mapping does not extract that line.
            continue
        definition = _line_definition(code)
        filed = [
            (column, value)
            for column, value in zip(columns, series, strict=True)
            if value is not None
        ]
        first_column, first_value = filed[0]
        latest_value = filed[-1][1]
        movement, change_pct = _movement(first_value, latest_value)
        views.append(
            FinancialLineView(
                code=code,
                label=definition[0],
                statement_label=definition[1],
                note=_LINE_NOTES.get(code, ""),
                cells=tuple(
                    LineCellView(
                        display=_amount_display(value) if value is not None else NOT_FILED,
                        is_filed=value is not None,
                    )
                    for value in series
                ),
                latest_display=_amount_display(latest_value),
                change_display=_change_display(change_pct, movement, first_column.label),
                movement=movement,
                tone=_tone(code, movement),
                sparkline_svg=_line_sparkline(code, definition[0], columns, series),
                feeds_display=", ".join(feeds.get(code, ())),
            )
        )
    return tuple(views)


def _line_definition(code: str) -> tuple[str, str]:
    """Return the line's display name and statement, from the seeded chart.

    Read through the domain chart rather than restated here, so a bank that
    renames a line in its taxonomy renames it on this screen too.
    """

    try:
        definition = default_chart().get(code)
    except ChartError:
        return code.replace("_", " ").capitalize(), ""
    return definition.name, _STATEMENT_LABELS.get(definition.statement, "")


def _lines_to_covenants(
    tests_by_period: Mapping[UUID, Mapping[str, _TestFacts]],
) -> dict[str, tuple[str, ...]]:
    """Map each statement line to the ratios that read it.

    This is what turns the table from a filing into an explanation: a reader
    scanning `total_debt` sees "Feeds: Leverage ratio" and knows, without
    leaving the row, which covenant a movement there will land on.

    Indicative ratios are named too, but always suffixed, because a line that
    feeds only uncovenanted ratios would otherwise show an empty cell and
    read as though nothing depends on it — revenue and cash flow available
    for debt service are exactly that case, and both are figures a reviewer
    is expected to act on. The suffix is what keeps "feeds a covenant" and
    "feeds a number we show you" from collapsing into the same claim.
    """

    covenanted: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for period_tests in tests_by_period.values():
        for definition_ref, facts in period_tests.items():
            terms = _RATIO_TERMS.get(definition_ref)
            if terms is None:
                continue
            for line_code in terms:
                key = (line_code, facts.covenant_name)
                if key in seen:
                    continue
                seen.add(key)
                covenanted.setdefault(line_code, []).append(facts.covenant_name)

    tested_codes = {code for period in tests_by_period.values() for code in period}
    indicative: dict[str, list[str]] = {}
    for code, _purpose in _CONTEXT_RATIOS:
        if code in tested_codes:
            continue
        definition = LIBRARY.get(code)
        terms = _RATIO_TERMS.get(code)
        if definition is None or terms is None:
            continue
        for line_code in terms:
            indicative.setdefault(line_code, []).append(f"{definition.name} (indicative)")

    return {
        code: tuple(sorted(covenanted.get(code, ()))) + tuple(sorted(indicative.get(code, ())))
        for code in set(covenanted) | set(indicative)
    }


def _line_sparkline(
    code: str,
    label: str,
    columns: Sequence[PeriodColumnView],
    series: Sequence[Decimal | None],
) -> Markup | None:
    points = tuple(
        SeriesPoint(label=column.label, value=value)
        for column, value in zip(columns, series, strict=True)
        if value is not None
    )
    if len(points) < 2:
        return None
    try:
        return render_series_svg(
            f"line-{code}",
            points,
            label=f"{label} across filed periods",
            value_labels=tuple(_amount_display(point.value) for point in points),
        )
    except (TypeError, ValueError, InvalidOperation):
        return None


# ---------------------------------------------------------------------------
# Ratios
# ---------------------------------------------------------------------------


def _covenanted_ratios(
    columns: Sequence[PeriodColumnView],
    lines_by_period: Mapping[UUID, Mapping[str, Decimal]],
    tests_by_period: Mapping[UUID, Mapping[str, _TestFacts]],
) -> tuple[RatioTrendView, ...]:
    """Build one trend per covenanted ratio, entirely from stored tests."""

    codes: list[str] = []
    for column in columns:
        for code in tests_by_period.get(column.period_id, {}):
            if code not in codes:
                codes.append(code)

    views: list[RatioTrendView] = []
    for code in codes:
        tested = [
            (column, tests_by_period[column.period_id][code])
            for column in columns
            if code in tests_by_period.get(column.period_id, {})
        ]
        if not tested:
            continue
        latest_column, latest = tested[-1]
        points = tuple(
            RatioPointView(
                period_label=column.label,
                value_display=_ratio_display(facts.value, facts.unit),
                verdict=facts.verdict,
                verdict_display=_VERDICT_LABELS.get(facts.verdict, facts.verdict),
                headroom_display=_headroom_display(facts.headroom_pct),
                is_breach=facts.verdict in _BREACHING_VERDICTS,
            )
            for column, facts in tested
        )
        first_breach = next((point.period_label for point in points if point.is_breach), "")
        views.append(
            RatioTrendView(
                code=code,
                name=latest.covenant_name,
                is_covenanted=True,
                covenant_reference=latest.covenant_reference,
                facility_reference=latest.facility_reference,
                formula_display=_formula_display(code),
                unit=latest.unit,
                obligation_display=_obligation_display(latest),
                latest_display=_ratio_display(latest.value, latest.unit),
                latest_verdict=latest.verdict,
                latest_verdict_display=_VERDICT_LABELS.get(latest.verdict, latest.verdict),
                latest_headroom_display=_headroom_display(latest.headroom_pct),
                points=points,
                chart_svg=_ratio_chart(
                    code,
                    latest.covenant_name,
                    tested,
                    unit=latest.unit,
                    threshold=latest.threshold_used,
                    breach_above=latest.direction == "max",
                ),
                attribution=_attribution(
                    code,
                    columns,
                    lines_by_period,
                    first_value=tested[0][1].value,
                    latest_value=latest.value,
                    unit=latest.unit,
                    first_label=tested[0][0].label,
                    latest_label=latest_column.label,
                    threshold=latest.threshold_used,
                    direction=latest.direction,
                    first_breach_label=first_breach,
                ),
                purpose="",
                first_breach_label=first_breach,
            )
        )
    return tuple(views)


def _context_ratios(
    columns: Sequence[PeriodColumnView],
    lines_by_period: Mapping[UUID, Mapping[str, Decimal]],
) -> tuple[RatioTrendView, ...]:
    """Compute the uncovenanted indicators through the domain ratio library.

    These are the only computed figures on the panel, and they are computed
    the same way the engine would: `compute_ratio` dispatches to the library's
    own pure formula for the code, so an indicator shown here can never be
    arithmetic that disagrees with the arithmetic a covenant on the same code
    would receive. A period whose lines cannot support the formula is skipped
    rather than filled in.
    """

    views: list[RatioTrendView] = []
    for code, purpose in _CONTEXT_RATIOS:
        definition = LIBRARY.get(code)
        if definition is None:
            continue
        computed: list[tuple[PeriodColumnView, Decimal]] = []
        for column in columns:
            lines = lines_by_period.get(column.period_id, {})
            if not lines:
                continue
            result = compute_ratio(definition, lines, None)
            if result.computable and result.value is not None:
                computed.append((column, result.value))
        if len(computed) < 2:
            continue
        latest_column, latest_value = computed[-1]
        first_column, first_value = computed[0]
        points = tuple(
            RatioPointView(
                period_label=column.label,
                value_display=_ratio_display(value, definition.unit),
                verdict="indicative",
                verdict_display="Not covenanted",
                headroom_display="—",
                is_breach=False,
            )
            for column, value in computed
        )
        views.append(
            RatioTrendView(
                code=code,
                name=definition.name,
                is_covenanted=False,
                covenant_reference="",
                facility_reference="",
                formula_display=_formula_display(code),
                unit=definition.unit,
                obligation_display="No covenant tests this ratio for this borrower.",
                latest_display=_ratio_display(latest_value, definition.unit),
                latest_verdict="indicative",
                latest_verdict_display="Not covenanted",
                latest_headroom_display="—",
                points=points,
                chart_svg=_ratio_chart(
                    code, definition.name, None, unit=definition.unit, series=computed
                ),
                attribution=_attribution(
                    code,
                    columns,
                    lines_by_period,
                    first_value=first_value,
                    latest_value=latest_value,
                    unit=definition.unit,
                    first_label=first_column.label,
                    latest_label=latest_column.label,
                    threshold=None,
                    direction=definition.direction_hint or "",
                    first_breach_label="",
                ),
                purpose=purpose,
                first_breach_label="",
            )
        )
    return tuple(views)


def _ratio_chart(
    code: str,
    name: str,
    tested: Sequence[tuple[PeriodColumnView, _TestFacts]] | None,
    *,
    unit: str,
    series: Sequence[tuple[PeriodColumnView, Decimal]] | None = None,
    threshold: Decimal | None = None,
    breach_above: bool | None = None,
) -> Markup | None:
    if tested is not None:
        pairs = [
            (column, facts.value, facts.verdict in _BREACHING_VERDICTS)
            for column, facts in tested
            if facts.value is not None
        ]
    else:
        pairs = [(column, value, False) for column, value in (series or ())]
    points = tuple(
        SeriesPoint(label=column.label, value=value, is_breach=is_breach)
        for column, value, is_breach in pairs
        if value is not None
    )
    if len(points) < 2:
        return None
    try:
        return render_series_svg(
            f"ratio-{code}",
            points,
            label=f"{name} across filed periods",
            value_labels=tuple(_ratio_display(point.value, unit) for point in points),
            threshold=threshold,
            threshold_label=_ratio_display(threshold, unit) if threshold is not None else "",
            breach_above=breach_above,
        )
    except (TypeError, ValueError, InvalidOperation):
        return None


def _attribution(
    code: str,
    columns: Sequence[PeriodColumnView],
    lines_by_period: Mapping[UUID, Mapping[str, Decimal]],
    *,
    first_value: Decimal | None,
    latest_value: Decimal | None,
    unit: str,
    first_label: str,
    latest_label: str,
    threshold: Decimal | None,
    direction: str,
    first_breach_label: str,
) -> str:
    """State, in a sentence, which side of the fraction moved the ratio.

    This is the panel's whole reason for existing. A ratio that fell from
    3.15x to 1.44x is a fact the covenants tab already gives a reader; that
    it fell because operating earnings halved while the interest bill held
    steady is the fact that decides whether the next call is about a covenant
    reset or about the business. Both halves come from the same filed lines
    already tabulated above, so the sentence can always be checked against
    the table it sits under.

    Nothing here is inferred where it could be read: the crossing period is
    the first stored *breach verdict*, never a comparison this function makes
    against the threshold itself.
    """

    terms = _RATIO_TERMS.get(code)
    if terms is None or first_value is None or latest_value is None or first_label == latest_label:
        return ""
    numerator_code, denominator_code = terms
    numerator = _endpoints(numerator_code, columns, lines_by_period)
    denominator = _endpoints(denominator_code, columns, lines_by_period)
    if numerator is None or denominator is None:
        return ""

    numerator_label, _ = _line_definition(numerator_code)
    denominator_label, _ = _line_definition(denominator_code)
    _, numerator_pct = _movement(*numerator)
    _, denominator_pct = _movement(*denominator)

    sentences = [
        f"{_ratio_display(first_value, unit)} at {first_label} to "
        f"{_ratio_display(latest_value, unit)} at {latest_label}."
    ]
    sentences.append(
        _side_clause(numerator_label, numerator, numerator_pct)
        + " while "
        + _side_clause(denominator_label, denominator, denominator_pct, lowercase=True)
        + "."
    )
    driver = _dominant_side(numerator_label, numerator_pct, denominator_label, denominator_pct)
    if driver:
        sentences.append(driver)
    limit = _limit_phrase(threshold, unit, direction)
    if first_breach_label:
        sentences.append(
            f"It first tested in breach of {limit} at {first_breach_label}."
            if limit
            else f"It first tested in breach at {first_breach_label}."
        )
    elif limit:
        sentences.append(f"It has not tested in breach of {limit} across these periods.")
    return " ".join(sentences)


def _limit_phrase(threshold: Decimal | None, unit: str, direction: str) -> str:
    """Name the threshold the way the covenant's own direction reads it.

    "the 3.00x ceiling" and "the 1.50x floor" say which way the covenant
    breaches in the same breath as the number, which the bare word "limit"
    does not — and the direction is read from the tested version, never
    inferred from where the value happens to sit relative to it.
    """

    if threshold is None:
        return ""
    value = _ratio_display(threshold, unit)
    if direction == "max":
        return f"the {value} ceiling"
    if direction == "min":
        return f"the {value} floor"
    return f"the {value} threshold"


def _endpoints(
    code: str,
    columns: Sequence[PeriodColumnView],
    lines_by_period: Mapping[UUID, Mapping[str, Decimal]],
) -> tuple[Decimal, Decimal] | None:
    """Return one line's first and last filed values across the periods shown."""

    filed = [
        value
        for column in columns
        if (value := lines_by_period.get(column.period_id, {}).get(code)) is not None
    ]
    if len(filed) < 2:
        return None
    return filed[0], filed[-1]


def _mid_sentence(label: str) -> str:
    """Lower a line's name for use mid-sentence, without wrecking an acronym.

    The chart's names are a mix of ordinary phrases ("Total debt") and
    acronyms ("EBIT", "EBITDA"). A blanket `.lower()` turns the second kind
    into "ebit", which reads as a typo in what is otherwise the panel's most
    carefully-worded sentence. A capital in the second position is the signal
    that the whole word is meant to be capitalised, so only names that fail
    that test are lowered.
    """

    if label[1:2].islower():
        return label[0].lower() + label[1:]
    return label


def _side_clause(
    label: str,
    endpoints: tuple[Decimal, Decimal],
    change_pct: Decimal | None,
    *,
    lowercase: bool = False,
) -> str:
    first, latest = endpoints
    name = _mid_sentence(label) if lowercase else label
    if change_pct is None or abs(change_pct) < _FLAT_THRESHOLD_PCT:
        return f"{name} held at {_amount_display(latest)}"
    verb = "rose" if latest > first else "fell"
    delta = _amount_display(abs(latest - first))
    return f"{name} {verb} {delta} ({_signed_percent(change_pct)}) to {_amount_display(latest)}"


def _dominant_side(
    numerator_label: str,
    numerator_pct: Decimal | None,
    denominator_label: str,
    denominator_pct: Decimal | None,
) -> str:
    """Name the side that actually moved, when one clearly did."""

    numerator_move = abs(numerator_pct) if numerator_pct is not None else Decimal(0)
    denominator_move = abs(denominator_pct) if denominator_pct is not None else Decimal(0)
    if max(numerator_move, denominator_move) < _FLAT_THRESHOLD_PCT:
        return ""
    if denominator_move < _FLAT_THRESHOLD_PCT:
        return f"The movement is entirely on the {_mid_sentence(numerator_label)} side."
    if numerator_move < _FLAT_THRESHOLD_PCT:
        return f"The movement is entirely on the {_mid_sentence(denominator_label)} side."
    # Twice the movement is the point at which naming one side is a reading
    # rather than a rounding: below it, both sides genuinely contributed and
    # the sentence above has already given the reader both figures.
    if numerator_move >= denominator_move * 2:
        return f"{numerator_label} is the larger mover."
    if denominator_move >= numerator_move * 2:
        return f"{denominator_label} is the larger mover."
    return "Both sides moved materially."


def _headline(columns: Sequence[PeriodColumnView], covenanted: Sequence[RatioTrendView]) -> str:
    """One line above the panel naming what these statements are showing."""

    if not columns:
        return ""
    span = f"{columns[0].label} to {columns[-1].label}"
    breaching = [ratio for ratio in covenanted if ratio.latest_verdict in _BREACHING_VERDICTS]
    if breaching:
        names = ", ".join(ratio.name.lower() for ratio in breaching)
        return (
            f"{len(columns)} filed periods, {span}. The figures below are the ones "
            f"{names} is tested on."
            if len(breaching) == 1
            else f"{len(columns)} filed periods, {span}. The figures below are the ones "
            f"{names} are tested on."
        )
    warning = [ratio for ratio in covenanted if ratio.latest_verdict == "warning"]
    if warning:
        names = ", ".join(ratio.name.lower() for ratio in warning)
        return (
            f"{len(columns)} filed periods, {span}. No covenant is in breach; "
            f"{names} is inside its warning band."
        )
    return f"{len(columns)} filed periods, {span}. No covenant is in breach on the filed figures."


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def _movement(first: Decimal, latest: Decimal) -> tuple[Movement, Decimal | None]:
    """Classify a first-to-latest move, with its percentage where meaningful.

    A move off a zero or negative base has no honest percentage — "up 400%"
    from a loss is arithmetic, not information — so the direction is returned
    without one rather than with a figure that would mislead.
    """

    if first == 0 or first < 0:
        if latest == first:
            return "flat", None
        return ("up" if latest > first else "down"), None
    change = ((latest - first) / first) * Decimal(100)
    if abs(change) < _FLAT_THRESHOLD_PCT:
        return "flat", change
    return ("up" if change > 0 else "down"), change


def _tone(code: str, movement: Movement) -> Tone:
    if movement in {"flat", "unknown"}:
        return "neutral"
    rising_is_adverse = code in _ADVERSE_WHEN_RISING
    is_adverse = (movement == "up") if rising_is_adverse else (movement == "down")
    return "adverse" if is_adverse else "favourable"


def _change_display(change_pct: Decimal | None, movement: Movement, since_label: str) -> str:
    if movement == "flat":
        return f"Held since {since_label}"
    if change_pct is None:
        return f"{'Higher' if movement == 'up' else 'Lower'} than {since_label}"
    return f"{_signed_percent(change_pct)} since {since_label}"


def _signed_percent(value: Decimal) -> str:
    rendered = value.quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    sign = "+" if rendered > 0 else ""
    return f"{sign}{format_indian_number(rendered)}%"


def _amount_display(value: Decimal) -> str:
    """Render a statement amount in ₹ crore, the unit the chart normalises to.

    `format_indian_currency`'s compact branches read a value as rupees and
    would render `230` as "₹230.00" — three orders of magnitude adrift of the
    ₹230 crore actually filed. Only the digit grouping is reused here; the
    unit is stated explicitly because it is the chart's, not the formatter's.
    """

    rendered = value.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_UP)
    sign = "−" if rendered < 0 else ""
    return f"{sign}₹{format_indian_number(abs(rendered))} cr"


def _ratio_display(value: Decimal | None, unit: str) -> str:
    if value is None:
        return "—"
    rendered = value.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_UP)
    if unit == "%":
        return f"{format_indian_number(rendered)}%"
    if unit in {"x", ""}:
        return f"{format_indian_number(rendered)}x"
    if unit == "days":
        return f"{format_indian_number(rendered)} days"
    return f"{format_indian_number(rendered)} {unit}"


def _headroom_display(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return _signed_percent(value)


def _obligation_display(facts: _TestFacts) -> str:
    if facts.threshold_used is None:
        return "No threshold is recorded on the tested version."
    limit = _ratio_display(facts.threshold_used, facts.unit)
    if facts.direction == "max":
        return f"Must stay at or below {limit}"
    return f"Must stay at or above {limit}"


def _formula_display(code: str) -> str:
    definition = LIBRARY.get(code)
    if definition is None:
        return ""
    return definition.formula_text.replace("_", " ")


__all__ = [
    "MAX_PERIODS",
    "NO_STATEMENTS",
    "FinancialLineView",
    "FinancialsPanelView",
    "LineCellView",
    "PeriodColumnView",
    "RatioPointView",
    "RatioTrendView",
    "build_financials_panel",
]
