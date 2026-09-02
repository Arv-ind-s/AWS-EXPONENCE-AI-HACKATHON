"""Integration coverage for T-064 simulation persistence."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastRun, Intervention
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Memo
from covenant_radar.db.repositories.simulation import SimulationRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.forecast import Observation, Weights, project
from covenant_radar.domain.interventions import InterventionFacts, LevelShiftEffect
from covenant_radar.domain.interventions.simulate import SimulationResult
from covenant_radar.services.simulation import SimulationService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_REQUEST_ID = "rq-t064-simulation-persistence"
_WEIGHTS = Weights(Decimal("1"), Decimal("1"), Decimal("1"))


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object, str]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, dict(payload), actor, request_id))
        return object()


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _fixture(session: Session) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    user_id = uuid4()
    portfolio = Portfolio.create(
        code="T064",
        name="T064 portfolio",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t064-portfolio",
    )
    borrower = Borrower(
        id=new_id(),
        reference="B-T064",
        legal_name="T064 Borrower Private Limited",
        portfolio_id=portfolio.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t064-borrower",
    )
    facility = Facility(
        id=new_id(),
        reference="F-T064",
        borrower_id=borrower.id,
        facility_type="term_loan",
        sanctioned_limit=Decimal("1000000"),
        currency="INR",
        sanction_date=date(2025, 1, 1),
        effective_from=date(2025, 1, 1),
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t064-facility",
    )
    covenant = Covenant(
        id=new_id(),
        reference="C-T064",
        facility_id=facility.id,
        name="Debt service coverage",
        covenant_class="leverage",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t064-covenant",
    )
    version = CovenantVersion(
        id=new_id(),
        covenant_id=covenant.id,
        version_no=1,
        threshold=Decimal("100"),
        direction="max",
        unit="ratio",
        frequency="quarterly",
        test_basis="reported",
        effective_from=date(2025, 1, 1),
        status="live",
        tested_at_least_once=False,
        registered_by_id=user_id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t064-version",
    )
    intervention = Intervention(
        id=new_id(),
        code="LEVEL",
        role_tag="credit",
        text="Reduce the balance",
        effect_model="level_shift",
        effect_parameters={"amount": "-2"},
        applicable_covenant_classes=["leverage"],
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t064-intervention",
    )
    user = AppUser(
        id=user_id,
        username="t064-user",
        email="t064@example.com",
        full_name="T064 User",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t064-user",
    )
    session.add_all((user, portfolio, borrower, facility, covenant, version, intervention))
    session.flush()
    return user_id, portfolio.id, version.id, intervention.id, borrower.id


def _forecast(
    session: Session,
    covenant_version_id: UUID,
    *,
    started_at: datetime,
) -> tuple[ForecastRun, Forecast]:
    run = ForecastRun(
        id=new_id(),
        as_of_date=started_at.date(),
        threshold_snapshot_id=None,
        model_version="forecast.t064.v1",
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=1),
        covenant_count=1,
        state="complete",
        created_at=started_at,
        updated_at=started_at,
        request_id=f"rq-t064-run-{started_at.day}",
    )
    forecast = Forecast(
        id=new_id(),
        run_id=run.id,
        covenant_version_id=covenant_version_id,
        horizon_days=30,
        probability=Decimal("0.5000"),
        confidence=Decimal("0.9000"),
        below_confidence_floor=False,
        projected_cross_date=date(2026, 9, 10),
        direction="max",
        formula_inputs={},
        data_as_of=started_at.date(),
        staleness_days=0,
        created_at=started_at,
        updated_at=started_at,
        request_id=f"rq-t064-forecast-{started_at.day}",
    )
    session.add_all((run, forecast))
    session.flush()
    return run, forecast


def _result() -> SimulationResult:
    projection = project(
        (
            Observation(observed_on=date(2026, 8, 1), value=Decimal("80"), source_id="obs-1"),
            Observation(observed_on=date(2026, 8, 2), value=Decimal("85"), source_id="obs-2"),
        ),
        pressure=Decimal("0.20"),
        horizon_days=30,
        threshold=Decimal("100"),
        direction="max",
    )
    intervention = InterventionFacts(
        code="LEVEL",
        effect=LevelShiftEffect(
            amount=Decimal("-2"),
            assumptions=("the approved balance reduction takes effect immediately",),
            applicable_covenant_classes=frozenset({"leverage"}),
        ),
        text="Reduce the balance",
    )
    return SimulationService().simulate(
        projection,
        intervention,
        {"covenant_class": "leverage", "weights": _WEIGHTS},
    )


def _scope(user_id: UUID, portfolio_id: UUID) -> Scope:
    return Scope.from_paths(user_id, [f"{portfolio_id.hex}/"])


def test_retrievable_after_run_superseded_and_marked(db_session: Session) -> None:
    user_id, portfolio_id, covenant_version_id, intervention_id, _ = _fixture(db_session)
    _, first_forecast = _forecast(
        db_session,
        covenant_version_id,
        started_at=_NOW,
    )
    scope = _scope(user_id, portfolio_id)
    repository = SimulationRepository(db_session)
    first = repository.save_result(
        _result(),
        forecast_id=first_forecast.id,
        intervention_id=intervention_id,
        scope=scope,
        occurred_at=_NOW,
        request_id=_REQUEST_ID,
        created_by_id=user_id,
    )
    db_session.commit()

    _forecast(
        db_session,
        covenant_version_id,
        started_at=_NOW + timedelta(days=1),
    )
    retrieved = repository.get(first.id, scope=scope)

    assert retrieved is not None
    assert retrieved.id == first.id
    assert getattr(retrieved, "based_on_superseded_run", None) is True
    lineage = repository.lineage(first.id, scope=scope)
    assert lineage is not None
    assert lineage.based_on_superseded_run is True
    assert isinstance(retrieved.assumptions, Mapping)
    assert retrieved.assumptions["assumptions"]
    result_payload = retrieved.assumptions["result"]
    assert isinstance(result_payload, Mapping)
    assert result_payload["content_hash"]


def test_rerun_links_not_overwrites(db_session: Session) -> None:
    user_id, portfolio_id, covenant_version_id, intervention_id, _ = _fixture(db_session)
    _, first_forecast = _forecast(db_session, covenant_version_id, started_at=_NOW)
    scope = _scope(user_id, portfolio_id)
    repository = SimulationRepository(db_session)
    result = _result()
    first = repository.save_result(
        result,
        forecast_id=first_forecast.id,
        intervention_id=intervention_id,
        scope=scope,
        occurred_at=_NOW,
        request_id=_REQUEST_ID,
        created_by_id=user_id,
    )
    second_run, second_forecast = _forecast(
        db_session,
        covenant_version_id,
        started_at=_NOW + timedelta(days=1),
    )
    second = repository.save_with_status(
        result,
        forecast_id=second_forecast.id,
        intervention_id=intervention_id,
        scope=scope,
        occurred_at=second_run.started_at,
        request_id=_REQUEST_ID,
        created_by_id=user_id,
    )

    assert second.created is True
    assert second.simulation.id != first.id
    assert second.supersedes_simulation_id == first.id
    assert first.parameters.get("_supersedes_simulation_id") is None
    assert second.simulation.parameters["_supersedes_simulation_id"] == str(first.id)
    assert db_session.scalars(select(type(first))).all() == [first, second.simulation]

    first_lineage = repository.lineage(first.id, scope=scope)
    assert first_lineage is not None
    assert first_lineage.superseded_by_simulation_id == second.simulation.id


def test_retired_intervention_still_resolves(db_session: Session) -> None:
    user_id, portfolio_id, covenant_version_id, intervention_id, _ = _fixture(db_session)
    _, forecast = _forecast(db_session, covenant_version_id, started_at=_NOW)
    intervention = db_session.get(Intervention, intervention_id)
    assert intervention is not None
    intervention.is_active = False
    intervention.retired_at = _NOW
    db_session.flush()

    row = SimulationRepository(db_session).save_result(
        _result(),
        forecast_id=forecast.id,
        intervention_id=intervention_id,
        scope=_scope(user_id, portfolio_id),
        occurred_at=_NOW,
        request_id=_REQUEST_ID,
        created_by_id=user_id,
    )

    assert db_session.get(Intervention, row.intervention_id) is intervention
    assert intervention.is_active is False


def test_memo_referenced_simulation_not_purgeable(db_session: Session) -> None:
    user_id, portfolio_id, covenant_version_id, intervention_id, borrower_id = _fixture(db_session)
    _, forecast = _forecast(db_session, covenant_version_id, started_at=_NOW)
    scope = _scope(user_id, portfolio_id)
    row = SimulationRepository(db_session).save_result(
        _result(),
        forecast_id=forecast.id,
        intervention_id=intervention_id,
        scope=scope,
        occurred_at=_NOW,
        request_id=_REQUEST_ID,
        created_by_id=user_id,
    )
    memo = Memo(
        borrower_id=borrower_id,
        template_version="memo.t064.v1",
        slots={},
        drafted_text="Simulation cited by this memo.",
        simulations={"simulation_ids": [str(row.id)]},
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t064-memo",
    )
    db_session.add(memo)
    db_session.flush()

    reference = SimulationRepository(db_session).retention_reference(row.id, scope=scope)

    assert reference.memo_ids == (memo.id,)
    assert reference.reason == "simulation is referenced by a retained memo"
    assert reference.purgeable is False
    assert SimulationRepository(db_session).can_purge(row.id, scope=scope) is False
    assert SimulationRepository(db_session).can_purge(uuid4(), scope=scope) is False


def test_creation_audited(db_session: Session) -> None:
    user_id, portfolio_id, covenant_version_id, intervention_id, _ = _fixture(db_session)
    _, forecast = _forecast(db_session, covenant_version_id, started_at=_NOW)
    audit = _RecordingAudit()
    service = SimulationService(
        db_session,
        audit=audit,
        clock=FixedClock(_NOW),
        request_id=_REQUEST_ID,
    )

    row = service.simulate_and_persist(
        project(
            (
                Observation(observed_on=date(2026, 8, 1), value=Decimal("80"), source_id="obs-1"),
                Observation(observed_on=date(2026, 8, 2), value=Decimal("85"), source_id="obs-2"),
            ),
            pressure=Decimal("0.20"),
            horizon_days=30,
            threshold=Decimal("100"),
            direction="max",
        ),
        InterventionFacts(
            code="LEVEL",
            effect=LevelShiftEffect(
                amount=Decimal("-2"),
                assumptions=("the approved balance reduction takes effect immediately",),
                applicable_covenant_classes=frozenset({"leverage"}),
            ),
        ),
        {"covenant_class": "leverage", "weights": _WEIGHTS},
        forecast_id=forecast.id,
        intervention_id=intervention_id,
        scope=_scope(user_id, portfolio_id),
        created_by_id=user_id,
    )

    assert row.created_by_id == user_id
    assert len(audit.events) == 1
    event_type, subject, payload, actor, request_id = audit.events[0]
    assert event_type == "simulation_created"
    assert subject == ("simulation", row.id)
    assert payload["forecast_id"] == str(forecast.id)
    assert actor == user_id
    assert request_id == _REQUEST_ID
