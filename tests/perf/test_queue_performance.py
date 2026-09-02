"""Reference-size performance check for the T-061 queue query."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.db.base import Base
from covenant_radar.db.models import Borrower, ForecastRun, Portfolio, TriageEntry
from covenant_radar.db.repositories.triage import TriageRepository
from covenant_radar.db.scoping import Scope

pytestmark = pytest.mark.perf

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


def test_first_page_within_budget_at_reference_size() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            portfolio = Portfolio(
                id=uuid4(),
                code="PERF",
                name="Performance portfolio",
                path=f"{uuid4().hex}/",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-perf-portfolio",
            )
            run = ForecastRun(
                id=uuid4(),
                as_of_date=date(2026, 8, 30),
                started_at=_NOW,
                finished_at=_NOW,
                covenant_count=10_000,
                state="complete",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-perf-run",
            )
            session.add_all([portfolio, run])
            session.flush()
            borrowers = [
                Borrower(
                    id=uuid4(),
                    reference=f"B-{position:06d}",
                    legal_name=f"Borrower {position:06d}",
                    portfolio_id=portfolio.id,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=f"rq-perf-borrower-{position}",
                )
                for position in range(10_000)
            ]
            session.add_all(borrowers)
            session.flush()
            session.add_all(
                TriageEntry(
                    id=uuid4(),
                    run_id=run.id,
                    borrower_id=borrower.id,
                    probability=Decimal("0.50"),
                    confidence=Decimal("0.80"),
                    exposure=Decimal("100"),
                    urgency=Decimal("1"),
                    band="watch",
                    rank=position + 1,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=f"rq-perf-entry-{position}",
                )
                for position, borrower in enumerate(borrowers)
            )
            session.flush()

            repository = TriageRepository(session, cursor_secret=b"p" * 32)
            started = perf_counter()
            page = repository.query(
                Scope.from_paths(uuid4(), [portfolio.path]),
                filters={"band": "watch"},
                page_size=50,
            )
            elapsed = perf_counter() - started

            assert len(page.entries) == 50
            assert page.has_more
            assert elapsed < 1.2
    finally:
        engine.dispose()
