"""Integration coverage for `T-121`: nightly pipeline composition and
idempotent re-run (`plan.md §5.9`, `spec §R-28.a`/`R-28.c`).

Uses a private, file-based SQLite database (real background threads need more
than one connection), the same pattern `tests/integration/test_scheduler.py`
(`T-120`) already established for self-contained scheduler/ledger coverage on
a laptop with no live PostgreSQL instance.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    Case,
    Covenant,
    CovenantTest,
    CovenantVersion,
    Facility,
    Notification,
    Portfolio,
    RatioDefinition,
)
from covenant_radar.db.models.forecast import ForecastRun
from covenant_radar.db.models.forecast import TriageEntry as TriageEntryModel
from covenant_radar.db.models.operations import JobRun
from covenant_radar.domain.forecast import Weights
from covenant_radar.scheduler.jobs import JobRegistry
from covenant_radar.scheduler.pipeline import (
    PIPELINE_STEPS,
    STEP_DISPATCH,
    STEP_RANK,
    STEP_SCORE,
    STEP_TEST,
    default_step_policy,
    register_nightly_pipeline,
    run_nightly_pipeline,
)
from covenant_radar.scheduler.runner import JobRunner
from covenant_radar.services.nightly import NightlyPipelineService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)
_TODAY = date(2026, 8, 31)
_YESTERDAY = date(2026, 8, 30)
_WEIGHTS = Weights(distance=Decimal("1"), velocity=Decimal("1"), pressure=Decimal("1"))


class _FakeThresholdStore:
    """A minimal `services.nightly.ThresholdSnapshotProvider` test double
    with a mutable snapshot id, standing in for a database-backed
    `config.thresholds.ThresholdRepository` — nothing in this codebase
    implements one yet (see `services/nightly.py`'s module docstring)."""

    def __init__(self, snapshot_id: UUID) -> None:
        self._values: dict[str, Mapping[str, object]] = {
            "T1": {"act": Decimal("0.60"), "amber": Decimal("0.30")},
            "T2": {"confidence_floor": Decimal("0.10")},
            "T5": {"contribution_share": Decimal("0.10")},
        }
        self._snapshot_id = snapshot_id

    def get(self, name: str) -> Mapping[str, object]:
        return self._values[name]

    def snapshot_id(self) -> UUID:
        return self._snapshot_id

    def set_snapshot_id(self, value: UUID) -> None:
        self._snapshot_id = value


def _lines_provider(
    values: Mapping[UUID, Decimal],
) -> Callable[[CovenantVersion, date], Mapping[str, Decimal] | None]:
    """A `StatementLinesProvider` giving `leverage_ratio` a value equal to
    `values[version.id]` (tangible_net_worth pinned at 1), or `None` — "no
    data yet" — for a covenant version not listed."""

    def provider(version: CovenantVersion, _as_of_date: date) -> Mapping[str, Decimal] | None:
        if version.id not in values:
            return None
        return {"total_debt": values[version.id], "tangible_net_worth": Decimal("1")}

    return provider


def _counting_lines_provider(
    values: Mapping[UUID, Decimal], calls: list[UUID]
) -> Callable[[CovenantVersion, date], Mapping[str, Decimal] | None]:
    inner = _lines_provider(values)

    def provider(version: CovenantVersion, as_of_date: date) -> Mapping[str, Decimal] | None:
        calls.append(version.id)
        return inner(version, as_of_date)

    return provider


class _Fixture:
    """Shared database, model builders and pipeline wiring for one test."""

    def __init__(self, tmp_path: Path) -> None:
        database_path = tmp_path / "nightly.db"
        self.engine: Engine = create_engine(
            f"sqlite:///{database_path}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        Base.metadata.create_all(self.engine)
        self.session_factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        self.system_actor_id = uuid4()
        self.registered_by_id = uuid4()
        with self.session_factory() as session:
            session.add(
                AppUser(
                    id=self.registered_by_id,
                    username="system",
                    email="system@example.com",
                    full_name="System",
                    auth_source="local",
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-fixture-user",
                )
            )
            session.add(
                RatioDefinition(
                    id=uuid4(),
                    code="leverage_ratio",
                    name="Leverage ratio",
                    formula_text="total_debt / tangible_net_worth",
                    required_lines=["total_debt", "tangible_net_worth"],
                    unit="x",
                    plausible_min=Decimal("0"),
                    plausible_max=Decimal("6"),
                    direction_hint="max",
                    taxonomy_version="v1",
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-fixture-ratio-definition",
                )
            )
            session.commit()

    def close(self) -> None:
        self.engine.dispose()

    def portfolio(self, code: str) -> Portfolio:
        with self.session_factory() as session:
            portfolio = Portfolio.create(
                code=code,
                name=f"Portfolio {code}",
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-portfolio-{code.lower()}",
            )
            session.add(portfolio)
            session.commit()
            return portfolio

    def borrower(self, portfolio: Portfolio, reference: str) -> Borrower:
        with self.session_factory() as session:
            borrower = Borrower(
                id=uuid4(),
                reference=reference,
                legal_name=f"Legal {reference}",
                portfolio_id=portfolio.id,
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-borrower-{reference.lower()}",
            )
            session.add(borrower)
            session.commit()
            return borrower

    def covenant_version(
        self,
        borrower: Borrower,
        *,
        threshold: Decimal = Decimal("3"),
        direction: str = "max",
    ) -> CovenantVersion:
        with self.session_factory() as session:
            facility = Facility(
                id=uuid4(),
                reference=f"F-{borrower.reference}",
                borrower_id=borrower.id,
                facility_type="term_loan",
                sanctioned_limit=Decimal("1000000"),
                currency="INR",
                outstanding=Decimal("900000"),
                sanction_date=date(2024, 1, 1),
                effective_from=date(2024, 1, 1),
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-facility-{borrower.reference.lower()}",
            )
            session.add(facility)
            session.flush()
            covenant = Covenant(
                id=uuid4(),
                reference=f"CV-{borrower.reference}",
                facility_id=facility.id,
                name=f"Leverage covenant {borrower.reference}",
                covenant_class="financial",
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-covenant-{borrower.reference.lower()}",
            )
            session.add(covenant)
            session.flush()
            version = CovenantVersion(
                id=uuid4(),
                covenant_id=covenant.id,
                version_no=1,
                definition_ref="leverage_ratio",
                threshold=threshold,
                direction=direction,
                unit="x",
                frequency="quarterly",
                test_basis="quarterly",
                effective_from=date(2024, 1, 1),
                status="live",
                registered_by_id=self.registered_by_id,
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-covenant-version-{borrower.reference.lower()}",
            )
            session.add(version)
            session.commit()
            return version

    def seed_test(
        self,
        version: CovenantVersion,
        *,
        as_of_date: date,
        value: Decimal,
        verdict: str = "pass",
    ) -> None:
        with self.session_factory() as session:
            session.add(
                CovenantTest(
                    id=uuid4(),
                    covenant_version_id=version.id,
                    as_of_date=as_of_date,
                    value=value,
                    threshold_used=version.threshold,
                    verdict=verdict,
                    computed_at=datetime.combine(as_of_date, datetime.min.time(), tzinfo=UTC),
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=f"rq-seed-test-{version.id}-{as_of_date.isoformat()}",
                )
            )
            session.commit()

    def seed_complete_run(self, borrower: Borrower, *, as_of_date: date, band: str) -> ForecastRun:
        """A prior day's already-complete run — "the prior day's results
        still serving the queue" that a later halted run must never touch."""

        with self.session_factory() as session:
            run = ForecastRun(
                id=uuid4(),
                as_of_date=as_of_date,
                started_at=_NOW,
                finished_at=_NOW,
                covenant_count=0,
                state="complete",
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-prior-run-{as_of_date.isoformat()}",
            )
            session.add(run)
            session.flush()
            session.add(
                TriageEntryModel(
                    id=uuid4(),
                    run_id=run.id,
                    borrower_id=borrower.id,
                    probability=Decimal("0.50"),
                    confidence=Decimal("0.80"),
                    exposure=Decimal("900000"),
                    urgency=Decimal("0.40"),
                    band=band,
                    rank=1,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=f"rq-prior-entry-{borrower.id}",
                )
            )
            session.commit()
            return run

    def build_service(self, **overrides: Any) -> NightlyPipelineService:
        kwargs: dict[str, Any] = {
            "session_factory": self.session_factory,
            "threshold_store": _FakeThresholdStore(uuid4()),
            "horizons": (30, 90),
            "weights": _WEIGHTS,
            "system_actor_id": self.system_actor_id,
            "clock": FixedClock(_NOW),
        }
        kwargs.update(overrides)
        return NightlyPipelineService(**kwargs)

    def build_runner(self, service: NightlyPipelineService, *, max_attempts: int = 1) -> JobRunner:
        registry = JobRegistry()
        register_nightly_pipeline(
            registry, service.handlers(), policy=default_step_policy(max_attempts=max_attempts)
        )
        return JobRunner(registry, self.session_factory, clock=service.clock)

    def job_runs(self, run_id: str, job_name: str) -> list[JobRun]:
        with self.session_factory() as session:
            statement = select(JobRun).where(JobRun.run_id == run_id, JobRun.job_name == job_name)
            return list(session.execute(statement).scalars().all())

    def covenant_tests(self, version_id: UUID) -> list[CovenantTest]:
        with self.session_factory() as session:
            statement = select(CovenantTest).where(CovenantTest.covenant_version_id == version_id)
            return list(session.execute(statement).scalars().all())

    def forecast_run(self, run_id: UUID) -> ForecastRun:
        with self.session_factory() as session:
            run = session.get(ForecastRun, run_id)
            assert run is not None
            return run

    def triage_entries(self, run_id: UUID) -> list[TriageEntryModel]:
        with self.session_factory() as session:
            statement = select(TriageEntryModel).where(TriageEntryModel.run_id == run_id)
            return list(session.execute(statement).scalars().all())

    def cases(self, borrower_id: UUID | None = None) -> list[Case]:
        with self.session_factory() as session:
            statement = select(Case)
            if borrower_id is not None:
                statement = statement.where(Case.borrower_id == borrower_id)
            return list(session.execute(statement).scalars().all())

    def notifications(self) -> list[Notification]:
        with self.session_factory() as session:
            return list(session.execute(select(Notification)).scalars().all())


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[_Fixture]:
    built = _Fixture(tmp_path)
    try:
        yield built
    finally:
        built.close()


def _score_run(runs: tuple[JobRun, ...]) -> JobRun:
    matching = [run for run in runs if run.job_name == STEP_SCORE]
    assert matching, "no nightly.score run was recorded"
    return matching[-1]


def test_completion_requires_every_step(fixture: _Fixture) -> None:
    """`spec §R-28.a`: a run is complete only once every step has succeeded."""

    calls: list[str] = []

    def make_handler(name: str, *, fail: bool) -> Callable[[Any], Mapping[str, object]]:
        def handler(_context: Any) -> Mapping[str, object]:
            calls.append(name)
            if fail:
                raise RuntimeError(f"{name} deliberately failed")
            return {"ok": True}

        return handler

    handlers = {step: make_handler(step, fail=(step == STEP_RANK)) for step in PIPELINE_STEPS}
    registry = JobRegistry()
    register_nightly_pipeline(registry, handlers, policy=default_step_policy(max_attempts=1))
    runner = JobRunner(registry, fixture.session_factory)

    result = run_nightly_pipeline(runner, trigger="manual")

    assert result.success is False
    assert result.failed_step == STEP_RANK
    assert result.completed_steps == PIPELINE_STEPS[: PIPELINE_STEPS.index(STEP_RANK)]
    assert STEP_DISPATCH not in calls, "a later step must never run once an earlier one has failed"


def test_ranked_exposure_converts_facility_crore_to_rupees(fixture: _Fixture) -> None:
    """Ranking crosses a unit boundary, and must convert as it does.

    `Facility` money is held in ₹ crore — the ratio library declares
    `unit="₹ crore"` on every absolute-amount covenant and bands
    `drawing_power_headroom` in crore.  `TriageEntry.exposure` is rupees,
    which is what the queue and the case file format.  Ranking is the only
    hop between them; carrying the figure across unchanged is why a ₹636
    crore book rendered as "₹636.30".
    """

    portfolio = fixture.portfolio("EXPOSURE")
    borrower = fixture.borrower(portfolio, "B-EXPOSURE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))

    service = fixture.build_service(
        statement_lines=_lines_provider({version.id: Decimal("3.5")})
    )
    runner = fixture.build_runner(service)
    result = run_nightly_pipeline(runner, trigger="manual", as_of=_TODAY.isoformat())

    assert result.success is True
    forecast_run_id = UUID(_score_run(result.runs).metrics["forecast_run_id"])
    entries = fixture.triage_entries(forecast_run_id)
    assert len(entries) == 1
    # The fixture's facility is outstanding ₹9,00,000 crore.
    assert entries[0].exposure == Decimal("900000") * Decimal("10000000")


def test_step_failure_halts_and_preserves_prior_day(fixture: _Fixture) -> None:
    """A step failure halts the run at that step; yesterday's complete run
    keeps serving the queue untouched."""

    portfolio = fixture.portfolio("HALT")
    borrower = fixture.borrower(portfolio, "B-HALT")
    version = fixture.covenant_version(borrower)
    prior_run = fixture.seed_complete_run(borrower, as_of_date=_YESTERDAY, band="watch")

    def failing_lines(_version: CovenantVersion, _as_of_date: date) -> Mapping[str, Decimal] | None:
        raise RuntimeError("statement source unavailable")

    service = fixture.build_service(statement_lines=failing_lines)
    runner = fixture.build_runner(service)

    result = run_nightly_pipeline(runner, trigger="manual", as_of=_TODAY.isoformat())

    assert result.success is False
    assert result.failed_step == STEP_TEST
    assert result.completed_steps == (PIPELINE_STEPS[0],)

    # Nothing downstream of the failed step ever ran.
    for step in PIPELINE_STEPS[PIPELINE_STEPS.index(STEP_TEST) + 1 :]:
        assert fixture.job_runs(result.run_id, step) == []
    test_run = fixture.job_runs(result.run_id, STEP_TEST)[0]
    assert test_run.state == "failed"

    # Today produced no covenant test, and yesterday's queue is untouched.
    assert fixture.covenant_tests(version.id) == []
    unchanged = fixture.forecast_run(prior_run.id)
    assert unchanged.state == "complete"
    assert [entry.band for entry in fixture.triage_entries(prior_run.id)] == ["watch"]


def test_single_borrower_run_scoped(fixture: _Fixture) -> None:
    """A single-borrower trigger touches only that borrower's data."""

    portfolio = fixture.portfolio("SCOPE")
    borrower_one = fixture.borrower(portfolio, "B-ONE")
    borrower_two = fixture.borrower(portfolio, "B-TWO")
    version_one = fixture.covenant_version(borrower_one)
    version_two = fixture.covenant_version(borrower_two)
    fixture.seed_test(version_one, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    fixture.seed_test(version_two, as_of_date=_YESTERDAY, value=Decimal("2.0"))

    lines = _lines_provider({version_one.id: Decimal("2.1"), version_two.id: Decimal("2.2")})
    service = fixture.build_service(statement_lines=lines)
    runner = fixture.build_runner(service)

    result = run_nightly_pipeline(
        runner, trigger="manual", as_of=_TODAY.isoformat(), borrower_id=borrower_two.id
    )

    assert result.success is True
    assert [test.as_of_date for test in fixture.covenant_tests(version_one.id)] == [_YESTERDAY], (
        "the other borrower must be untouched: only its seeded prior test may exist"
    )
    tests_two = fixture.covenant_tests(version_two.id)
    assert sorted(test.as_of_date for test in tests_two) == [_YESTERDAY, _TODAY]

    score_run = _score_run(result.runs)
    forecast_run_id = UUID(score_run.metrics["forecast_run_id"])
    entries = fixture.triage_entries(forecast_run_id)
    assert [entry.borrower_id for entry in entries] == [borrower_two.id]
    assert fixture.cases(borrower_id=borrower_one.id) == []


def test_snapshot_captured_once_at_start(fixture: _Fixture) -> None:
    """A threshold snapshot changing after a run starts must not change
    which snapshot that run is recorded against."""

    portfolio = fixture.portfolio("SNAPSHOT")
    borrower = fixture.borrower(portfolio, "B-SNAP")
    version = fixture.covenant_version(borrower)
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))

    store = _FakeThresholdStore(uuid4())
    original_snapshot_id = store.snapshot_id()
    lines = _lines_provider({version.id: Decimal("2.1")})
    service = fixture.build_service(threshold_store=store, statement_lines=lines)
    runner = fixture.build_runner(service)

    result = run_nightly_pipeline(runner, trigger="manual", as_of=_TODAY.isoformat())
    assert result.success is True
    score_run = _score_run(result.runs)
    forecast_run_id = UUID(score_run.metrics["forecast_run_id"])
    assert fixture.forecast_run(forecast_run_id).threshold_snapshot_id == original_snapshot_id

    # The store's active snapshot changes mid-run (a threshold gets approved
    # between last night's batch and a retry of just the score step).
    new_snapshot_id = uuid4()
    store.set_snapshot_id(new_snapshot_id)
    assert store.snapshot_id() == new_snapshot_id

    retried = runner.run_now(
        STEP_SCORE, trigger="manual-retry", run_id=result.run_id, as_of=_TODAY.isoformat()
    )
    assert retried.state == "succeeded"
    assert fixture.forecast_run(forecast_run_id).threshold_snapshot_id == original_snapshot_id, (
        "the run's snapshot must stay pinned to the one captured when it started"
    )


def test_retry_resumes_without_redoing_committed_work(fixture: _Fixture) -> None:
    """Retrying a step for the same run id must not redo already-committed
    work."""

    portfolio = fixture.portfolio("RETRY")
    borrower = fixture.borrower(portfolio, "B-RETRY")
    version = fixture.covenant_version(borrower)
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))

    calls: list[UUID] = []
    lines = _counting_lines_provider({version.id: Decimal("2.1")}, calls)
    service = fixture.build_service(statement_lines=lines)
    runner = fixture.build_runner(service)
    run_id = "rq-retry-fixed-run"

    first = runner.run_now(STEP_TEST, trigger="manual", run_id=run_id, as_of=_TODAY.isoformat())
    second = runner.run_now(
        STEP_TEST, trigger="manual-retry", run_id=run_id, as_of=_TODAY.isoformat()
    )

    assert first.state == "succeeded"
    assert second.state == "succeeded"
    assert calls == [version.id], "the statement provider must be consulted only once"
    # One seeded prior-day test plus exactly one new test for today — the
    # retry must not have produced a second test for today.
    assert sorted(test.as_of_date for test in fixture.covenant_tests(version.id)) == [
        _YESTERDAY,
        _TODAY,
    ]
    assert second.metrics["already_tested"] == 1
    assert second.metrics["tested"] == 0


