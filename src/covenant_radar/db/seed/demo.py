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
from covenant_radar.db.models.facility import Facility
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
    ("FY25Q3", date(2025, 7, 1), date(2025, 9, 30)),
    ("FY25Q4", date(2025, 10, 1), date(2025, 12, 31)),
    ("FY26Q1", date(2026, 1, 1), date(2026, 3, 31)),
    ("FY26Q2", date(2026, 4, 1), date(2026, 6, 30)),
)
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
            .limit(36)
        )
    )
    if len(borrowers) < 30:
        raise ValueError(
            f"The reference portfolio contains only {len(borrowers)} active borrowers; "
            "at least 30 are required for the demo."
        )

    batch = _ensure_import_batch(
        session, system_actor_id=system_actor_id, now=now, request_id=request_id
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
                        effective_from=date(2025, 1, 1),
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
                current_inputs = (
                    dict(result.inputs) if isinstance(result.inputs, Mapping) else {}
                )
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


def _ensure_demo_signal_source(path: Path, borrowers: Sequence[Borrower], session: Session) -> int:
    """Write deterministic seven-family events for the local demo source.

    The file is deliberately a source artifact, not a database fixture.  The
    nightly ingest step reads it through ``FileSignalSource`` and therefore
    exercises the same validation, quarantine, evidence, attribution, and
    audit path as a production connector.  Deteriorating borrowers have a
    sustained adverse run; noisy borrowers have isolated spikes; stable
    borrowers remain healthy.  Re-seeding never rewrites an existing source.
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
        profile = (
            "deteriorating"
            if borrower_index <= 12
            else "noisy"
            if borrower_index <= 24
            else "stable"
        )
        for day_offset in range(DEMO_SIGNAL_DAYS):
            event_date = start_date.fromordinal(start_date.toordinal() + day_offset)
            for family, event_type, unit, value_field in DEMO_SIGNAL_FAMILIES:
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
    if profile == "deteriorating":
        return True
    # Noise is deliberately non-adverse: the persistence stage should show
    # that frequent movement without a material adverse flag is filtered out
    # rather than creating a false supersession chain in the ledger.
    if profile == "noisy":
        return False
    return False


def _demo_signal_value(
    family: str, profile: str, day_offset: int, borrower_index: int
) -> Decimal:
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
        return value.quantize(Decimal("1" if family == "payment" else "0.001"))
    if profile == "noisy":
        spike = Decimal("4.00") if day_offset in {10, 25} else Decimal("1.00")
        value = spike + Decimal((borrower_index * 3 + day_offset) % 7) / 10
        return value.quantize(Decimal("1" if family == "payment" else "0.001"))
    value = Decimal("0.50") + Decimal((borrower_index + day_offset) % 5) / 10
    return value.quantize(Decimal("1" if family == "payment" else "0.001"))


def _ensure_import_batch(
    session: Session, *, system_actor_id: UUID, now: datetime, request_id: str
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
    batch = ImportBatch(
        id=new_id(),
        source_type="json",
        source_reference=DEMO_SOURCE_REFERENCE,
        mapping_id=mapping.id,
        content_hash=DEMO_BATCH_HASH,
        started_at=now,
        finished_at=now,
        row_count=36 * len(DEMO_PERIODS),
        accepted_count=36 * len(DEMO_PERIODS),
        quarantined_count=0,
        state="completed",
        report={"seed": "phase-7a", "rows": 36 * len(DEMO_PERIODS)},
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
                session.scalar(select(ThresholdSnapshot.version).order_by(ThresholdSnapshot.version.desc()).limit(1))
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


def _demo_lines(borrower_index: int, period_index: int) -> dict[str, Decimal]:
    """Return a coherent statement with curated leverage trajectories."""

    if borrower_index <= 3:
        leverage = Decimal("2.80") + Decimal("0.14") * period_index
    elif borrower_index <= 8:
        leverage = Decimal("1.85") + Decimal("0.35") * period_index
    elif borrower_index <= 13:
        leverage = Decimal("1.125") + Decimal("0.525") * period_index
    elif borrower_index <= 18:
        leverage = Decimal("1.25") + Decimal("0.45") * period_index
    elif borrower_index <= 25:
        leverage = Decimal("2.55") + Decimal("0.055") * period_index
    else:
        leverage = Decimal("1.55") + Decimal("0.025") * period_index
    coverage = Decimal("2.40") - Decimal("0.05") * period_index
    liquidity = Decimal("1.55") - Decimal("0.025") * period_index
    return {
        "total_debt": (leverage * Decimal("100")).quantize(Decimal("0.001")),
        "tangible_net_worth": Decimal("100"),
        "ebit": (coverage * Decimal("10")).quantize(Decimal("0.001")),
        "finance_cost": Decimal("10"),
        "current_assets": (liquidity * Decimal("100")).quantize(Decimal("0.001")),
        "current_liabilities": Decimal("100"),
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
