"""Focused regression coverage for the phase 7c presentation surfaces."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_radar.db.models.forecast import ForecastDriver, ForecastPath
from covenant_radar.db.repositories.triage import TriageRepository
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.web.svg.trajectory import (
    TrajectoryCrossing,
    TrajectoryLedgerFigure,
    TrajectoryPoint,
    render_trajectory_svg,
)
from tests.integration.test_case_file import _Fixture as CaseFileFixture
from tests.integration.test_forecast_panel import _forecast, _path
from tests.integration.test_queue_screen import _NOW, _Fixture

pytestmark = pytest.mark.integration


def test_queue_summary_is_scoped_and_not_page_sized() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("SUMMARY")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        for rank, (band, exposure, changed) in enumerate(
            (
                ("act", Decimal("100"), "band worsened"),
                ("amber", Decimal("200"), "probability increased"),
                ("watch", Decimal("300"), "no change; probability movement 0.01"),
            ),
            start=1,
        ):
            borrower = fixture.borrower(portfolio, f"B-SUMMARY-{rank}")
            row = fixture.entry(run, borrower, rank, band=band, exposure=exposure)
            row.what_changed = changed
        fixture.session.flush()

        summary = TriageRepository(fixture.session).summary(
            resolve_scope(fixture.principal, fixture.session)
        )

        assert summary.total == 3
        assert summary.act == 1
        assert summary.amber == 1
        assert summary.watch == 1
        assert summary.what_changed == 2
        assert summary.exposure_total == Decimal("600.0000")
    finally:
        fixture.close()


def test_queue_renders_a_stored_mini_trajectory() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("SPARK")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-SPARK")
        version = fixture.covenant_version(borrower, "CV-SPARK")
        fixture.entry(run, borrower, 1, worst_covenant_version_id=version.id)
        for day_offset, value in ((0, "2.80"), (30, "3.05"), (90, "3.60")):
            fixture.session.add(
                ForecastPath(
                    id=uuid4(),
                    run_id=run.id,
                    covenant_version_id=version.id,
                    day_offset=day_offset,
                    projected_value=Decimal(value),
                    headroom_pct=Decimal("5"),
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=f"rq-spark-path-{day_offset}",
                )
            )
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get("/")

        assert response.status_code == 200
        assert 'data-trajectory-size="mini"' in response.text
        assert 'class="queue-row__trajectory"' in response.text
    finally:
        fixture.close()


def test_full_trajectory_names_the_crossing_driver_and_escapes_it() -> None:
    rendered = render_trajectory_svg(
        "forecast-safe",
        (TrajectoryPoint(0, Decimal("2.8")), TrajectoryPoint(90, Decimal("3.6"))),
        Decimal("3.25"),
        (TrajectoryLedgerFigure("Threshold", "3.25x"),),
        crossing=TrajectoryCrossing(
            day=60,
            date_label="29 Oct 2026",
            label="Dominant driver: debt / <risk>",
        ),
    )

    body = str(rendered)
    assert 'data-crossing-day="60"' in body
    assert 'data-crossing-label="Dominant driver: debt / &lt;risk&gt;"' in body
    assert 'data-crossing-annotation="true"' in body
    assert "Crossing day 60" in body
    assert "Dominant driver: debt / &lt;risk&gt;" in body
    assert "<risk>" not in body


def test_case_file_reads_and_renders_persisted_crossing_driver() -> None:
    fixture = CaseFileFixture()
    try:
        fixture.triage()
        for horizon in (30, 60, 90):
            forecast = _forecast(
                fixture,
                horizon,
                crossing_date=date(2026, 10, 29),
                crossing_day=60,
            )
            if horizon == 90:
                fixture.session.add(
                    ForecastDriver(
                        id=uuid4(),
                        forecast_id=forecast.id,
                        name="debt expansion",
                        share=Decimal("0.7500"),
                        is_other=False,
                        created_at=fixture.run.created_at,
                        updated_at=fixture.run.updated_at,
                        request_id="rq-phase7c-driver",
                    )
                )
        _path(fixture)
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        assert response.status_code == 200
        assert 'data-crossing-label="Dominant driver: debt expansion"' in response.text
        assert "Crossing day 60" in response.text
        assert "Dominant driver: debt expansion" in response.text
    finally:
        fixture.close()
