"""Integration coverage for the T-073 portfolio queue screen."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    Borrower,
    Case,
    Covenant,
    CovenantVersion,
    Facility,
    Forecast,
    ForecastRun,
    Portfolio,
    TriageEntry,
    UserPortfolioScope,
)
from covenant_radar.domain.triage.views import QUEUE_EMPTY_MESSAGE
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.queue import create_queue_router
from covenant_radar.web.view_models.queue import EMPTY_SCOPE_MESSAGE, SUPPRESSED_TEXT

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_LONG_NAME = "Meridian Auto Components Private Limited"


class _Fixture:
    def __init__(self) -> None:
        # TestClient serves the ASGI app from a worker thread.  StaticPool
        # keeps this deliberately tiny in-memory database available to that
        # thread, while check_same_thread makes the shared test connection
        # safe for the request lifecycle.  Production uses the configured
        # pooled database engine instead.
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.principal = Principal.user(uuid4(), (Permission.VIEW_QUEUE,))
        self.app_user(self.principal.id, "caller")

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def client(self) -> TestClient:
        app = create_app(
            routers=(create_queue_router(self.session),),
            principal_resolver=lambda _request: self.principal,
        )
        return TestClient(app)

    def portfolio(self, code: str) -> Portfolio:
        result = Portfolio.create(
            code=code,
            name=f"Portfolio {code}",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-portfolio-{code.lower()}",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def grant_scope(self, portfolio: Portfolio) -> None:
        self.session.add(
            UserPortfolioScope(
                user_id=self.principal.id,
                portfolio_id=portfolio.id,
                include_descendants=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-scope-{portfolio.code.lower()}",
            )
        )
        self.session.flush()

    def borrower(self, portfolio: Portfolio, reference: str, *, legal_name: str = "") -> Borrower:
        result = Borrower(
            id=uuid4(),
            reference=reference,
            legal_name=legal_name or f"Legal {reference}",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-borrower-{reference.lower()}",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def app_user(self, user_id, username: str) -> AppUser:
        result = AppUser(
            id=user_id,
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

    def covenant_version(self, borrower: Borrower, reference: str) -> CovenantVersion:
        facility = Facility(
            reference=f"F-{reference}",
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000.0000"),
            currency="INR",
            sanction_date=date(2026, 1, 1),
            effective_from=date(2026, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-facility-{reference.lower()}",
        )
        self.session.add(facility)
        self.session.flush()
        covenant = Covenant(
            reference=reference,
            facility_id=facility.id,
            name="Total Debt / Tangible Net Worth",
            covenant_class="financial",
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-covenant-{reference.lower()}",
        )
        self.session.add(covenant)
        self.session.flush()
        version = CovenantVersion(
            covenant_id=covenant.id,
            version_no=1,
            threshold=Decimal("3.25"),
            direction="max",
            unit="x",
            frequency="quarterly",
            test_basis="standalone",
            effective_from=date(2026, 1, 1),
            status="live",
            tested_at_least_once=False,
            registered_by_id=self.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-version-{reference.lower()}",
        )
        self.session.add(version)
        self.session.flush()
        return version

    def forecast(
        self, run: ForecastRun, version: CovenantVersion, horizon: int, *, crossing: date | None
    ) -> Forecast:
        result = Forecast(
            id=uuid4(),
            run_id=run.id,
            covenant_version_id=version.id,
            horizon_days=horizon,
            probability=Decimal("0.5825"),
            confidence=Decimal("0.9"),
            below_confidence_floor=False,
            projected_cross_date=crossing,
            direction="max",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-forecast-{version.id}-{horizon}",
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
        probability: Decimal | None = Decimal("0.5825"),
        worst_covenant_version_id=None,
        worst_horizon: int | None = None,
        exposure: Decimal | None = Decimal("100"),
    ) -> TriageEntry:
        result = TriageEntry(
            id=uuid4(),
            run_id=run.id,
            borrower_id=borrower.id,
            worst_covenant_version_id=worst_covenant_version_id,
            worst_horizon=worst_horizon,
            probability=probability,
            confidence=Decimal("0.80"),
            exposure=exposure,
            urgency=Decimal("1"),
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

    def case(self, borrower: Borrower, *, state: str) -> Case:
        result = Case(
            id=uuid4(),
            reference=f"C-{borrower.reference}",
            borrower_id=borrower.id,
            state=state,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-case-{borrower.reference.lower()}",
        )
        self.session.add(result)
        self.session.flush()
        return result


def test_order_matches_service_exactly() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("ORDER")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        first = fixture.borrower(portfolio, "B-FIRST", legal_name="Alpha Industries Limited")
        second = fixture.borrower(portfolio, "B-SECOND", legal_name="Beta Textiles Private Limited")
        third = fixture.borrower(portfolio, "B-THIRD", legal_name="Gamma Logistics Limited")
        fixture.entry(run, third, 3, worst_covenant_version_id=None)
        fixture.entry(run, first, 1, worst_covenant_version_id=None)
        fixture.entry(run, second, 2, worst_covenant_version_id=None)

        with fixture.client() as client:
            response = client.get("/")

        assert response.status_code == 200
        body = response.text
        positions = [body.index(borrower.legal_name) for borrower in (first, second, third)]
        assert positions == sorted(positions)
    finally:
        fixture.close()


def test_suppressed_row_shows_text_not_number() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("SUPPRESS")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-SUPPRESSED", legal_name=_LONG_NAME)
        version = fixture.covenant_version(borrower, "CV-SUPPRESS")
        fixture.entry(
            run,
            borrower,
            1,
            worst_covenant_version_id=version.id,
            worst_horizon=90,
            probability=None,
        )

        with fixture.client() as client:
            response = client.get("/")

        assert response.status_code == 200
        assert SUPPRESSED_TEXT in response.text
        assert "None%" not in response.text
    finally:
        fixture.close()


def test_empty_scope_designed_state() -> None:
    fixture = _Fixture()
    try:
        other_portfolio = fixture.portfolio("OTHER")
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(other_portfolio, "B-OTHER")
        fixture.entry(run, borrower, 1)
        # The caller is granted no scope at all, so the run exists but this
        # caller's page has zero rows -- the "empty scope" case, distinct
        # from "no completed run".

        with fixture.client() as client:
            response = client.get("/")

        assert response.status_code == 200
        assert EMPTY_SCOPE_MESSAGE in response.text
        assert 'class="state state--empty"' in response.text
        assert "<table" not in response.text
    finally:
        fixture.close()


def test_no_completed_run_state() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("NORUN")
        fixture.grant_scope(portfolio)
        # No ForecastRun exists at all yet.

        with fixture.client() as client:
            response = client.get("/")

        assert response.status_code == 200
        assert QUEUE_EMPTY_MESSAGE in response.text
        assert "<table" not in response.text
    finally:
        fixture.close()


def test_accent_only_in_band_chips() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("ACCENT")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        act = fixture.borrower(portfolio, "B-ACT")
        amber = fixture.borrower(portfolio, "B-AMBER")
        watch = fixture.borrower(portfolio, "B-WATCH")
        fixture.entry(run, act, 1, band="act", worst_covenant_version_id=None)
        fixture.entry(run, amber, 2, band="amber", worst_covenant_version_id=None)
        fixture.entry(run, watch, 3, band="watch", worst_covenant_version_id=None)

        with fixture.client() as client:
            response = client.get("/")

        body = response.text
        chip_count = len(
            re.findall(r'class="band-chip band-chip--(?:act|amber|watch|neutral)"', body)
        )
        assert chip_count == 3
        # No accent CSS class appears anywhere outside a band-chip element.
        # The guard is about class attributes, not prose: the queue explains
        # its own vocabulary, and that copy must be free to name a covenant
        # breach without tripping a styling check.
        for accent_class in ("headroom", "watch-bg", "breach", "band-chip--"):
            occurrences = len(re.findall(rf'class="[^"]*{re.escape(accent_class)}', body))
            chip_occurrences = len(
                re.findall(rf'class="band-chip[^"]*{re.escape(accent_class)}', body)
            )
            assert occurrences == chip_occurrences
        assert body.count('class="band-chip') == 3
    finally:
        fixture.close()


def test_long_name_wraps() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("WRAP")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-WRAP", legal_name=_LONG_NAME)
        fixture.entry(run, borrower, 1, worst_covenant_version_id=None)

        with fixture.client() as client:
            response = client.get("/")

        body = response.text
        assert _LONG_NAME in body
        assert "…" not in body
        assert "text-overflow" not in body
        assert "white-space: nowrap" not in body
    finally:
        fixture.close()


@pytest.mark.parametrize("target", ("queue-workspace", "queue-summary", "queue-ledger"))
def test_live_regions_return_scoped_fragments(target: str) -> None:
    fixture = _Fixture()
    try:
        with fixture.client() as client:
            response = client.get(
                "/",
                headers={"HX-Request": "true", "HX-Target": target},
            )

        assert response.status_code == 200
        assert f'id="{target}"' in response.text
        assert "<html" not in response.text
        assert response.headers["vary"] == "HX-Request, HX-Target"
    finally:
        fixture.close()


def test_workspace_fragment_carries_counts_and_rows() -> None:
    """One swap region means the counts can never describe a different slice.

    The band tiles, the filter chips and the ledger are re-read together, so
    this fragment must contain all three or the guarantee is not real.
    """

    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("WORKSPACE")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-WORKSPACE")
        fixture.entry(run, borrower, 1, band="act", worst_covenant_version_id=None)

        with fixture.client() as client:
            response = client.get(
                "/",
                headers={"HX-Request": "true", "HX-Target": "queue-workspace"},
            )

        body = response.text
        assert 'id="queue-summary"' in body
        assert 'id="queue-filters"' in body
        assert 'id="queue-ledger"' in body
        assert 'id="queue-selection"' in body
        assert borrower.legal_name in body
    finally:
        fixture.close()


def test_band_accent_tracks_severity() -> None:
    """Colour means urgency, and the stylesheet is the only place that decides.

    Act is the emergency and must read as the breach accent; Watch is listed
    but not escalated and must carry no accent at all.  Asserting against the
    stylesheet keeps the mapping from silently re-inverting.
    """

    stylesheet = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "covenant_radar"
        / "web"
        / "static"
        / "css"
        / "app.css"
    ).read_text(encoding="utf-8")

    def block(selector: str) -> str:
        match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", stylesheet)
        assert match is not None, f"{selector} is not defined in app.css"
        return match.group(1)

    for prefix in (".band-chip", ".queue-summary-chip"):
        act = block(f"{prefix}--act")
        amber = block(f"{prefix}--amber")
        watch = block(f"{prefix}--watch")
        assert "var(--breach)" in act and "var(--breach-bg)" in act
        assert "var(--watch)" in amber and "var(--watch-bg)" in amber
        # The least urgent band borrows no accent from either direction.
        for token in ("--breach", "--watch)", "--watch-bg", "--headroom"):
            assert token not in watch
        # Green is reserved for genuine covenant headroom on the case file.
        assert "--headroom" not in act and "--headroom" not in amber


def test_detail_row_is_hidden_and_matches_its_disclosure() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("DETAIL")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-DETAIL")
        fixture.entry(run, borrower, 1, worst_covenant_version_id=None)

        with fixture.client() as client:
            body = client.get("/").text

        controls = re.findall(r'aria-controls="(queue-detail-[^"]+)"', body)
        assert len(controls) == 1
        detail_id = controls[0]
        assert f'<tr class="ledger-detail" id="{detail_id}" hidden>' in body
        # Every disclosure starts collapsed, so the server's state and the
        # first thing script does about it agree.
        assert 'aria-expanded="false"' in body
    finally:
        fixture.close()


def test_row_checkbox_carries_the_case_reference() -> None:
    """The selection posts a case handle, and only for rows that have one."""

    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("SELECT")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        with_case = fixture.borrower(portfolio, "B-WITHCASE")
        without_case = fixture.borrower(portfolio, "B-NOCASE")
        case = fixture.case(with_case, state="open")
        fixture.entry(run, with_case, 1, worst_covenant_version_id=None)
        fixture.entry(run, without_case, 2, worst_covenant_version_id=None)

        with fixture.client() as client:
            body = client.get("/").text

        boxes = re.findall(r'<input type="checkbox"\s+class="row-select".*?>', body, re.DOTALL)
        assert len(boxes) == 2
        selectable = [box for box in boxes if "disabled" not in box]
        # A borrower with no case cannot be acted on, so its box is inert
        # rather than posting an empty selection.
        assert len(selectable) == 1
        assert f'value="{case.reference}"' in selectable[0]
        # The boxes live in the table and the bar lives outside it; `form=`
        # is what makes a no-script selection possible at all.
        assert all('form="queue-selection"' in box for box in boxes)
    finally:
        fixture.close()


def test_exposure_reads_in_rupees_like_the_rest_of_the_app() -> None:
    """Exposure is rupees, and a corporate book must read as crore on screen.

    `test_case_file.py` already pins this unit for the same column, and the
    reference-portfolio generator was writing crore into it — which is why
    every demo showed a covenanted corporate at "₹636.30".
    """

    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("EXPOSURE")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-EXPOSURE")
        fixture.entry(
            run,
            borrower,
            1,
            exposure=Decimal("6363000000"),
            worst_covenant_version_id=None,
        )

        with fixture.client() as client:
            body = client.get("/").text

        cells = re.findall(r'ledger-row__cell--amount">([^<]*)<', body)
        assert cells == ["₹636.3 crore"]
    finally:
        fixture.close()


def test_filter_chip_removes_only_its_own_parameter() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("CHIPS")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-CHIPS")
        fixture.entry(run, borrower, 1, band="act", sma_band="SMA-1")

        with fixture.client() as client:
            body = client.get("/?band=act&sma_band=SMA-1").text

        chips = re.findall(r'class="queue-filter-chip"\s+href="([^"]+)"', body)
        assert len(chips) == 2
        # Undoing one decision keeps the other; that is the whole point of a
        # chip over a single "Clear filters" link.
        assert sorted(chips) == ["/?band=act", "/?sma_band=SMA-1"]
    finally:
        fixture.close()
