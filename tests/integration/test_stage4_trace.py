"""Integration coverage for T-058's stage-4 attribution and trace writes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.audit import AuditRecorder, InMemoryAuditStore
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import TraceRow
from covenant_radar.db.models.forecast import ForecastDriver
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.domain.forecast import Observation, evidence_pressure
from covenant_radar.services.scoring import ForecastCandidate, ForecastScoringService, ScoringResult

pytestmark = pytest.mark.integration

_AS_OF = date(2026, 1, 15)
_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


class _ThresholdStore:
    def __init__(self, *, t5: Decimal = Decimal("0.10")) -> None:
        self.id = uuid4()
        self.t5 = t5

    def snapshot_id(self) -> UUID:
        return self.id

    def get(self, name: str) -> dict[str, Decimal]:
        values: dict[str, dict[str, Decimal]] = {
            "T1": {"act": Decimal("0.70"), "amber": Decimal("0.40")},
            "T2": {"confidence_floor": Decimal("0.50")},
            "T5": {"contribution_share": self.t5},
        }
        if name not in values:
            raise KeyError(name)
        return values[name]


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def evidence(db_session: Session) -> EvidenceItem:
    item = EvidenceItem(
        id=uuid4(),
        borrower_id=uuid4(),
        facility_id=None,
        family="payment",
        evidence_type="payment_delay",
        first_seen=_AS_OF,
        last_seen=_AS_OF,
        persistence_days=14,
        event_count_window=3,
        materiality_pct=Decimal("20"),
        decay_factor=Decimal("0.80"),
        state="sustained",
        counts_toward_pressure=True,
        source_event_ids=["event-1"],
        last_scored_at=_NOW,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        created_by_id=None,
        updated_by_id=None,
        request_id="rq-t058-evidence",
    )
    db_session.add(item)
    db_session.flush()
    return item


def _service(session: Session, thresholds: _ThresholdStore) -> ForecastScoringService:
    return ForecastScoringService(
        session,
        audit=AuditRecorder(InMemoryAuditStore(), clock=FixedClock(_NOW)),
        threshold_store=thresholds,
        weights={"distance": Decimal("1"), "velocity": Decimal("1"), "pressure": Decimal("1")},
        clock=FixedClock(_NOW),
        request_id="rq-t058-stage4",
    )


def _candidate(
    *,
    pressure: object = Decimal("0"),
    completeness: Decimal = Decimal("1"),
) -> ForecastCandidate:
    return ForecastCandidate(
        covenant_version_id=uuid4(),
        threshold=Decimal("100"),
        direction="max",
        series=(
            Observation(date=date(2026, 1, 5), value=Decimal("60")),
            Observation(date=_AS_OF, value=Decimal("65")),
        ),
        pressure=pressure,
        completeness=completeness,
        evidence_support=Decimal("1"),
        data_as_of=_AS_OF,
    )


def _score(
    session: Session,
    candidate: ForecastCandidate,
    *,
    thresholds: _ThresholdStore | None = None,
) -> tuple[ForecastCandidate, ScoringResult]:
    effective_thresholds = thresholds or _ThresholdStore()
    result = _service(session, effective_thresholds).score(
        [candidate],
        as_of_date=_AS_OF,
        horizons=[30],
    )
    return candidate, result


def _trace_for(session: Session, forecast_id: UUID) -> TraceRow:
    row = session.scalar(
        select(TraceRow).where(
            TraceRow.subject_type == "forecast",
            TraceRow.subject_id == forecast_id,
            TraceRow.stage == "4",
        )
    )
    assert row is not None
    return row


def test_driver_links_resolve_to_evidence(db_session: Session, evidence: EvidenceItem) -> None:
    pressure = evidence_pressure([evidence], "max")
    _candidate_value, result = _score(db_session, _candidate(pressure=pressure))

    drivers = db_session.scalars(
        select(ForecastDriver).where(ForecastDriver.forecast_id == result.forecasts[0].id)
    ).all()
    linked = [driver for driver in drivers if driver.evidence_id is not None]

    assert linked
    assert linked[0].evidence_id == evidence.id
    assert db_session.get(EvidenceItem, linked[0].evidence_id) is evidence


def test_trend_driver_has_typed_null_link(db_session: Session) -> None:
    _candidate_value, result = _score(db_session, _candidate())

    drivers = db_session.scalars(
        select(ForecastDriver).where(ForecastDriver.forecast_id == result.forecasts[0].id)
    ).all()
    trace = _trace_for(db_session, result.forecasts[0].id)
    trend = next(driver for driver in drivers if driver.name == "trend")
    trace_driver = next(driver for driver in trace.outputs["drivers"] if driver["name"] == "trend")

    assert trend.evidence_id is None
    assert trace_driver["type"] == "trend"
    assert trace_driver["link_status"] == "not_traceable"


def test_other_row_flagged(db_session: Session) -> None:
    thresholds = _ThresholdStore(t5=Decimal("1"))
    _candidate_value, result = _score(
        db_session,
        _candidate(pressure=Decimal("0.50")),
        thresholds=thresholds,
    )

    other = db_session.scalar(
        select(ForecastDriver).where(
            ForecastDriver.forecast_id == result.forecasts[0].id,
            ForecastDriver.name == "other",
        )
    )

    assert other is not None
    assert other.is_other is True


def test_suppressed_forecast_still_traced_with_reason(db_session: Session) -> None:
    _candidate_value, result = _score(
        db_session,
        _candidate(completeness=Decimal("0.40")),
    )
    forecast = result.forecasts[0]
    trace = _trace_for(db_session, forecast.id)

    assert forecast.probability is None
    assert trace.outputs["probability_suppressed"] is True
    assert "T2" in str(trace.outputs["reason"])


def test_trace_names_t1_t2_with_sides(db_session: Session) -> None:
    _candidate_value, result = _score(db_session, _candidate())
    trace = _trace_for(db_session, result.forecasts[0].id)
    comparisons = {item["name"]: item for item in trace.thresholds_compared}

    assert set(comparisons) == {"T1", "T2"}
    assert comparisons["T1"]["side"] in {"above", "below", "at"}
    assert comparisons["T2"]["side"] == "above"


def test_rule_version_stamped(db_session: Session) -> None:
    _candidate_value, result = _score(db_session, _candidate())
    trace = _trace_for(db_session, result.forecasts[0].id)

    assert trace.rule_or_prompt_version == "forecast.trend_pressure.v1"
