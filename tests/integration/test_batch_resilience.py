"""Integration coverage for `T-122`: partial-failure policy, retry and
deadline alerting (`plan.md §5.9`, `spec §R-28.b`/`R-28.d`).

Uses a private, file-based SQLite database, the same pattern
`tests/integration/test_scheduler.py` (`T-120`) and
`tests/integration/test_nightly_pipeline.py` (`T-121`) already established
for self-contained scheduler/ledger coverage on a laptop with no live
PostgreSQL instance.
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
    Covenant,
    CovenantTest,
    CovenantVersion,
    Facility,
    Portfolio,
    RatioDefinition,
)
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.forecast import ForecastRun
from covenant_radar.db.models.forecast import TriageEntry as TriageEntryModel
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.repositories.triage import TriageRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.forecast import Weights
from covenant_radar.scheduler.jobs import JobRegistry
from covenant_radar.scheduler.pipeline import (
    PIPELINE_STEPS,
    STEP_DISPATCH,
    STEP_RANK,
    STEP_SCORE,
    STEP_TEST,
    STEP_UPDATE_CASES,
    default_step_policy,
    register_nightly_pipeline,
    run_nightly_pipeline,
)
from covenant_radar.scheduler.runner import JobRunner
from covenant_radar.services.nightly import NightlyPipelineService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 8, 31)
_YESTERDAY = date(2026, 8, 30)
_WEIGHTS = Weights(distance=Decimal("1"), velocity=Decimal("1"), pressure=Decimal("1"))

# T12's default deadline (07:00 IST) is 01:30 UTC on the same calendar day
# (IST is UTC+5:30, no daylight saving).
_BEFORE_DEADLINE = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)
_AFTER_DEADLINE = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)


class _FakeThresholdStore:
    """A minimal `services.nightly.ThresholdSnapshotProvider` test double,
    the same shape `tests/integration/test_nightly_pipeline.py` already
    uses, extended with T12 for this task's deadline checks."""

    def __init__(self, snapshot_id: UUID, *, deadline_ist: str = "07:00") -> None:
        self._values: dict[str, Mapping[str, object]] = {
            "T1": {"act": Decimal("0.60"), "amber": Decimal("0.30")},
            "T2": {"confidence_floor": Decimal("0.10")},
            "T5": {"contribution_share": Decimal("0.10")},
            "T12": {"deadline_ist": deadline_ist},
        }
        self._snapshot_id = snapshot_id

    def get(self, name: str) -> Mapping[str, object]:
        return self._values[name]

    def snapshot_id(self) -> UUID:
        return self._snapshot_id


def _lines_provider(
    values: Mapping[UUID, Decimal],
) -> Callable[[CovenantVersion, date], Mapping[str, Decimal] | None]:
    def provider(version: CovenantVersion, _as_of_date: date) -> Mapping[str, Decimal] | None:
        if version.id not in values:
            return None
        return {"total_debt": values[version.id], "tangible_net_worth": Decimal("1")}

    return provider


def _always_failing_lines(
    message: str,
) -> Callable[[CovenantVersion, date], Mapping[str, Decimal] | None]:
    def provider(_version: CovenantVersion, _as_of_date: date) -> Mapping[str, Decimal] | None:
        raise RuntimeError(message)

    return provider