def test_rerun_identical_and_no_duplicate_notifications(fixture: _Fixture) -> None:
    """Triggering the same pipeline run twice produces identical outputs and
    never a second set of notifications — the failure users notice fastest."""

    portfolio = fixture.portfolio("RERUN")
    borrower = fixture.borrower(portfolio, "B-RERUN")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))

    # Today's value breaches the threshold outright, so the candidate is
    # `already_breached` and lands reliably in the act band regardless of
    # the exact trend/velocity numbers.
    lines = _lines_provider({version.id: Decimal("3.5")})
    assignee_id = uuid4()
    with fixture.session_factory() as session:
        session.add(
            AppUser(
                id=assignee_id,
                username="assignee",
                email="assignee@example.com",
                full_name="Assignee",
                auth_source="local",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-assignee",
            )
        )
        session.commit()

    service = fixture.build_service(statement_lines=lines, default_assignee_id=assignee_id)
    runner = fixture.build_runner(service)
    run_id = "rq-rerun-fixed-run"

    first = run_nightly_pipeline(runner, trigger="manual", run_id=run_id, as_of=_TODAY.isoformat())
    assert first.success is True
    first_score = _score_run(first.runs)
    first_hash = first_score.metrics["content_hash"]
    forecast_run_id = UUID(first_score.metrics["forecast_run_id"])

    entries = fixture.triage_entries(forecast_run_id)
    assert [entry.band for entry in entries] == ["act"]
    assert len(fixture.cases(borrower_id=borrower.id)) == 1
    assert len(fixture.notifications()) == 1

    second = run_nightly_pipeline(runner, trigger="manual", run_id=run_id, as_of=_TODAY.isoformat())
    assert second.success is True
    second_score = _score_run(second.runs)

    assert second_score.metrics["content_hash"] == first_hash
    assert UUID(second_score.metrics["forecast_run_id"]) == forecast_run_id
    # One seeded prior-day test plus exactly one test for today — the rerun
    # must not have produced a second test for today.
    assert sorted(test.as_of_date for test in fixture.covenant_tests(version.id)) == [
        _YESTERDAY,
        _TODAY,
    ]
    assert len(fixture.triage_entries(forecast_run_id)) == 1
    assert len(fixture.cases(borrower_id=borrower.id)) == 1, "no second case may be opened"
    assert len(fixture.notifications()) == 1, "no second notification may be sent"
