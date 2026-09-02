"""Seed current, pipeline-derived inputs for the 120-borrower showcase."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
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
from covenant_radar.services.master_data import MasterDataService
from covenant_radar.services.registry import RegistryService

from .manifest import Scenario, scenario_manifest

DEMO_PORTFOLIO_CODE: Final[str] = "REF-PORTFOLIO"
DEMO_SOURCE_REFERENCE: Final[str] = "evaluation/full-product-demo-v2"
DEMO_SIGNAL_SOURCE_REFERENCE: Final[str] = "evaluation/full-product-demo-signals-v2"
DEMO_SIGNAL_PATH: Final[Path] = Path("var/inbox/covenant-radar-demo-signals.json")
DEMO_SIGNAL_DAYS: Final[int] = 35
_LEGACY_INDUSTRIES: Final[tuple[str, ...]] = (
    "A01",
    "C10",
    "C24",
    "C25",
    "F41",
    "G46",
    "G47",
    "H49",
    "J62",
    "M70",
)
_SIGNAL_FAMILIES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("account_activity", "account_activity_change", "%", "activity_change_pct"),
    ("payment", "payment_delay", "days", "days_past_due"),
    ("utilisation", "facility_utilisation", "%", "utilisation_pct"),
    ("treasury", "treasury_outflow", "ratio", "cash_outflow_ratio"),
    ("concentration", "concentration_exposure", "%", "top_group_exposure_pct"),
    ("industry", "industry_indicator", "score", "industry_stress_score"),
    ("news", "news_event", "score", "news_risk_score"),
)


@dataclass(frozen=True, slots=True)
class ShowcaseInputReport:
    borrowers: int
    industries_updated: int
    protected_industries: tuple[str, ...]
    covenants_created: int
    periods_created: int
    tests_created: int
    conduct_created: int
    signal_rows: int
    threshold_snapshot_id: UUID


class _AuditWriter:
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


def seed_showcase_inputs(
    session: Session,
    *,
    system_actor_id: UUID,
    demo_day: date,
    clock: Clock | None = None,
    signal_path: Path | str = DEMO_SIGNAL_PATH,
) -> ShowcaseInputReport:
    """Create scenario inputs while leaving pipeline outputs to the pipeline."""

    if not isinstance(session, Session):
        raise TypeError("seed_showcase_inputs requires a SQLAlchemy Session.")
    if not isinstance(system_actor_id, UUID):
        raise TypeError("system_actor_id must be a UUID.")
    if not isinstance(demo_day, date):
        raise TypeError("demo_day must be a date.")
    active_clock = clock or SystemClock()
    now = active_clock.now()
    request_id = "demo-v2-" + new_request_id()[:30]
    portfolio = session.scalar(select(Portfolio).where(Portfolio.code == DEMO_PORTFOLIO_CODE))
    if portfolio is None:
        raise ValueError("The reference portfolio must exist before showcase inputs are seeded.")
    manifest = scenario_manifest()
    borrowers = list(
        session.scalars(
            select(Borrower)
            .where(
                Borrower.portfolio_id == portfolio.id,
                Borrower.reference.in_(row.borrower_reference for row in manifest),
                Borrower.is_active.is_(True),
            )
            .order_by(Borrower.reference)
        )
    )
    if len(borrowers) != len(manifest):
        raise ValueError(
            f"The showcase needs {len(manifest)} active borrowers; found {len(borrowers)}."
        )
    borrower_by_reference = {row.reference: row for row in borrowers}
    audit = _AuditWriter(
        AuditRecorder(AuditRepository(session), clock=active_clock, request_id=request_id)
    )
    principal = Principal.user(
        system_actor_id,
        (
            Permission.CORRECT_SOURCE_DATA,
            Permission.REGISTER_COVENANT,
            Permission.VIEW_COVENANT,
        ),
    )
    scope = Scope(principal_id=system_actor_id, descendant_paths=(portfolio.path,))
    master_data = MasterDataService(
        session, audit=audit, clock=active_clock, request_id=request_id
    )
    registry = RegistryService(
        session,
        audit=audit,
        clock=active_clock,
        request_id=request_id,
        maker_checker_enabled=False,
    )
    engine = EngineService(
        session,
        audit=audit,
        clock=active_clock,
        request_id=request_id,
    )
    batch = _ensure_batch(
        session,
        system_actor_id=system_actor_id,
        demo_day=demo_day,
        now=now,
        request_id=request_id,
    )
    threshold_snapshot_id = _ensure_threshold_snapshot(
        session,
        system_actor_id=system_actor_id,
        now=now,
        request_id=request_id,
    )

    industries_updated = 0
    protected_industries: list[str] = []
    covenants_created = 0
    periods_created = 0
    tests_created = 0
    conduct_created = 0
    for scenario in manifest:
        borrower = borrower_by_reference[scenario.borrower_reference]
        legacy_industry = _LEGACY_INDUSTRIES[(scenario.ordinal - 1) % len(_LEGACY_INDUSTRIES)]
        if borrower.industry_code not in {legacy_industry, scenario.industry_code}:
            protected_industries.append(borrower.reference)
        elif borrower.industry_code != scenario.industry_code:
            master_data.update_borrower(
                principal,
                borrower.reference,
                expected_version=borrower.version,
                scope=scope,
                industry_code=scenario.industry_code,
            )
            industries_updated += 1

        facilities = list(
            session.scalars(
                select(Facility)
                .where(
                    Facility.borrower_id == borrower.id,
                    Facility.effective_from <= demo_day,
                    (
                        Facility.effective_to.is_(None)
                        | (Facility.effective_to >= demo_day - timedelta(days=1))
                    ),
                )
                .order_by(Facility.reference)
            )
        )
        if not facilities:
            raise ValueError(f"Showcase borrower {borrower.reference} has no effective facility.")
        for conduct_day in (demo_day - timedelta(days=1), demo_day):
            for facility in facilities:
                existing_conduct = session.scalar(
                    select(FacilityConduct.id).where(
                        FacilityConduct.facility_id == facility.id,
                        FacilityConduct.as_of_date == conduct_day,
                    )
                )
                if existing_conduct is not None:
                    continue
                dpd = scenario.days_past_due
                if conduct_day != demo_day:
                    dpd = max(0, dpd - 5)
                outstanding = facility.outstanding or Decimal("0")
                limit = facility.sanctioned_limit
                utilisation = (
                    (outstanding / limit * Decimal("100")).quantize(Decimal("0.0001"))
                    if limit > 0
                    else None
                )
                session.add(
                    FacilityConduct(
                        id=new_id(),
                        facility_id=facility.id,
                        as_of_date=conduct_day,
                        outstanding=outstanding,
                        utilisation_pct=utilisation,
                        days_past_due=dpd,
                        overdue_amount=(
                            (outstanding * Decimal("0.03")).quantize(Decimal("0.01"))
                            if dpd > 0
                            else Decimal("0")
                        ),
                        excess_amount=Decimal("0"),
                        source_id=uuid5(
                            NAMESPACE_URL,
                            f"{DEMO_SOURCE_REFERENCE}/conduct/{facility.id}/{conduct_day}",
                        ),
                        created_at=now,
                        updated_at=now,
                        created_by_id=system_actor_id,
                        updated_by_id=system_actor_id,
                        request_id=request_id,
                    )
                )
                conduct_created += 1

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
            reference = f"D{scenario.ordinal:02d}{kind}"
            covenant = session.scalar(select(Covenant).where(Covenant.reference == reference))
            if covenant is None:
                result = registry.register(
                    principal,
                    facility_id=facilities[0].id,
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
                        effective_from=demo_day - timedelta(days=730),
                        warning_headroom_pct=Decimal("10.00"),
                        cure_days=120,
                        grace_days=0,
                    ),
                    scope=scope,
                )
                covenant = result.covenant
                covenants_created += 1
            version = session.scalar(
                select(CovenantVersion)
                .where(CovenantVersion.covenant_id == covenant.id)
                .order_by(CovenantVersion.version_no.desc())
                .limit(1)
            )
            if version is not None:
                versions[kind] = version

        for period_index, (period_start, period_end, label) in enumerate(
            _rolling_periods(demo_day)
        ):
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
                    superseded_by_id=None,
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
            lines = _scenario_lines(scenario, period_index)
            _ensure_lines(
                session,
                period=period,
                lines=lines,
                batch=batch,
                system_actor_id=system_actor_id,
                now=now,
                request_id=request_id,
            )
            for kind, version in versions.items():
                existing_test = session.scalar(
                    select(CovenantTest.id).where(
                        CovenantTest.covenant_version_id == version.id,
                        CovenantTest.period_id == period.id,
                    )
                )
                if existing_test is not None:
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
                current_inputs.update(
                    {
                        "demo_version": "v2",
                        "demo_risk_profile": scenario.risk_band,
                        "demo_forecast_story": scenario.forecast_story,
                        "demo_driver": _driver_for(kind, scenario),
                    }
                )
                result.inputs = current_inputs
                tests_created += 1
    session.flush()
    signal_rows = write_signal_source(
        Path(signal_path), borrowers, manifest, session=session, demo_day=demo_day
    )
    return ShowcaseInputReport(
        borrowers=len(borrowers),
        industries_updated=industries_updated,
        protected_industries=tuple(protected_industries),
        covenants_created=covenants_created,
        periods_created=periods_created,
        tests_created=tests_created,
        conduct_created=conduct_created,
        signal_rows=signal_rows,
        threshold_snapshot_id=threshold_snapshot_id,
    )


def write_signal_source(
    path: Path,
    borrowers: Sequence[Borrower],
    manifest: Sequence[Scenario],
    *,
    session: Session,
    demo_day: date,
) -> int:
    """Write a current, valid seven-family source file for the real ingest path."""

    borrower_by_reference = {row.reference: row for row in borrowers}
    start = demo_day - timedelta(days=DEMO_SIGNAL_DAYS - 1)
    rows: list[dict[str, object]] = []
    for scenario in manifest:
        borrower = borrower_by_reference[scenario.borrower_reference]
        facility = session.scalar(
            select(Facility)
            .where(Facility.borrower_id == borrower.id)
            .order_by(Facility.reference)
            .limit(1)
        )
        if facility is None:
            continue
        for day_offset in range(DEMO_SIGNAL_DAYS):
            event_date = start + timedelta(days=day_offset)
            for family, event_type, unit, value_field in _SIGNAL_FAMILIES:
                value = _signal_value(family, scenario.risk_band, day_offset, scenario.ordinal)
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
                            "is_adverse": scenario.risk_band == "act",
                            "profile": scenario.risk_band,
                            "demo_version": "v2",
                            "source_date": event_date.isoformat(),
                        },
                    }
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return len(rows)


def _rolling_periods(demo_day: date) -> tuple[tuple[date, date, str], ...]:
    rows: list[tuple[date, date, str]] = []
    for index in range(4):
        end = demo_day - timedelta(days=(3 - index) * 91)
        start = end - timedelta(days=90)
        rows.append((start, end, f"DV2-{demo_day:%Y%m%d}-Q{index + 1}"))
    return tuple(rows)


def _scenario_lines(scenario: Scenario, period_index: int) -> dict[str, Decimal]:
    leverage_series = {
        "watch": ("1.55", "1.56", "1.55", "1.54"),
        "amber": ("2.72", "2.76", "2.80", "2.84"),
        "act": ("2.55", "2.72", "2.91", "3.12"),
    }
    leverage = Decimal(leverage_series[scenario.risk_band][period_index])
    coverage = Decimal("2.55") - Decimal("0.01") * period_index
    liquidity = Decimal("1.84") - Decimal("0.01") * period_index
    return {
        "total_debt": leverage * Decimal("100"),
        "tangible_net_worth": Decimal("100"),
        "ebit": coverage * Decimal("10"),
        "finance_cost": Decimal("10"),
        "current_assets": liquidity * Decimal("100"),
        "current_liabilities": Decimal("100"),
    }


def _signal_value(family: str, risk_band: str, day_offset: int, ordinal: int) -> int | float:
    if risk_band == "act":
        base = {
            "account_activity": 12.0,
            "payment": 8,
            "utilisation": 82.0,
            "treasury": 0.30,
            "concentration": 52.0,
            "industry": 0.66,
            "news": 0.62,
        }[family]
        if family == "payment":
            return int(base) + day_offset // 7
        return round(float(base) + day_offset * 0.005 + (ordinal % 5) * 0.001, 3)
    base = {
        "account_activity": 0.4,
        "payment": 0,
        "utilisation": 48.0,
        "treasury": 0.05,
        "concentration": 22.0,
        "industry": 0.12,
        "news": 0.08,
    }[family]
    if family == "payment":
        return 0
    jitter = ((ordinal + day_offset) % 3) * 0.001
    return round(float(base) + jitter, 3)


def _ensure_batch(
    session: Session,
    *,
    system_actor_id: UUID,
    demo_day: date,
    now: datetime,
    request_id: str,
) -> ImportBatch:
    content_hash = hashlib.sha256(f"{DEMO_SOURCE_REFERENCE}:{demo_day}".encode()).hexdigest()
    existing = session.scalar(select(ImportBatch).where(ImportBatch.content_hash == content_hash))
    if existing is not None:
        return existing
    mapping = session.scalar(
        select(ImportMapping).where(
            ImportMapping.name == "full-product-demo-financials",
            ImportMapping.version == 2,
        )
    )
    if mapping is None:
        mapping = ImportMapping(
            id=new_id(),
            name="full-product-demo-financials",
            source_type="json",
            version=2,
            spec={"mapping_version": 2, "purpose": "Full-product synthetic demo"},
            is_active=True,
            created_at=now,
            updated_at=now,
            created_by_id=system_actor_id,
            updated_by_id=system_actor_id,
            request_id=request_id,
        )
        session.add(mapping)
        session.flush()
    batch = ImportBatch(
        id=new_id(),
        source_type="json",
        source_reference=f"{DEMO_SOURCE_REFERENCE}/{demo_day}",
        mapping_id=mapping.id,
        content_hash=content_hash,
        started_at=now,
        finished_at=now,
        row_count=120 * 4,
        accepted_count=120 * 4,
        quarantined_count=0,
        state="completed",
        report={"demo_version": "v2", "demo_day": demo_day.isoformat(), "rows": 480},
        created_at=now,
        updated_at=now,
        created_by_id=system_actor_id,
        updated_by_id=system_actor_id,
        request_id=request_id,
    )
    session.add(batch)
    session.flush()
    return batch


def _ensure_lines(
    session: Session,
    *,
    period: FinancialPeriod,
    lines: Mapping[str, Decimal],
    batch: ImportBatch,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> None:
    existing = set(
        session.scalars(
            select(StatementLineValue.line_code).where(StatementLineValue.period_id == period.id)
        )
    )
    if existing >= set(lines):
        return
    provenance = FieldProvenance(
        id=new_id(),
        source_type="json",
        source_reference=DEMO_SOURCE_REFERENCE,
        row_reference=f"{period.borrower_id}:{period.fy_label}",
        mapping_version=2,
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
        if code in existing:
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


def _ensure_threshold_snapshot(
    session: Session,
    *,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> UUID:
    existing = session.scalar(
        select(ThresholdSnapshot.id).where(
            ThresholdSnapshot.source == "calibration",
            ThresholdSnapshot.note == "Full-product demo v2 calibration",
        )
    )
    if existing is not None:
        return existing
    values = _json_safe(ThresholdStore(path=DEFAULT_THRESHOLD_PATH).values())
    if not isinstance(values, dict):
        raise RuntimeError("The threshold store did not produce an object.")
    version = session.scalar(
        select(ThresholdSnapshot.version).order_by(ThresholdSnapshot.version.desc()).limit(1)
    )
    row = ThresholdSnapshot(
        id=new_id(),
        values=values,
        source="calibration",
        effective_from=now,
        proposed_by_id=system_actor_id,
        approved_by_id=system_actor_id,
        note="Full-product demo v2 calibration",
        version=(version or 0) + 1,
        created_at=now,
        updated_at=now,
        created_by_id=system_actor_id,
        updated_by_id=system_actor_id,
        request_id=request_id,
    )
    session.add(row)
    session.flush()
    return row.id


def _driver_for(kind: str, scenario: Scenario) -> str:
    if kind == "LEV":
        return {
            "watch": "stable debt and net worth",
            "amber": "narrowing leverage headroom",
            "act": "debt growth outpacing net worth",
        }[scenario.risk_band]
    if kind == "COV":
        return "EBIT relative to finance cost"
    return "current assets relative to current liabilities"


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    return value


__all__ = [
    "DEMO_SIGNAL_PATH",
    "DEMO_SIGNAL_SOURCE_REFERENCE",
    "DEMO_SOURCE_REFERENCE",
    "ShowcaseInputReport",
    "seed_showcase_inputs",
    "write_signal_source",
]