class _Fixture:
    """Shared database, model builders and pipeline wiring for one test."""

    def __init__(self, tmp_path: Path) -> None:
        database_path = tmp_path / "batch_resilience.db"
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
        outstanding: Decimal = Decimal("900000"),
    ) -> CovenantVersion:
        with self.session_factory() as session:
            facility = Facility(
                id=uuid4(),
                reference=f"F-{borrower.reference}",
                borrower_id=borrower.id,
                facility_type="term_loan",
                sanctioned_limit=Decimal("1000000"),
                currency="INR",
                outstanding=outstanding,
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
        """A prior day's already-complete run: "the last complete run still
        serving, with its age visible, never a blank queue" (`spec §R-28.b`)."""

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
            statement = (
                select(JobRun)
                .where(JobRun.run_id == run_id, JobRun.job_name == job_name)
                .order_by(JobRun.attempt)
            )
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

    def audit_events(self, event_type: str | None = None) -> list[AuditEvent]:
        with self.session_factory() as session:
            statement = select(AuditEvent)
            if event_type is not None:
                statement = statement.where(AuditEvent.event_type == event_type)
            return list(session.execute(statement).scalars().all())

    def queue_page(self, portfolio: Portfolio):
        with self.session_factory() as session:
            scope = Scope(principal_id=self.system_actor_id, descendant_paths=(portfolio.path,))
            return TriageRepository(session).query(scope)


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


def test_single_borrower_failure_isolated(fixture: _Fixture) -> None:
    """`spec §R-28.b`: one borrower's own bad data (a negative outstanding
    balance) is isolated at ranking, recorded, and the rest of the book is
    still scored and ranked."""

    portfolio = fixture.portfolio("ISO")
    good = fixture.borrower(portfolio, "B-GOOD")
    bad = fixture.borrower(portfolio, "B-BAD")
    good_version = fixture.covenant_version(good, outstanding=Decimal("900000"))
    bad_version = fixture.covenant_version(bad, outstanding=Decimal("-500"))
    fixture.seed_test(good_version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    fixture.seed_test(bad_version, as_of_date=_YESTERDAY, value=Decimal("2.0"))

    lines = _lines_provider(
        {good_version.id: Decimal("2.1"), bad_version.id: Decimal("2.1")}
    )
    service = fixture.build_service(statement_lines=lines)
    runner = fixture.build_runner(service)

    result = run_nightly_pipeline(runner, trigger="manual", as_of=_TODAY.isoformat())

    assert result.success is True, "one bad borrower must not halt the book"
    score_run = _score_run(result.runs)
    forecast_run_id = UUID(score_run.metrics["forecast_run_id"])

    entries = fixture.triage_entries(forecast_run_id)
    assert [entry.borrower_id for entry in entries] == [good.id], (
        "only the good borrower may be ranked; the bad one is isolated, not silently dropped"
    )

    rank_run = fixture.job_runs(result.run_id, STEP_RANK)[-1]
    assert rank_run.state == "succeeded"
    assert rank_run.metrics["ranked"] == 1
    assert rank_run.metrics["failed"] == 1
    assert rank_run.metrics["succeeded"] == 1
    assert rank_run.metrics["attempted"] == 2
    failures = rank_run.metrics["failures"]
    assert len(failures) == 1
    assert failures[0]["item_id"] == str(bad.id)
    assert "negative" in failures[0]["error"].lower()


def test_step_exhaustion_marks_run_failed_naming_step(fixture: _Fixture) -> None:
    """`spec §R-28.b`: a step that exhausts every retry is marked failed,
    naming the step, after genuinely retrying (not failing once and
    giving up silently)."""

    attempts: list[int] = []

    def make_handler(name: str, *, fail: bool) -> Callable[[Any], Mapping[str, object]]:
        def handler(context: Any) -> Mapping[str, object]:
            if fail:
                attempts.append(context.attempt)
                raise RuntimeError(f"{name} deliberately failed")
            return {"ok": True}

        return handler

    handlers = {step: make_handler(step, fail=(step == STEP_SCORE)) for step in PIPELINE_STEPS}
    registry = JobRegistry()
    register_nightly_pipeline(registry, handlers, policy=default_step_policy(max_attempts=3))
    runner = JobRunner(registry, fixture.session_factory)

    result = run_nightly_pipeline(runner, trigger="manual")

    assert result.success is False
    assert result.failed_step == STEP_SCORE
    assert attempts == [1, 2, 3], "the runner must exhaust every declared retry, not just one"

    score_runs = fixture.job_runs(result.run_id, STEP_SCORE)
    assert [run.state for run in score_runs] == ["failed", "failed", "failed"]
    assert [run.attempt for run in score_runs] == [1, 2, 3]

    for step in PIPELINE_STEPS[PIPELINE_STEPS.index(STEP_SCORE) + 1 :]:
        assert fixture.job_runs(result.run_id, step) == []


def test_deadline_alert_raised_run_continues(fixture: _Fixture) -> None:
    """T12 (`spec §17.5`): a run still open at the deadline raises an
    alert while the run itself keeps going, and the alert is never raised
    twice for the same run."""

    calls: list[str] = []

    def make_handler(name: str, *, fail_first: bool) -> Callable[[Any], Mapping[str, object]]:
        def handler(context: Any) -> Mapping[str, object]:
            calls.append(name)
            if fail_first and context.attempt == 1:
                raise RuntimeError(f"{name} not ready yet")
            return {"ok": True}

        return handler

    handlers = {
        step: make_handler(step, fail_first=(step == STEP_RANK)) for step in PIPELINE_STEPS
    }
    registry = JobRegistry()
    register_nightly_pipeline(registry, handlers, policy=default_step_policy(max_attempts=1))
    runner = JobRunner(registry, fixture.session_factory)
    run_id = "rq-deadline-run"

    result = run_nightly_pipeline(
        runner, trigger="manual", run_id=run_id, as_of=_TODAY.isoformat()
    )
    assert result.success is False, "the run is genuinely still open for this test's setup"

    store = _FakeThresholdStore(uuid4())
    before = fixture.build_service(threshold_store=store, clock=FixedClock(_BEFORE_DEADLINE))
    early = before.check_deadline(run_id, as_of=_TODAY.isoformat())
    assert early["breached"] is False
    assert early["alert_raised"] is False
    assert fixture.audit_events() == [], "no alert before the deadline"

    after = fixture.build_service(threshold_store=store, clock=FixedClock(_AFTER_DEADLINE))
    first_check = after.check_deadline(run_id, as_of=_TODAY.isoformat())
    assert first_check["breached"] is True
    assert first_check["is_complete"] is False
    assert first_check["alert_raised"] is True
    assert len(fixture.audit_events()) == 1

    second_check = after.check_deadline(run_id, as_of=_TODAY.isoformat())
    assert second_check["alert_raised"] is False
    assert second_check["already_raised"] is True
    assert len(fixture.audit_events()) == 1, "the same run must never be alerted on twice"

    # The run itself keeps going after the alert — retrying the failed step
    # and letting the rest of the pipeline finish must succeed normally.
    retried = runner.run_now(STEP_RANK, trigger="manual-retry", run_id=run_id, attempt=2)
    assert retried.state == "succeeded"
    for step in (STEP_UPDATE_CASES, STEP_DISPATCH):
        remaining = runner.run_now(step, trigger="manual-retry", run_id=run_id)
        assert remaining.state == "succeeded"

    completion_check = after.check_deadline(run_id, as_of=_TODAY.isoformat())
    assert completion_check["is_complete"] is True
    assert completion_check["alert_raised"] is False, "a complete run is never alerted on"


def test_queue_serves_last_complete_run_with_age(fixture: _Fixture) -> None:
    """`spec §R-28.b`: when tonight's run never reaches a new complete
    run, the queue keeps serving yesterday's complete run with its age
    visible — never a blank queue."""

    portfolio = fixture.portfolio("AGE")
    borrower = fixture.borrower(portfolio, "B-AGE")
    # A live covenant version so tonight's pipeline has something to test
    # (and genuinely fail on) rather than trivially succeeding with nothing
    # due.
    fixture.covenant_version(borrower)
    prior_run = fixture.seed_complete_run(borrower, as_of_date=_YESTERDAY, band="amber")

    service = fixture.build_service(
        statement_lines=_always_failing_lines("statement source unavailable")
    )
    runner = fixture.build_runner(service)

    result = run_nightly_pipeline(runner, trigger="manual", as_of=_TODAY.isoformat())
    assert result.success is False
    assert result.failed_step == STEP_TEST

    page = fixture.queue_page(portfolio)
    assert page.run_id == prior_run.id
    assert page.as_of_date == _YESTERDAY
    assert (_TODAY - page.as_of_date).days == 1, "the data age must be derivable from the page"
    assert [entry.band for entry in page.entries] == ["amber"]
    assert [entry.borrower_id for entry in page.entries] == [borrower.id]


def test_recurring_failure_escalates(fixture: _Fixture) -> None:
    """`spec §R-28.b`: the same failure recurring across nights is
    escalated once it repeats, and never re-escalated for the same run."""

    portfolio = fixture.portfolio("RECUR")
    borrower = fixture.borrower(portfolio, "B-RECUR")
    version = fixture.covenant_version(borrower)
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))

    failing = _always_failing_lines("statement source unavailable")
    service = fixture.build_service(statement_lines=failing)
    runner = fixture.build_runner(service, max_attempts=1)

    first_run_id = "rq-recur-night-1"
    second_run_id = "rq-recur-night-2"
    runner.run_now(STEP_TEST, trigger="manual", run_id=first_run_id, as_of=_TODAY.isoformat())

    # Checked the morning after night one: only one failure exists yet.
    first_check = service.check_recurring_failure(STEP_TEST, run_id=first_run_id)
    assert first_check["escalated"] is False, "one failure alone is not yet recurring"

    runner.run_now(STEP_TEST, trigger="manual", run_id=second_run_id, as_of=_TODAY.isoformat())
    second_check = service.check_recurring_failure(STEP_TEST, run_id=second_run_id)
    assert second_check["escalated"] is True
    assert second_check["consecutive_failures"] == 2
    escalations = fixture.audit_events()
    assert len(escalations) == 1

    repeat_check = service.check_recurring_failure(STEP_TEST, run_id=second_run_id)
    assert repeat_check["escalated"] is False
    assert repeat_check["already_escalated"] is True
    assert len(fixture.audit_events()) == 1, "the same recurrence must never be escalated twice"


