"""Idempotent, presentation-ready Phase 7A demo data.

The demo seed deliberately uses the same registry and covenant engine as a
live deployment.  That keeps the showcase honest: queue bands, forecast
crossings, traces, audit rows and threshold snapshots are all produced by
the product's real services rather than a UI-only fixture.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from covenant_radar.audit.record import AuditRecorder
from covenant_radar.config.thresholds import DEFAULT_THRESHOLD_PATH, ThresholdStore
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.audit import ThresholdSnapshot
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.statements import (
    FieldProvenance,
    FinancialPeriod,
    ImportBatch,
    ImportMapping,
    StatementLineValue,
)
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.evaluate import PeriodFacts
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.engine import EngineService
from covenant_radar.services.registry import RegistryService

DEMO_PORTFOLIO_CODE: Final[str] = "REF-PORTFOLIO"
DEMO_BATCH_HASH: Final[str] = hashlib.sha256(
    b"covenant-radar-phase-7a-demo-statements-v1"
).hexdigest()
DEMO_SOURCE_REFERENCE: Final[str] = "evaluation/phase-7a-demo-statements-v1"
DEMO_PERIODS: Final[tuple[tuple[str, date, date], ...]] = (
    ("FY24Q3", date(2024, 7, 1), date(2024, 9, 30)),
    ("FY24Q4", date(2024, 10, 1), date(2024, 12, 31)),
    ("FY25Q1", date(2025, 1, 1), date(2025, 3, 31)),
    ("FY25Q2", date(2025, 4, 1), date(2025, 6, 30)),
    ("FY25Q3", date(2025, 7, 1), date(2025, 9, 30)),
    ("FY25Q4", date(2025, 10, 1), date(2025, 12, 31)),
    ("FY26Q1", date(2026, 1, 1), date(2026, 3, 31)),
    ("FY26Q2", date(2026, 4, 1), date(2026, 6, 30)),
)
#: The mapping the `/financial-statements` screen imports against.
#:
#: `phase-7a-demo-financials` below is only a provenance label for the
#: seeded statement rows — an `import_batch` needs a `mapping_id`, and its
#: spec was never a real `ImportMappingSpec`.  Nothing in the demo database
#: could therefore drive an actual import, so every upload on the screen
#: named after `INGEST_FINANCIAL_STATEMENTS` failed at `parse_mapping_spec`.
#: This is a real mapping over the same chart lines the demo portfolio
#: already carries, so an imported quarter lands in the same shape as the
#: seeded history and feeds the same covenant tests.
#:
#: Only non-derived lines are mapped; `ebitda`, `ebit`, `current_assets`,
#: `current_liabilities` and `total_debt` are left for the chart to derive,
#: which is what the covenant ratios read.
DEMO_IMPORT_MAPPING_NAME: Final[str] = "quarterly-financials-v1"
DEMO_IMPORT_MAPPING_SPEC: Final[dict[str, object]] = {
    "borrower_key_column": "borrower_key",
    "fy_label_column": "fy_label",
    "period_type_column": "period_type",
    "period_start_column": "period_start",
    "period_end_column": "period_end",
    "is_audited_column": "is_audited",
    "unit": "lakh",
    "currency": "INR",
    "sign": "as_reported",
    "columns": {
        "revenue_lakh": "revenue",
        "cogs_lakh": "cost_of_goods_sold",
        "opex_lakh": "operating_expenses",
        "depreciation_lakh": "depreciation",
        "finance_cost_lakh": "finance_cost",
        "tax_expense_lakh": "tax_expense",
        "pat_lakh": "profit_after_tax",
        "cash_and_bank_lakh": "cash_and_bank",
        "inventory_lakh": "inventory",
        "receivables_lakh": "receivables",
        "other_current_assets_lakh": "other_current_assets",
        "payables_lakh": "payables",
        "short_term_debt_lakh": "short_term_debt",
        "other_current_liabilities_lakh": "other_current_liabilities",
        "long_term_debt_lakh": "long_term_debt",
        "total_liabilities_lakh": "total_liabilities",
        "tangible_net_worth_lakh": "tangible_net_worth",
        "total_assets_lakh": "total_assets",
    },
    "totals_row": {"column": "borrower_key", "value": "TOTAL"},
}
DEMO_COVENANTS_PER_BORROWER: Final[int] = 3
DEMO_SIGNAL_DAYS: Final[int] = 35
DEMO_SIGNAL_END_DATE: Final[date] = date(2026, 8, 30)
DEMO_SIGNAL_SOURCE_REFERENCE: Final[str] = "evaluation/phase-7a-demo-signals-v1"
DEMO_SIGNAL_PATH: Final[Path] = Path("var/inbox/covenant-radar-demo-signals.json")
DEMO_SIGNAL_FAMILIES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("account_activity", "account_activity_change", "%", "activity_change_pct"),
    ("payment", "payment_delay", "days", "days_past_due"),
    ("utilisation", "facility_utilisation", "%", "utilisation_pct"),
    ("treasury", "treasury_outflow", "ratio", "cash_outflow_ratio"),
    ("concentration", "concentration_exposure", "%", "top_group_exposure_pct"),
    ("industry", "industry_indicator", "score", "industry_stress_score"),
    ("news", "news_event", "score", "news_risk_score"),
)

# The curated 24-company roster: `Borrower.reference` sequence number (1-based,
# matching `B-{sequence:06d}`) grouped into narrative tiers. Sequence numbers
# were hand-picked so each tier lands on a believable industry (see the
# seeding plan) once the reference portfolio is built at 24 borrowers, one per
# industry code. These tiers drive `_demo_lines` (leverage/coverage/liquidity
# trajectory) and the signal-evidence profile below.
_ALREADY_BREACHED: Final[tuple[int, ...]] = (2, 4, 7, 22)
_ABOUT_TO_BREACH: Final[tuple[int, ...]] = (5, 9, 12, 16, 17)
_DETERIORATING: Final[tuple[int, ...]] = (8, 11, 13, 14, 15, 24)
_EARLY_SIGNAL: Final[tuple[int, ...]] = (1, 10, 19, 23)
_SAFE_STABLE: Final[tuple[int, ...]] = (3, 6, 18, 20, 21)

# Which covenant (LEV/COV/LIQ) tells each borrower's breach story. Every
# borrower previously used leverage as the only covenant that ever moved
# meaningfully, so every alert in the portfolio traced back to the same
# "Leverage ratio" line — indistinguishable across 24 companies. Rotating the
# driver within every tier (roughly a third each) means the queue's "worst
# covenant" and the case file's binding covenant genuinely vary: some
# companies breach on leverage, some on interest coverage, some on the
# current ratio, spread evenly across act/amber/watch.
_DRIVER: Final[Mapping[int, str]] = {
    1: "LEV",
    2: "LEV",
    3: "LEV",
    4: "COV",
    5: "LEV",
    6: "COV",
    7: "LIQ",
    8: "LEV",
    9: "COV",
    10: "COV",
    11: "COV",
    12: "LIQ",
    13: "LIQ",
    14: "LEV",
    15: "COV",
    16: "LEV",
    17: "COV",
    18: "LIQ",
    19: "LIQ",
    20: "LEV",
    21: "COV",
    22: "LEV",
    23: "LEV",
    24: "LIQ",
}

# The flat, comfortably-healthy shape used for whichever two covenants are
# *not* a borrower's driver, so only the driver ever approaches its
# threshold. Each covenant type gets its own shape calibrated to its own
# threshold and direction (leverage breaches high, coverage and liquidity
# breach low), not a single number reused across different units.
_SAFE_SHAPE: Final[Mapping[str, tuple[Decimal, Decimal]]] = {
    "LEV": (Decimal("1.35"), Decimal("-0.01")),
    "COV": (Decimal("3.15"), Decimal("0.01")),
    "LIQ": (Decimal("2.85"), Decimal("0.01")),
}

# Tier-uniform driver shapes for the tiers that do not need per-borrower
# crossing-date variety: an already-breached covenant always shows today as
# its crossing date regardless of margin, and the amber/watch tiers never
# cross within the forecast horizon at all, so only their T1 band placement
# matters. Each is mirrored across LEV (max, 3.00x), COV (min, 1.50x) and LIQ
# (min, 1.20x) using the same start-to-threshold margin and slope magnitude
# so whichever covenant is the driver produces an equivalent distance and
# velocity.
_TIER_SHAPE: Final[Mapping[str, Mapping[str, tuple[Decimal, Decimal]]]] = {
    "already_breached": {
        "LEV": (Decimal("2.30"), Decimal("0.16")),
        "COV": (Decimal("2.20"), Decimal("-0.16")),
        "LIQ": (Decimal("1.90"), Decimal("-0.16")),
    },
    # Only the driver covenant moves for a given borrower now (the other two
    # sit on `_SAFE_SHAPE`), so it alone must carry the amber band instead of
    # three covenants reinforcing each other. Calibrated so the 90-day
    # projected distance is ~0.15 (raw score ~0.43), comfortably inside the
    # 0.40-0.69 T1 amber range without crossing its own threshold.
    "deteriorating": {
        "LEV": (Decimal("2.1312"), Decimal("0.09")),
        "COV": (Decimal("2.3688"), Decimal("-0.09")),
        "LIQ": (Decimal("2.0688"), Decimal("-0.09")),
    },
    "early_signal": {
        "LEV": (Decimal("1.55"), Decimal("0.03")),
        "COV": (Decimal("2.95"), Decimal("-0.03")),
        "LIQ": (Decimal("2.65"), Decimal("-0.03")),
    },
}

# About-to-breach borrowers are individually tuned rather than sharing one
# tier shape. `domain/forecast/path.py` adds sustained evidence pressure to
# the projected daily drift at full, undecayed magnitude, so this seed keeps
# every signal family non-adverse (see `_demo_signal_adverse`) and lets the
# financial trend alone decide the projected crossing date. Each entry names
# the borrower's driver covenant, its current (today) value, and its
# per-quarter slope, chosen so the 90-day projection crosses that covenant's
# own threshold on a different day per borrower (roughly 15/30/50/70/85
# days out) instead of every "about to breach" company converging on the
# same date.
_ABOUT_TO_BREACH_SHAPE: Final[Mapping[int, tuple[Decimal, Decimal]]] = {
    5: (Decimal("2.0686"), Decimal("0.13")),
    9: (Decimal("2.4527"), Decimal("-0.13")),
    12: (Decimal("2.1812"), Decimal("-0.13")),
    16: (Decimal("1.9903"), Decimal("0.13")),
    17: (Decimal("2.5311"), Decimal("-0.13")),
}

# Each tier's signal-evidence profile, used by `_demo_signal_value` for every
# family (the `payment` family is driven by `_DPD_TARGET` instead, feeding
# `facility_conduct` directly rather than a signal event — see
# `_ensure_demo_conduct`). No family is ever flagged adverse (see
# `_demo_signal_adverse`): the profile only shapes the *magnitude* shown on
# the Signals tab, so every tier still reads distinctly instead of the three
# non-act tiers all looking identically flat.
_TIER_PROFILE: Final[Mapping[str, str]] = {
    "already_breached": "deteriorating",
    "about_to_breach": "deteriorating",
    "deteriorating": "moderate",
    "early_signal": "noisy",
    "safe_stable": "stable",
}

# Target days-past-due for the borrower's SMA payment-conduct band
# (`none`/`SMA-0`/`SMA-1`/`SMA-2`/`beyond`), held constant across the signal
# window. A nonzero, sustained days-past-due is itself "sustained adverse
# evidence" under the same pressure mechanism as the other six families (see
# `_TIER_PROFILE` above), so it is restricted to the already-act tiers, where
# it only reinforces a band the leverage trend already guarantees. This
# still covers the full SMA vocabulary (see the seeding plan) and tells a
# sharper story than spreading it across every tier would: every amber/watch
# company has clean payment conduct, so the covenant forecast is shown
# catching deterioration before it would show up in conduct at all. Two
# deliberate divergences remain within the act tier: #9 is about to breach
# its covenant but pays on time; #17 is not yet covenant-breached but is
# already `beyond` on conduct, i.e. conduct worsened before the covenant did.
_DPD_TARGET: Final[Mapping[int, int]] = {
    1: 0,
    2: 95,
    3: 0,
    4: 70,
    5: 35,
    6: 0,
    7: 45,
    8: 0,
    9: 0,
    10: 0,
    11: 0,
    12: 15,
    13: 0,
    14: 0,
    15: 0,
    16: 40,
    17: 95,
    18: 0,
    19: 0,
    20: 0,
    21: 0,
    22: 25,
    23: 0,
    24: 0,
}


# The non-covenant statement lines (`_demo_lines`). None of these feeds a
# demo covenant threshold, so they are free to vary; they exist so a reader
# can see *why* a covenant moved. All amounts are ₹ crore per quarter, sized
# for a mid-corporate borrower with a roughly ₹1,200-1,600 crore annual top
# line — large enough for the three covenanted ratios to look proportionate
# beside them, small enough to stay a believable single relationship.
_REVENUE_BASE: Final[Decimal] = Decimal("300")
_REVENUE_GROWTH: Final[Decimal] = Decimal("4")
#: Per-borrower top-line spread, applied as `1 + spread * (index % 5)`, so
#: EBIT margin lands somewhere in roughly 8-11% across the portfolio rather
#: than every company reporting the identical margin.
_REVENUE_SPREAD: Final[Decimal] = Decimal("0.08")
_DEPRECIATION_OF_REVENUE: Final[Decimal] = Decimal("0.045")
_CFDS_OF_EBITDA: Final[Decimal] = Decimal("0.70")
_CFDS_DEBT_DRAG: Final[Decimal] = Decimal("0.015")


def _tier_for(borrower_index: int) -> str:
    """Return the narrative tier assigned to one curated borrower sequence."""

    if borrower_index in _ALREADY_BREACHED:
        return "already_breached"
    if borrower_index in _ABOUT_TO_BREACH:
        return "about_to_breach"
    if borrower_index in _DETERIORATING:
        return "deteriorating"
    if borrower_index in _EARLY_SIGNAL:
        return "early_signal"
    if borrower_index in _SAFE_STABLE:
        return "safe_stable"
    raise ValueError(f"Borrower {borrower_index} has no assigned demo tier.")


@dataclass(frozen=True, slots=True)
class DemoSeedReport:
    """Stable counts printed by ``radarctl seed --demo-covenants``."""

    borrowers: int
    covenants_created: int
    periods_created: int
    tests_created: int
    threshold_snapshot_id: UUID
    signal_events: int = 0


class _DemoAuditWriter:
    """Adapt the broad recorder API to service audit protocols."""

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        return self._recorder.record(
            event_type,
            subject,  # type: ignore[arg-type]
            payload,
            actor=actor,
            request_id=request_id,
        )


def seed_demo_covenants(
    session: Session,
    *,
    system_actor_id: UUID,
    clock: Clock | None = None,
    signal_path: str | Path | None = None,
) -> DemoSeedReport:
    """Seed the reference portfolio with real covenants and test history.

    Re-running the function only fills missing rows.  Existing rows are
    never rewritten, which makes it safe for a presenter to restart the
    bootstrap command or refresh an already-running demo environment.
    """

    if not isinstance(session, Session):
        raise TypeError("seed_demo_covenants requires a SQLAlchemy Session.")
    if not isinstance(system_actor_id, UUID):
        raise TypeError("system_actor_id must be a UUID.")

    demo_clock = clock or SystemClock()
    now = demo_clock.now()
    request_id = "demo-" + new_request_id()
    portfolio = session.scalar(select(Portfolio).where(Portfolio.code == DEMO_PORTFOLIO_CODE))
    if portfolio is None:
        raise ValueError(
            "The reference portfolio is missing. Run `radarctl seed --reference-portfolio` first."
        )

    borrowers = list(
        session.scalars(
            select(Borrower)
            .where(Borrower.portfolio_id == portfolio.id, Borrower.is_active.is_(True))
            .order_by(Borrower.reference)
            .limit(24)
        )
    )
    if len(borrowers) < 24:
        raise ValueError(
            f"The reference portfolio contains only {len(borrowers)} active borrowers; "
            "the curated 24-company demo roster requires all 24."
        )

    _clear_non_curated_financials(session, borrowers)

    batch = _ensure_import_batch(
        session,
        system_actor_id=system_actor_id,
        now=now,
        request_id=request_id,
        borrower_count=len(borrowers),
    )
    _ensure_statement_import_mapping(
        session,
        system_actor_id=system_actor_id,
        now=now,
        request_id=request_id,
    )
    threshold_snapshot_id = _ensure_demo_threshold_snapshot(
        session, system_actor_id=system_actor_id, now=now, request_id=request_id
    )
    principal = Principal.user(
        system_actor_id,
        (Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT),
    )
    scope = Scope(principal_id=system_actor_id, descendant_paths=(portfolio.path,))
    audit = _DemoAuditWriter(
        AuditRecorder(AuditRepository(session), clock=demo_clock, request_id=request_id)
    )
    registry = RegistryService(
        session,
        audit=audit,
        clock=demo_clock,
        request_id=request_id,
        maker_checker_enabled=False,
    )
    engine = EngineService(
        session,
        audit=audit,
        clock=demo_clock,
        request_id=request_id,
    )

    covenants_created = 0
    periods_created = 0
    tests_created = 0
    for borrower_index, borrower in enumerate(borrowers, start=1):
        facility = session.scalar(
            select(Facility)
            .where(Facility.borrower_id == borrower.id)
            .order_by(Facility.reference)
            .limit(1)
        )
        if facility is None:
            continue
        _ensure_demo_conduct(
            session,
            facility=facility,
            borrower_index=borrower_index,
            as_of_date=now.date(),
            system_actor_id=system_actor_id,
            now=now,
            request_id=request_id,
        )

        versions: dict[str, CovenantVersion] = {}
        for kind, definition, threshold, direction, covenant_class, display_name in (
            ("LEV", "leverage_ratio", Decimal("3.00"), "max", "leverage", "Leverage ratio"),
            (
                "COV",
                "interest_coverage_ratio",
                Decimal("1.50"),
                "min",
                "coverage",
                "Interest coverage ratio",
            ),
            ("LIQ", "current_ratio", Decimal("1.20"), "min", "liquidity", "Current ratio"),
        ):
            reference = f"D{borrower_index:02d}{kind}"
            covenant = session.scalar(select(Covenant).where(Covenant.reference == reference))
            if covenant is None:
                registered = registry.register(
                    principal,
                    facility_id=facility.id,
                    reference=reference,
                    name=display_name,
                    covenant_class=covenant_class,
                    terms=CovenantVersionTerms(
                        definition_ref=definition,
                        custom_formula=None,
                        threshold=threshold,
                        direction=direction,
                        unit="x",
                        frequency="quarterly",
                        test_basis="period_end",
                        effective_from=DEMO_PERIODS[0][1],
                        warning_headroom_pct=Decimal("10.00"),
                        cure_days=120,
                        grace_days=0,
                    ),
                    scope=scope,
                )
                covenant = registered.covenant
                covenants_created += 1
            version = session.scalar(
                select(CovenantVersion)
                .where(CovenantVersion.covenant_id == covenant.id)
                .order_by(CovenantVersion.version_no.desc())
                .limit(1)
            )
            if version is not None:
                versions[kind] = version

        period_rows: list[tuple[FinancialPeriod, Mapping[str, Decimal]]] = []
        for period_index, (label, period_start, period_end) in enumerate(DEMO_PERIODS):
            period = session.scalar(
                select(FinancialPeriod).where(
                    FinancialPeriod.borrower_id == borrower.id,
                    FinancialPeriod.fy_label == label,
                    FinancialPeriod.version == 1,
                )
            )
            if period is None:
                period = FinancialPeriod(
                    id=new_id(),
                    borrower_id=borrower.id,
                    fy_label=label,
                    period_type="quarterly",
                    period_start=period_start,
                    period_end=period_end,
                    is_complete=True,
                    is_audited=True,
                    source_batch_id=batch.id,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    created_by_id=system_actor_id,
                    updated_by_id=system_actor_id,
                    request_id=request_id,
                )
                session.add(period)
                session.flush()
                periods_created += 1

            period_lines = _demo_lines(borrower_index, period_index)
            _ensure_statement_lines(
                session,
                period=period,
                lines=period_lines,
                batch=batch,
                system_actor_id=system_actor_id,
                now=now,
                request_id=request_id,
            )
            period_rows.append((period, period_lines))

        session.flush()
        for kind, version in versions.items():
            for period, lines in period_rows:
                existing = session.scalar(
                    select(CovenantTest.id).where(
                        CovenantTest.covenant_version_id == version.id,
                        CovenantTest.period_id == period.id,
                    )
                )
                if existing is not None:
                    continue
                result = engine.test(
                    principal,
                    covenant_version_id=version.id,
                    period=PeriodFacts(
                        period_label=period.fy_label,
                        period_id=period.id,
                        as_of_date=period.period_end,
                        is_complete=period.is_complete,
                    ),
                    lines=lines,
                    scope=scope,
                    as_of_date=period.period_end,
                    period_id=period.id,
                )
                current_inputs = dict(result.inputs) if isinstance(result.inputs, Mapping) else {}
                current_inputs["demo_driver"] = _driver_for(kind, borrower_index)
                result.inputs = current_inputs
                tests_created += 1
        session.flush()

    signal_events = 0
    if signal_path is not None:
        signal_events = _ensure_demo_signal_source(Path(signal_path), borrowers, session)

    return DemoSeedReport(
        borrowers=len(borrowers),
        covenants_created=covenants_created,
        periods_created=periods_created,
        tests_created=tests_created,
        threshold_snapshot_id=threshold_snapshot_id,
        signal_events=signal_events,
    )


def _ensure_demo_conduct(
    session: Session,
    *,
    facility: Facility,
    borrower_index: int,
    as_of_date: date,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> None:
    """Write the borrower's SMA-driving conduct row directly.

    Signal-event ingestion only derives `facility_conduct` for the base
    reference portfolio's own generator (`evaluation.reference_portfolio`);
    the demo's curated signal fixture is ingested through the nightly
    pipeline's file source without a matching conduct-derivation step, so
    `_DPD_TARGET` would otherwise never reach the SMA band the queue reads.
    The SMA lookup requires an exact `as_of_date` match, so this writes
    today's row directly — the same way the curated financial statements are
    written directly rather than through a generic ingest pipeline.
    """

    existing = session.scalar(
        select(FacilityConduct).where(
            FacilityConduct.facility_id == facility.id,
            FacilityConduct.as_of_date == as_of_date,
        )
    )
    if existing is not None:
        return
    session.add(
        FacilityConduct(
            id=new_id(),
            facility_id=facility.id,
            as_of_date=as_of_date,
            outstanding=facility.outstanding,
            utilisation_pct=Decimal("60.00"),
            days_past_due=_DPD_TARGET[borrower_index],
            overdue_amount=None,
            excess_amount=Decimal("0.00"),
            source_id=None,
            created_at=now,
            updated_at=now,
            created_by_id=system_actor_id,
            updated_by_id=system_actor_id,
            request_id=request_id,
        )
    )
    session.flush()


def _clear_non_curated_financials(session: Session, borrowers: Sequence[Borrower]) -> None:
    """Remove the base reference-portfolio's own random financial history.

    ``load_reference_portfolio`` gives every borrower its own randomly
    generated financial periods (dated far in the past, unrelated to the
    curated tiers above). Left in place, the forecast trend fit picks up the
    single most recent one of those as a ninth, wildly-off-trend observation
    alongside the eight curated quarters, which can distort the fitted slope
    enough to push a borrower into the wrong queue band. The curated overlay
    fully owns these borrowers' financial story, so their non-curated
    periods are pure noise and are removed rather than left to interfere.
    """

    curated_labels = {label for label, _, _ in DEMO_PERIODS}
    borrower_ids = [borrower.id for borrower in borrowers]
    stray_period_ids = tuple(
        session.scalars(
            select(FinancialPeriod.id).where(
                FinancialPeriod.borrower_id.in_(borrower_ids),
                FinancialPeriod.fy_label.not_in(curated_labels),
            )
        )
    )
    if not stray_period_ids:
        return
    session.execute(
        delete(StatementLineValue).where(StatementLineValue.period_id.in_(stray_period_ids))
    )
    session.execute(delete(FinancialPeriod).where(FinancialPeriod.id.in_(stray_period_ids)))
    session.flush()


def _ensure_demo_signal_source(path: Path, borrowers: Sequence[Borrower], session: Session) -> int:
    """Write deterministic seven-family events for the local demo source.

    The file is deliberately a source artifact, not a database fixture.  The
    nightly ingest step reads it through ``FileSignalSource`` and therefore
    exercises the same validation, quarantine, evidence, attribution, and
    audit path as a production connector.  Deteriorating and moderate
    borrowers have a sustained adverse run (moderate at roughly half
    severity); noisy borrowers have isolated, non-adverse spikes; stable
    borrowers remain healthy.  The payment family is held at each borrower's
    target days-past-due independent of this profile, so the SMA
    payment-conduct band and the covenant-forecast band can agree or
    deliberately diverge.  Re-seeding never rewrites an existing source.
    """

    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = []
        return len(payload) if isinstance(payload, list) else 0

    path.parent.mkdir(parents=True, exist_ok=True)
    start_date = DEMO_SIGNAL_END_DATE.fromordinal(
        DEMO_SIGNAL_END_DATE.toordinal() - DEMO_SIGNAL_DAYS + 1
    )
    rows: list[dict[str, object]] = []
    for borrower_index, borrower in enumerate(borrowers, start=1):
        facility = session.scalar(
            select(Facility)
            .where(Facility.borrower_id == borrower.id)
            .order_by(Facility.reference)
            .limit(1)
        )
        if facility is None:
            continue
        profile = _TIER_PROFILE[_tier_for(borrower_index)]
        dpd_target = _DPD_TARGET[borrower_index]
        for day_offset in range(DEMO_SIGNAL_DAYS):
            event_date = start_date.fromordinal(start_date.toordinal() + day_offset)
            for family, event_type, unit, value_field in DEMO_SIGNAL_FAMILIES:
                if family == "payment":
                    value = _demo_payment_dpd(dpd_target)
                else:
                    value = _demo_signal_value(family, profile, day_offset, borrower_index)
                is_adverse = _demo_signal_adverse(profile, day_offset)
                rows.append(
                    {
                        "borrower_id": str(borrower.id),
                        "facility_id": str(facility.id),
                        "event_date": event_date.isoformat(),
                        "family": family,
                        "event_type": event_type,
                        "magnitude": value,
                        "unit": unit,
                        "payload": {
                            value_field: value,
                            "is_adverse": is_adverse,
                            "profile": profile,
                            "source_date": event_date.isoformat(),
                        },
                    }
                )
    path.write_text(
        json.dumps(_json_safe(rows), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return len(rows)


def _demo_signal_adverse(profile: str, day_offset: int) -> bool:
    """Never flag demo evidence adverse; see `_TIER_PROFILE` for why.

    `domain/forecast/path.py` adds sustained evidence pressure to the
    forecast's projected daily drift at full, undecayed magnitude, so any
    adverse-flagged family collapses every borrower's projected crossing to
    within a day or two regardless of its calibrated financial trend. The
    curated portfolio's bands and crossing dates are fully determined by the
    trend shapes in `_TIER_SHAPE`/`_ABOUT_TO_BREACH_SHAPE`, so evidence stays
    descriptive — it still varies by profile for a readable Signals tab —
    without ever entering the pressure term.
    """

    return False


def _demo_signal_value(family: str, profile: str, day_offset: int, borrower_index: int) -> Decimal:
    if profile == "deteriorating":
        base = {
            "account_activity": Decimal("12.00"),
            "payment": Decimal("8.00"),
            "utilisation": Decimal("78.00"),
            "treasury": Decimal("0.28"),
            "concentration": Decimal("48.00"),
            "industry": Decimal("0.62"),
            "news": Decimal("0.58"),
        }[family]
        slope = {
            "account_activity": Decimal("0.18"),
            "payment": Decimal("0.12"),
            "utilisation": Decimal("0.35"),
            "treasury": Decimal("0.008"),
            "concentration": Decimal("0.30"),
            "industry": Decimal("0.009"),
            "news": Decimal("0.011"),
        }[family]
        value = base + slope * day_offset + Decimal(borrower_index % 5) / 100
        return value.quantize(Decimal("0.001"))
    if profile == "moderate":
        # Roughly half the "deteriorating" severity: enough sustained
        # pressure to help lift the amber tier's raw score into the T1
        # amber band without pushing it as far as the act-band tiers.
        base = {
            "account_activity": Decimal("6.00"),
            "payment": Decimal("0"),
            "utilisation": Decimal("60.00"),
            "treasury": Decimal("0.18"),
            "concentration": Decimal("30.00"),
            "industry": Decimal("0.40"),
            "news": Decimal("0.35"),
        }[family]
        slope = {
            "account_activity": Decimal("0.07"),
            "payment": Decimal("0"),
            "utilisation": Decimal("0.14"),
            "treasury": Decimal("0.004"),
            "concentration": Decimal("0.12"),
            "industry": Decimal("0.004"),
            "news": Decimal("0.005"),
        }[family]
        value = base + slope * day_offset + Decimal(borrower_index % 5) / 100
        return value.quantize(Decimal("0.001"))
    if profile == "noisy":
        spike = Decimal("4.00") if day_offset in {10, 25} else Decimal("1.00")
        value = spike + Decimal((borrower_index * 3 + day_offset) % 7) / 10
        return value.quantize(Decimal("0.001"))
    value = Decimal("0.50") + Decimal((borrower_index + day_offset) % 5) / 10
    return value.quantize(Decimal("0.001"))


def _demo_payment_dpd(dpd_target: int) -> Decimal:
    """Hold the payment family at the borrower's target days-past-due.

    Held constant across the signal window (rather than driven by the
    deterioration ``profile`` used for the other six families) so
    ``facility_conduct.days_past_due`` on the final ingested day lands
    cleanly in the borrower's intended SMA-0/SMA-1/SMA-2/beyond band,
    independent of the covenant-forecast story.
    """

    return Decimal(dpd_target)


def _ensure_statement_import_mapping(
    session: Session,
    *,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> ImportMapping:
    """Seed the one mapping the financial-statement import screen can use.

    Kept separate from `_ensure_import_batch`'s provenance-label mapping so
    the two are not confused: that one records where the seeded rows came
    from, this one is a real `ImportMappingSpec` a presenter can import a
    fresh quarter against.
    """

    mapping = session.scalar(
        select(ImportMapping).where(
            ImportMapping.name == DEMO_IMPORT_MAPPING_NAME, ImportMapping.version == 1
        )
    )
    if mapping is not None:
        return mapping
    mapping = ImportMapping(
        id=new_id(),
        name=DEMO_IMPORT_MAPPING_NAME,
        source_type="csv",
        version=1,
        spec=dict(DEMO_IMPORT_MAPPING_SPEC),
        is_active=True,
        created_at=now,
        updated_at=now,
        created_by_id=system_actor_id,
        updated_by_id=system_actor_id,
        request_id=request_id,
    )
    session.add(mapping)
    session.flush()
    return mapping


def _ensure_import_batch(
    session: Session,
    *,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
    borrower_count: int,
) -> ImportBatch:
    batch = session.scalar(select(ImportBatch).where(ImportBatch.content_hash == DEMO_BATCH_HASH))
    if batch is not None:
        return batch
    mapping = session.scalar(
        select(ImportMapping).where(
            ImportMapping.name == "phase-7a-demo-financials", ImportMapping.version == 1
        )
    )
    if mapping is None:
        mapping = ImportMapping(
            id=new_id(),
            name="phase-7a-demo-financials",
            source_type="json",
            version=1,
            spec={"mapping_version": 1, "purpose": "Phase 7A presentation data"},
            is_active=True,
            created_at=now,
            updated_at=now,
            created_by_id=system_actor_id,
            updated_by_id=system_actor_id,
            request_id=request_id,
        )
        session.add(mapping)
        session.flush()
    row_count = borrower_count * len(DEMO_PERIODS)
    batch = ImportBatch(
        id=new_id(),
        source_type="json",
        source_reference=DEMO_SOURCE_REFERENCE,
        mapping_id=mapping.id,
        content_hash=DEMO_BATCH_HASH,
        started_at=now,
        finished_at=now,
        row_count=row_count,
        accepted_count=row_count,
        quarantined_count=0,
        state="completed",
        report={"seed": "phase-7a", "rows": row_count},
        created_at=now,
        updated_at=now,
        created_by_id=system_actor_id,
        updated_by_id=system_actor_id,
        request_id=request_id,
    )
    session.add(batch)
    session.flush()
    return batch


def _ensure_demo_threshold_snapshot(
    session: Session, *, system_actor_id: UUID, now: datetime, request_id: str
) -> UUID:
    existing = session.scalar(
        select(ThresholdSnapshot.id).where(
            ThresholdSnapshot.source == "calibration",
            ThresholdSnapshot.note == "Phase 7A demo calibration",
        )
    )
    if existing is not None:
        return existing
    values = _json_safe(ThresholdStore(path=DEFAULT_THRESHOLD_PATH).values())
    if not isinstance(values, dict):
        raise RuntimeError("The packaged threshold snapshot did not produce a JSON object.")
    values.update(
        {
            "T1": {"act": 0.70, "amber": 0.40},
            "T3": {"sustained_days": 14, "sustained_events": 3, "event_window_days": 30},
            "T4": {"headroom_erosion_pct": 0.05},
            "T5": {"contribution_share": 0.10},
        }
    )
    snapshot_id = new_id()
    session.add(
        ThresholdSnapshot(
            id=snapshot_id,
            values=values,
            source="calibration",
            effective_from=now,
            proposed_by_id=system_actor_id,
            approved_by_id=system_actor_id,
            note="Phase 7A demo calibration",
            version=(
                session.scalar(
                    select(ThresholdSnapshot.version)
                    .order_by(ThresholdSnapshot.version.desc())
                    .limit(1)
                )
                or 0
            )
            + 1,
            created_at=now,
            updated_at=now,
            created_by_id=system_actor_id,
            updated_by_id=system_actor_id,
            request_id=request_id,
        )
    )
    session.flush()
    return snapshot_id


def _ensure_statement_lines(
    session: Session,
    *,
    period: FinancialPeriod,
    lines: Mapping[str, Decimal],
    batch: ImportBatch,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> None:
    existing_codes = set(
        session.scalars(
            select(StatementLineValue.line_code).where(StatementLineValue.period_id == period.id)
        )
    )
    if existing_codes >= set(lines):
        return
    provenance = FieldProvenance(
        id=new_id(),
        source_type="json",
        source_reference=DEMO_SOURCE_REFERENCE,
        row_reference=f"{period.borrower_id}:{period.fy_label}",
        mapping_version=1,
        ingested_at=now,
        batch_id=batch.id,
        transform_note="Synthetic demo values; no identifying fields included.",
        created_at=now,
        updated_at=now,
        created_by_id=system_actor_id,
        updated_by_id=system_actor_id,
        request_id=request_id,
    )
    session.add(provenance)
    session.flush()
    for code, value in lines.items():
        if code in existing_codes:
            continue
        session.add(
            StatementLineValue(
                id=new_id(),
                period_id=period.id,
                line_code=code,
                value=value,
                unit="amount",
                currency="INR",
                provenance_id=provenance.id,
                created_at=now,
                updated_at=now,
                created_by_id=system_actor_id,
                updated_by_id=system_actor_id,
                request_id=request_id,
            )
        )
    session.flush()


def _covenant_shapes(borrower_index: int, tier: str) -> Mapping[str, tuple[Decimal, Decimal]]:
    """Return this borrower's (start, slope) pair for LEV, COV and LIQ.

    Only the borrower's driver covenant (`_DRIVER`) ever moves toward its
    threshold; the other two stay on their flat, healthy shape. This is what
    makes the queue's "worst covenant" vary across the portfolio instead of
    leverage winning for every borrower.
    """

    shapes = dict(_SAFE_SHAPE)
    driver = _DRIVER[borrower_index]
    if tier == "about_to_breach":
        shapes[driver] = _ABOUT_TO_BREACH_SHAPE[borrower_index]
    elif tier in _TIER_SHAPE:
        shapes[driver] = _TIER_SHAPE[tier][driver]
    return shapes


def _demo_lines(borrower_index: int, period_index: int) -> dict[str, Decimal]:
    """Return a coherent statement whose ratios follow the borrower's tier.

    The driver covenant crosses (or nears) its own threshold on the schedule
    its tier calls for; the other two covenants stay flat and healthy so
    they never become an accidental second binding covenant. See
    `_covenant_shapes` for how the driver is chosen and shaped.

    The six covenant-bearing lines are pinned by the covenant shape above
    and must not be re-derived here. The four lines below them exist so the
    case file can *explain* a covenant movement rather than only state it:
    a reader who sees leverage cross 3.00x needs revenue, EBITDA and cash
    flow available for debt service beside it to tell debt-funded expansion
    apart from earnings collapse. They are deliberately derived from the
    covenant lines rather than drawn independently, so no figure on the
    financials tab can contradict the covenant it sits next to.
    """

    tier = _tier_for(borrower_index)
    shapes = _covenant_shapes(borrower_index, tier)
    lev_start, lev_slope = shapes["LEV"]
    cov_start, cov_slope = shapes["COV"]
    liq_start, liq_slope = shapes["LIQ"]
    leverage = lev_start + lev_slope * period_index
    coverage = cov_start + cov_slope * period_index
    liquidity = liq_start + liq_slope * period_index

    total_debt = (leverage * Decimal("100")).quantize(Decimal("0.001"))
    ebit = (coverage * Decimal("10")).quantize(Decimal("0.001"))

    # Top line grows mildly and is scaled per borrower, so EBIT margin is a
    # figure that genuinely moves: a coverage-driven borrower's earnings fall
    # against a rising top line, which is what margin compression looks like
    # on a real statement. Revenue never feeds a covenant, so varying it
    # cannot disturb any threshold.
    revenue = (
        (_REVENUE_BASE + _REVENUE_GROWTH * period_index)
        * (Decimal("1") + _REVENUE_SPREAD * Decimal(borrower_index % 5))
    ).quantize(Decimal("0.001"))
    depreciation = (revenue * _DEPRECIATION_OF_REVENUE).quantize(Decimal("0.001"))
    # Exactly the chart's own derivation (`ebit + depreciation`), so a
    # supplied EBITDA can never disagree with its derived value.
    ebitda = ebit + depreciation
    # Cash available for debt service is EBITDA net of tax and working
    # capital, less a drag that scales with the debt stack. The drag is the
    # point: a borrower levering up watches DSCR fall even while EBITDA holds,
    # which is the mechanism the case file names when leverage is the driver.
    cash_flow_debt_service = ((ebitda * _CFDS_OF_EBITDA) - (total_debt * _CFDS_DEBT_DRAG)).quantize(
        Decimal("0.001")
    )

    return {
        "total_debt": total_debt,
        "tangible_net_worth": Decimal("100"),
        "ebit": ebit,
        "finance_cost": Decimal("10"),
        "current_assets": (liquidity * Decimal("100")).quantize(Decimal("0.001")),
        "current_liabilities": Decimal("100"),
        "revenue": revenue,
        "depreciation": depreciation,
        "ebitda": ebitda,
        "cash_flow_debt_service": cash_flow_debt_service,
    }


def _driver_for(kind: str, borrower_index: int) -> str:
    if kind == "LEV":
        if borrower_index <= 18:
            return "debt expansion / tangible net worth"
        return "debt headroom"
    if kind == "COV":
        return "EBIT / finance cost"
    return "current assets / current liabilities"


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


__all__ = ["DemoSeedReport", "seed_demo_covenants"]
