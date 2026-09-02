"""Integration coverage for the T-061 portfolio queue read path."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.core.errors import Conflict
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, Case, ForecastRun, Portfolio, TriageEntry
from covenant_radar.db.repositories.triage import TriageRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.triage.views import (
    QUEUE_EMPTY_MESSAGE,
    QueueFilters,
    SavedView,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.principal_id = uuid4()
        self.repository = TriageRepository(self.session, cursor_secret=b"q" * 32)

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def portfolio(self, code: str, *, parent: Portfolio | None = None) -> Portfolio:
        result = Portfolio.create(
            code=code,
            name=f"Portfolio {code}",
            parent=parent,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-portfolio-{code.lower()}",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def borrower(
        self,
        portfolio: Portfolio,
        reference: str,
        *,
        industry_code: str | None = None,
    ) -> Borrower:
        result = Borrower(
            id=uuid4(),
            reference=reference,
            legal_name=f"Legal {reference}",
            industry_code=industry_code,
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-borrower-{reference.lower()}",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def user(self, username: str) -> AppUser:
        result = AppUser(
            id=uuid4(),
            username=username,
            email=f"{username}@example.com",
            full_name=username.title(),
            auth_source="local",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-user-{username}",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def run(self, as_of_date: date, *, state: str = "complete") -> ForecastRun:
        result = ForecastRun(
            id=uuid4(),
            as_of_date=as_of_date,
            started_at=_NOW - timedelta(hours=1),
            finished_at=_NOW if state == "complete" else None,
            covenant_count=0,
            state=state,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-run-{as_of_date.isoformat()}-{state}",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def entry(
        self,
        run: ForecastRun,
        borrower: Borrower,
        rank: int,
        *,
        band: str = "watch",
        sma_band: str | None = None,
        urgency: str = "1",
    ) -> TriageEntry:
        result = TriageEntry(
            id=uuid4(),
            run_id=run.id,
            borrower_id=borrower.id,
            probability=Decimal("0.50"),
            confidence=Decimal("0.80"),
            exposure=Decimal("100"),
            urgency=Decimal(urgency),
            band=band,
            sma_band=sma_band,
            rank=rank,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-entry-{rank}-{borrower.reference.lower()}",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def case(
        self,
        borrower: Borrower,
        *,
        state: str,
        assignee: AppUser | None = None,
        run: ForecastRun | None = None,
    ) -> Case:
        result = Case(
            id=uuid4(),
            reference=f"C-{borrower.reference}",
            borrower_id=borrower.id,
            opened_from_run_id=run.id if run is not None else None,
            state=state,
            assignee_id=assignee.id if assignee is not None else None,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-case-{borrower.reference.lower()}",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def scope(self, *portfolios: Portfolio) -> Scope:
        return Scope.from_paths(self.principal_id, [portfolio.path for portfolio in portfolios])


def test_latest_complete_run_only() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("LATEST")
        borrower_old = fixture.borrower(portfolio, "B-OLD")
        borrower_new = fixture.borrower(portfolio, "B-NEW")
        old_run = fixture.run(date(2026, 8, 29))
        new_run = fixture.run(date(2026, 8, 30))
        incomplete = fixture.run(date(2026, 8, 31), state="running")
        fixture.entry(old_run, borrower_old, 1)
        fixture.entry(new_run, borrower_new, 1)
        fixture.entry(incomplete, borrower_old, 1)

        page = fixture.repository.query(fixture.scope(portfolio))

        assert page.run_id == new_run.id
        assert [row.borrower_reference for row in page.entries] == ["B-NEW"]
    finally:
        fixture.close()


def test_every_filter_applies() -> None:
    fixture = _Fixture()
    try:
        focus_portfolio = fixture.portfolio("FOCUS")
        other_portfolio = fixture.portfolio("OTHER")
        assignee = fixture.user("focus-assignee")
        run = fixture.run(date(2026, 8, 30))
        focus = fixture.borrower(focus_portfolio, "B-FOCUS", industry_code="FOCUS-IND")
        wrong_band = fixture.borrower(focus_portfolio, "B-BAND", industry_code="FOCUS-IND")
        wrong_sma = fixture.borrower(focus_portfolio, "B-SMA", industry_code="FOCUS-IND")
        wrong_industry = fixture.borrower(focus_portfolio, "B-IND", industry_code="OTHER-IND")
        wrong_portfolio = fixture.borrower(other_portfolio, "B-PORT", industry_code="FOCUS-IND")
        fixture.entry(run, focus, 1, band="act", sma_band="SMA-1")
        fixture.entry(run, wrong_band, 2, band="amber", sma_band="SMA-1")
        fixture.entry(run, wrong_sma, 3, band="act", sma_band="SMA-2")
        fixture.entry(run, wrong_industry, 4, band="act", sma_band="SMA-1")
        fixture.entry(run, wrong_portfolio, 5, band="act", sma_band="SMA-1")
        fixture.case(focus, state="open", assignee=assignee, run=run)

        scope = fixture.scope(focus_portfolio, other_portfolio)
        checks = (
            QueueFilters(band="act"),
            QueueFilters(portfolio_id=focus_portfolio.id),
            QueueFilters(industry_code="FOCUS-IND"),
            QueueFilters(assignee_id=assignee.id),
            QueueFilters(sma_band="SMA-1"),
            QueueFilters(case_state="open"),
        )
        for filter_number, filters in enumerate(checks, start=1):
            try:
                page = fixture.repository.query(scope, filters, page_size=10)
            except Exception as error:
                pytest.fail(f"filter {filter_number} ({filters.to_dict()}) failed: {error}")
            assert page.entries
            if filters.band is not None:
                assert all(row.band == filters.band for row in page.entries)
            if filters.portfolio_id is not None:
                assert all(row.portfolio_id == filters.portfolio_id for row in page.entries)
            if filters.industry_code is not None:
                assert all(row.industry_code == filters.industry_code for row in page.entries)
            if filters.assignee_id is not None:
                assert all(row.assignee_id == filters.assignee_id for row in page.entries)
            if filters.sma_band is not None:
                assert all(row.sma_band == filters.sma_band for row in page.entries)
            if filters.case_state is not None:
                assert all(row.case_state == filters.case_state for row in page.entries)

        combined = fixture.repository.query(
            scope,
            QueueFilters(
                band="act",
                portfolio_id=focus_portfolio.id,
                industry_code="FOCUS-IND",
                assignee_id=assignee.id,
                sma_band="SMA-1",
                case_state="open",
            ),
        )
        assert [row.borrower_id for row in combined.entries] == [focus.id]

        assert (
            fixture.repository.query(
                scope,
                QueueFilters(industry_code="DOES-NOT-EXIST"),
            ).entries
            == ()
        )
    finally:
        fixture.close()


def test_stale_cursor_refused_with_reload() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("CURSOR")
        first = fixture.borrower(portfolio, "B-FIRST")
        second = fixture.borrower(portfolio, "B-SECOND")
        first_run = fixture.run(date(2026, 8, 30))
        fixture.entry(first_run, first, 1)
        fixture.entry(first_run, second, 2)

        page = fixture.repository.query(fixture.scope(portfolio), page_size=1)
        assert page.next_cursor is not None
        second_page = fixture.repository.query(
            fixture.scope(portfolio), page_size=1, cursor=page.next_cursor
        )
        assert [row.borrower_reference for row in second_page.entries] == ["B-SECOND"]
        assert second_page.next_cursor is None

        newer_run = fixture.run(date(2026, 8, 31))
        fixture.entry(newer_run, second, 1)

        with pytest.raises(Conflict, match="reload"):
            fixture.repository.query(fixture.scope(portfolio), page_size=1, cursor=page.next_cursor)
    finally:
        fixture.close()


def test_scope_applied_in_query() -> None:
    fixture = _Fixture()
    try:
        visible = fixture.portfolio("VISIBLE")
        hidden = fixture.portfolio("HIDDEN")
        run = fixture.run(date(2026, 8, 30))
        visible_borrower = fixture.borrower(visible, "B-VISIBLE")
        hidden_borrower = fixture.borrower(hidden, "B-HIDDEN")
        fixture.entry(run, visible_borrower, 1)
        fixture.entry(run, hidden_borrower, 2)
        scope = fixture.scope(visible)

        page = fixture.repository.query(scope)

        assert [row.borrower_reference for row in page.entries] == ["B-VISIBLE"]
        compiled = str(
            fixture.repository.build_statement(fixture.scope(visible)).compile(
                compile_kwargs={"literal_binds": False}
            )
        )
        assert "portfolio" in compiled.lower()
        assert "triage_entry" in compiled.lower()
        query_plan = fixture.repository.explain(scope)
        assert any("triage_entry_run_id_" in plan for plan in query_plan), query_plan
    finally:
        fixture.close()


def test_saved_view_round_trips() -> None:
    portfolio_id = uuid4()
    assignee_id = uuid4()
    view = SavedView(
        name="Act accounts",
        filters=QueueFilters(
            band="act",
            portfolio_id=portfolio_id,
            industry_code="INFRA",
            assignee_id=assignee_id,
            sma_band="SMA-1",
            case_state="open",
        ),
    )

    restored = SavedView.from_json(view.to_json())

    assert restored == view
    assert restored.filters.portfolio_id == portfolio_id
    assert restored.filters.assignee_id == assignee_id


def test_no_run_returns_documented_empty_state() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("EMPTY")

        page = fixture.repository.query(fixture.scope(portfolio))

        assert page.entries == ()
        assert page.run_id is None
        assert page.reason == "no_complete_run"
        assert page.message == QUEUE_EMPTY_MESSAGE
    finally:
        fixture.close()