def test_never_presents_partial_as_complete(fixture: _Fixture) -> None:
    """A run is reported complete only once every one of the six steps has
    succeeded — never on the strength of an early step alone — and a
    step's own isolated per-item failures are always visible in its
    metrics, never silently folded into a clean-looking success."""

    portfolio = fixture.portfolio("PARTIAL")
    good = fixture.borrower(portfolio, "B-PGOOD")
    bad = fixture.borrower(portfolio, "B-PBAD")
    good_version = fixture.covenant_version(good, outstanding=Decimal("900000"))
    bad_version = fixture.covenant_version(bad, outstanding=Decimal("-100"))
    fixture.seed_test(good_version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    fixture.seed_test(bad_version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    lines = _lines_provider(
        {good_version.id: Decimal("2.1"), bad_version.id: Decimal("2.1")}
    )
    service = fixture.build_service(statement_lines=lines)
    runner = fixture.build_runner(service)

    run_id = "rq-partial-run"
    result = run_nightly_pipeline(
        runner, trigger="manual", run_id=run_id, as_of=_TODAY.isoformat()
    )
    assert result.success is True

    # Every step succeeded, so the deadline policy correctly reports the
    # run complete — a bad borrower isolated inside one step is not the
    # same thing as the pipeline being incomplete.
    complete_check = service.check_deadline(run_id, as_of=_TODAY.isoformat())
    assert complete_check["is_complete"] is True

    rank_run = fixture.job_runs(run_id, STEP_RANK)[-1]
    assert rank_run.metrics["failed"] == 1, (
        "the isolated borrower failure must remain visible in the run report, "
        "never silently absorbed into a clean 'ranked' count"
    )

    # Now a genuinely halted run: only three of six steps ever ran.
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
    halted_runner = JobRunner(registry, fixture.session_factory)
    halted_run_id = "rq-partial-halted-run"

    halted_result = run_nightly_pipeline(
        halted_runner, trigger="manual", run_id=halted_run_id, as_of=_TODAY.isoformat()
    )
    assert halted_result.success is False

    halted_check = service.check_deadline(halted_run_id, as_of=_TODAY.isoformat())
    assert halted_check["is_complete"] is False, (
        "a run missing three of its six steps must never be reported complete, "
        "even though every step that did run succeeded"
    )
