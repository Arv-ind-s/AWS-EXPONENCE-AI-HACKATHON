"""Integration coverage for T-056's run and forecast persistence contract."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.audit import AuditRecorder, InMemoryAuditStore
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.forecast import Forecast, ForecastPath, ForecastRun
from covenant_radar.domain.forecast import (
    FeatureContribution,
    FeatureSnapshot,
    Observation,
    Prediction,
    Weights,
)
from covenant_radar.services.scoring import (
    CHAMPION_PREDICTOR_MODE,
    SHADOW_PREDICTOR_MODE,
    ForecastCandidate,
    ForecastScoringService,
)

pytestmark = pytest.mark.integration

_AS_OF = date(2026, 1, 15)
_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_REQUEST_ID = "rq-t056-forecast-persistence"


class _ThresholdStore:
    def __init__(self) -> None:
        self.id = uuid4()

    def snapshot_id(self) -> UUID:
        return self.id

    def get(self, name: str) -> dict[str, Decimal]:
        if name != "T2":
            raise KeyError(name)
        return {"confidence_floor": Decimal("0.50")}


class _Predictor:
    def predict(self, snapshot: FeatureSnapshot, *, horizon_days: int) -> Prediction:
        assert snapshot.values
        assert horizon_days > 0
        return Prediction(
            probability=Decimal("0.9100"),
            model_version="stage4:test:v1",
            artifact_checksum="f" * 64,
            contributions=(FeatureContribution("evidence_pressure", Decimal("0.42")),),
        )


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def thresholds() -> _ThresholdStore:
    return _ThresholdStore()


def _service(session: Session, thresholds: _ThresholdStore) -> ForecastScoringService:
    return ForecastScoringService(
        session,
        audit=AuditRecorder(InMemoryAuditStore(), clock=FixedClock(_NOW)),
        threshold_store=thresholds,
        weights=Weights(Decimal("1"), Decimal("1"), Decimal("1")),
        clock=FixedClock(_NOW),
        request_id=_REQUEST_ID,
    )


def _candidate(*, computable: bool = True, reason: str | None = None) -> ForecastCandidate:
    return ForecastCandidate(
        covenant_version_id=uuid4(),
        threshold=Decimal("100"),
        direction="max",
        series=(
            Observation(date=date(2026, 1, 5), value=Decimal("60")),
            Observation(date=_AS_OF, value=Decimal("65")),
        ),
        pressure=Decimal("0.20"),
        completeness=Decimal("1"),
        evidence_support=Decimal("1"),
        data_as_of=_AS_OF - timedelta(days=1),
        computable=computable,
        not_computable_reason=reason,
    )


def test_run_carries_snapshot_and_versions(
    db_session: Session, thresholds: _ThresholdStore
) -> None:
    candidate = _candidate()

    result = _service(db_session, thresholds).score(
        [candidate],
        as_of_date=_AS_OF,
        horizons=[7, 30],
        model_version="forecast.test.v1",
        rule_versions={"path": "forecast.path.v1", "mapping": "forecast.probability.v1"},
    )

    assert result.run.state == "complete"
    assert result.run.as_of_date == _AS_OF
    assert result.run.threshold_snapshot_id == thresholds.id
    assert result.run.model_version == "forecast.test.v1"
    assert result.run.covenant_count == 1
    assert result.run.finished_at is not None
    assert result.forecasts[0].formula_inputs["rule_versions"] == {
        "mapping": "forecast.probability.v1",
        "path": "forecast.path.v1",
    }


def test_interrupted_run_incomplete_and_resumable(
    db_session: Session, thresholds: _ThresholdStore
) -> None:
    candidates = [_candidate(), _candidate()]
    service = _service(db_session, thresholds)

    interrupted = service.score(
        candidates,
        as_of_date=_AS_OF,
        horizons=[7, 30],
        interrupt_after=1,
    )
    assert interrupted.run.state == "incomplete"
    assert interrupted.run.finished_at is not None
    assert len(interrupted.forecasts) == 2
    assert len(interrupted.paths) == 31
    db_session.commit()

    resumed = service.score(
        candidates,
        as_of_date=_AS_OF,
        horizons=[7, 30],
        run_id=interrupted.run_id,
    )

    assert resumed.resumed is True
    assert resumed.run.state == "complete"
    assert resumed.run.covenant_count == 2
    assert len(resumed.forecasts) == 4
    assert len(resumed.paths) == 62
    assert len(db_session.scalars(select(ForecastRun)).all()) == 1


def test_rerun_identical_by_content_hash(db_session: Session, thresholds: _ThresholdStore) -> None:
    candidate = _candidate()
    service = _service(db_session, thresholds)
    first = service.score([candidate], as_of_date=_AS_OF, horizons=[3, 7])
    db_session.commit()

    second = service.score(
        [candidate],
        as_of_date=_AS_OF,
        horizons=[3, 7],
        run_id=first.run_id,
    )

    assert second.content_hash == first.content_hash
    assert db_session.scalar(select(ForecastRun).where(ForecastRun.id == first.run_id)) is not None
    assert len(db_session.scalars(select(Forecast)).all()) == 2
    assert len(db_session.scalars(select(ForecastPath)).all()) == 8


def test_uncomputable_covenant_written_with_reason(
    db_session: Session, thresholds: _ThresholdStore
) -> None:
    candidate = _candidate(computable=False, reason="required financial period is missing")

    result = _service(db_session, thresholds).score(
        [candidate],
        as_of_date=_AS_OF,
        horizons=[7, 30],
    )

    rows = [
        row for row in result.forecasts if row.covenant_version_id == candidate.covenant_version_id
    ]
    assert len(rows) == 2
    assert all(row.probability is None for row in rows)
    assert all(row.confidence == Decimal("0.0000") for row in rows)
    assert all(
        row.formula_inputs["not_computable_reason"] == "required financial period is missing"
        for row in rows
    )
    assert all(
        point.projected_value is None
        for point in result.paths
        if point.covenant_version_id == candidate.covenant_version_id
    )


def test_no_probability_without_a_record(db_session: Session, thresholds: _ThresholdStore) -> None:
    result = _service(db_session, thresholds).score(
        [_candidate(), _candidate(computable=False, reason="missing ratio inputs")],
        as_of_date=_AS_OF,
        horizons=[7],
    )
    stored = {
        (row.covenant_version_id, row.horizon_days): row.probability
        for row in db_session.scalars(select(Forecast)).all()
    }
    surface = {
        (row.covenant_version_id, row.horizon_days): row.probability
        for row in result.forecasts
        if row.probability is not None
    }

    assert surface
    assert all(key in stored and stored[key] == value for key, value in surface.items())
    expected = tuple(row.probability for row in result.forecasts if row.probability is not None)
    assert result.probabilities == expected


def test_horizons_read_from_configuration(db_session: Session, thresholds: _ThresholdStore) -> None:
    service = ForecastScoringService(
        db_session,
        audit=AuditRecorder(InMemoryAuditStore(), clock=FixedClock(_NOW)),
        threshold_store=thresholds,
        clock=FixedClock(_NOW),
        request_id=_REQUEST_ID,
    )

    result = service.score(
        [_candidate()],
        as_of_date=_AS_OF,
        configuration={
            "forecast": {"horizons": [3, 11]},
            "probability": {
                "distance": Decimal("1"),
                "velocity": Decimal("1"),
                "pressure": Decimal("1"),
            },
        },
    )

    assert {row.horizon_days for row in result.forecasts} == {3, 11}
    assert {row.day_offset for row in result.paths} == set(range(12))


def test_ml_challenger_is_persisted_but_does_not_replace_shadow_probability(
    db_session: Session,
    thresholds: _ThresholdStore,
) -> None:
    result = ForecastScoringService(
        db_session,
        audit=AuditRecorder(InMemoryAuditStore(), clock=FixedClock(_NOW)),
        threshold_store=thresholds,
        weights=Weights(Decimal("1"), Decimal("1"), Decimal("1")),
        clock=FixedClock(_NOW),
        request_id=_REQUEST_ID,
        predictor=_Predictor(),
        predictor_mode=SHADOW_PREDICTOR_MODE,
    ).score([_candidate()], as_of_date=_AS_OF, horizons=[30])

    row = result.forecasts[0]
    assert row.probability != Decimal("0.9100")
    assert row.probability_source == "deterministic"
    assert row.fallback_reason == (
        "ML challenger runs in shadow mode; deterministic probability retained"
    )
    assert row.formula_inputs["challenger_probability"] == "0.9100"
    assert row.formula_inputs["ml_prediction"] == {
        "model_version": "stage4:test:v1",
        "artifact_checksum": "f" * 64,
        "probability": "0.9100",
        "contributions": [{"name": "evidence_pressure", "value": "0.42"}],
    }


def test_approved_champion_mode_marks_ml_as_operational_source(
    db_session: Session,
    thresholds: _ThresholdStore,
) -> None:
    result = ForecastScoringService(
        db_session,
        audit=AuditRecorder(InMemoryAuditStore(), clock=FixedClock(_NOW)),
        threshold_store=thresholds,
        weights=Weights(Decimal("1"), Decimal("1"), Decimal("1")),
        clock=FixedClock(_NOW),
        request_id=_REQUEST_ID,
        predictor=_Predictor(),
        predictor_mode=CHAMPION_PREDICTOR_MODE,
    ).score([_candidate()], as_of_date=_AS_OF, horizons=[30])

    row = result.forecasts[0]
    assert row.probability == Decimal("0.9100")
    assert row.probability_source == "ml"
    assert row.fallback_reason is None
    assert row.formula_inputs["predictor_mode"] == "champion"
