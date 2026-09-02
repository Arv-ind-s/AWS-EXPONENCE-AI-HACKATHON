"""The `T-083` screen manifest: every real screen template, rendered
through its real route with real (if minimal) data, reusing the
self-contained SQLite fixtures each feature's own integration test module
already built — the same reuse `tests/a11y/test_why_panel_a11y.py` and
`tests/e2e/test_horizon_control.py` established rather than duplicating
fixture setup here. Two fragment templates
(`screens/statements/_restate_result.html`, whose only route is a `POST`
that requires reproducing the statement-import chart/mapping pipeline in
full) are rendered directly through Jinja2 with a representative context
instead, the same technique `tests/e2e/test_component_gallery.py` already
uses for `_states/component_gallery.html` — still a real render of the
real template, just not behind a live round trip through an unrelated
subsystem. That one exception is called out at its definition below.

`test_all_screens.py` imports only `SCREENS` and `COVERED_TEMPLATES` from
this module.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
import covenant_radar.db.models  # noqa: F401 - registers every table on Base.metadata
from covenant_radar.db.base import Base
from covenant_radar.db.models.covenant import CovenantSchedule
from covenant_radar.db.models.document import DocumentPage
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import CertificateRequest
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.security.mfa import MfaSettings, TOTPService
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.admin_users import AdminUsersService
from covenant_radar.services.auth import AuthenticationSettings
from covenant_radar.services.catalogue import CatalogueService
from covenant_radar.services.certificates import CertificateService
from covenant_radar.services.registry import RegistryService
from covenant_radar.web.routes.admin import create_admin_config_router, create_admin_users_router
from covenant_radar.web.routes.auth import CHALLENGE_COOKIE_NAME, create_auth_router
from covenant_radar.web.routes.catalogue import create_catalogue_router
from covenant_radar.web.routes.certificates import create_certificates_router
from covenant_radar.web.routes.covenants import create_covenants_router
from covenant_radar.web.routes.documents import create_documents_router
from covenant_radar.web.routes.master_data import create_master_data_router
from covenant_radar.web.routes.statements import create_statements_router
from covenant_radar.web.routes.why import create_why_router
from tests.integration.test_admin_ops import _World as _AdminOpsWorld
from tests.integration.test_audit_screens import _World as _AuditWorld
from tests.integration.test_auth_local import _build as _build_auth, _STRONG_PASSWORD
from tests.integration.test_case_file import _Fixture as _CaseFileFixture
from tests.integration.test_case_file import financials as _case_file_financials
from tests.integration.test_case_screens import _Fixture as _CasesFixture
from tests.integration.test_document_upload import _Fixture as _DocumentFixture
from tests.integration.test_evidence_margin import _Fixture as _EvidenceFixture
from tests.integration.test_forecast_panel import _forecast, _path
from tests.integration.test_governance_screens import _World as _GovernanceWorld
from tests.integration.test_intake_screen import _generator as _intake_generator
from tests.integration.test_intake_screen import _ScreenFixture as _IntakeFixture
from tests.integration.test_master_data import _Bundle as _MasterDataBundle
from tests.integration.test_inapp_notifications import _Fixture as _NotificationFixture
from tests.integration.test_queue_screen import _Fixture as _QueueFixture
from tests.integration.test_search import _SearchBundle
from tests.integration.test_simulator_screen import _SimulatorFixture
from tests.integration.test_why_panel import _Bundle as _WhyBundle

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ScreenState:
    """One reachable rendering of one screen — `theme -> html`."""

    name: str
    render: Callable[[str], str]


@dataclass(frozen=True)
class ScreenCase:
    """One or more templates that always render together from one route,
    with every state of that route the fixtures below can actually reach.

    `fragment=True` marks an htmx partial meant to be swapped into an
    already-landmarked host page rather than a full document — see
    `assert_accessible`'s `fragment` parameter."""

    name: str
    templates: tuple[str, ...]
    states: tuple[ScreenState, ...]
    fragment: bool = False


def _get(
    client: TestClient, path: str, *, theme: str, allow_5xx: bool = False, **kwargs: object
) -> str:
    client.cookies.set("covenant_radar_theme", theme)
    response = client.get(path, **kwargs)
    if not allow_5xx:
        assert response.status_code < 500, (
            f"GET {path} returned {response.status_code}: {response.text[:500]}"
        )
    return response.text


class _Audit:
    """The minimal append-only audit double every service constructor
    below requires, matching the shape every integration fixture uses."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: object,
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del event_type, subject, payload, actor, request_id
        return object()


@contextmanager
def _sqlite_session() -> Iterator[Session]:
    """`StaticPool` (`tests/integration/test_queue_screen.py::_Fixture`
    already documents why): `TestClient` runs the route handler from a
    worker thread, and SQLAlchemy's default `SingletonThreadPool` for a
    bare `sqlite:///:memory:` engine hands a thread its first-ever
    checkout as a genuinely separate, schema-less database unless every
    thread shares the one physical connection `StaticPool` keeps alive."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _prime(session: Session) -> None:
    """Force a fixture's session to bind its connection on the calling
    (main) thread before a `TestClient` request reaches it from a worker
    thread — see `_sqlite_session` above for why this matters for any
    fixture whose engine isn't built with `StaticPool` and that performs
    no write before the request that would otherwise do this implicitly."""
    session.execute(text("SELECT 1"))


# ---------------------------------------------------------------------------
# auth/*  (screens/auth/sign_in.html, change_password.html, mfa_enrol.html,
# mfa_verify.html) — the only screens with no `create_app`-based render
# precedent anywhere in the suite (`tests/integration/test_auth_local.py`
# only asserts the redirect/cookie side). Every route is `@public`; the
# state a page shows depends entirely on which signed challenge cookie the
# request carries, so each state below drives `AuthService` directly to
# reach that `AuthStatus`, then issues the real HTTP GET with that cookie.
# ---------------------------------------------------------------------------


def _sign_in_rest(theme: str) -> str:
    service, _users, _sessions, _audit, _clock = _build_auth()
    app = create_app(routers=(create_auth_router(service),))
    with TestClient(app) as client:
        return _get(client, "/sign-in", theme=theme)


def _change_password_rest(theme: str) -> str:
    from covenant_radar.services.auth import UserRecord

    from tests.integration.test_auth_local import _password_service, _USER_ID

    password_service = _password_service()
    user = UserRecord(
        id=_USER_ID,
        username="alice",
        password_hash=password_service.hash(_STRONG_PASSWORD),
        must_change_password=True,
    )
    service, _users, _sessions, _audit, _clock = _build_auth(user=user)
    result = service.sign_in("alice", _STRONG_PASSWORD)
    assert result.challenge_cookie is not None
    app = create_app(routers=(create_auth_router(service),))
    with TestClient(app) as client:
        client.cookies.set(CHALLENGE_COOKIE_NAME, result.challenge_cookie)
        return _get(client, "/password/change", theme=theme)


def _mfa_fixture() -> tuple[object, object]:
    clock = FixedClock(_NOW)
    mfa = TOTPService(b"c" * 32, settings=MfaSettings(enabled=True), clock=clock)
    service, _users, _sessions, _audit, _clock = _build_auth(
        clock=clock,
        mfa=mfa,
        auth_settings=AuthenticationSettings(mfa_required=True),
    )
    return service, mfa


def _mfa_enrol_rest(theme: str) -> str:
    service, _mfa = _mfa_fixture()
    primary = service.sign_in("alice", _STRONG_PASSWORD)
    assert primary.challenge_cookie is not None
    app = create_app(routers=(create_auth_router(service),))
    with TestClient(app) as client:
        client.cookies.set(CHALLENGE_COOKIE_NAME, primary.challenge_cookie)
        return _get(client, "/mfa/enrol", theme=theme)


def _mfa_verify_rest(theme: str) -> str:
    service, mfa = _mfa_fixture()
    primary = service.sign_in("alice", _STRONG_PASSWORD)
    assert primary.challenge_cookie is not None
    enrollment = service.begin_mfa_enrollment(primary.challenge_cookie)
    code = mfa.code_for_secret(enrollment.enrollment.secret, _NOW)
    completed = service.complete_mfa_enrollment(enrollment.challenge_cookie, code)
    assert completed.authenticated

    second = service.sign_in("alice", _STRONG_PASSWORD)
    assert second.challenge_cookie is not None
    app = create_app(routers=(create_auth_router(service),))
    with TestClient(app) as client:
        client.cookies.set(CHALLENGE_COOKIE_NAME, second.challenge_cookie)
        return _get(client, "/mfa/verify", theme=theme)


# ---------------------------------------------------------------------------
# shell error pages (screens/_404.html, _500.html) — `T-022`'s own render
# precedent, `tests/integration/test_shell.py`.
# ---------------------------------------------------------------------------


def _not_found_rest(theme: str) -> str:
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        return _get(client, "/route-that-does-not-exist", theme=theme)


def _server_error_rest(theme: str) -> str:
    from covenant_radar.api.deps import public

    app = create_app()

    @app.get("/__t083_explode")
    @public
    async def explode() -> None:
        raise RuntimeError("synthetic failure for the a11y audit")

    with TestClient(app, raise_server_exceptions=False) as client:
        return _get(client, "/__t083_explode", theme=theme, allow_5xx=True)


# ---------------------------------------------------------------------------
# master_data/* — `_Bundle` from `tests/integration/test_master_data.py`.
# ---------------------------------------------------------------------------


def _master_data_client(bundle: _MasterDataBundle) -> TestClient:
    app = create_app(
        routers=(create_master_data_router(bundle.service),),
        principal_resolver=lambda _request: bundle.principal,
    )
    return TestClient(app)


def _borrowers_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        portfolio = bundle.root()
        bundle.borrower(portfolio)
        with _master_data_client(bundle) as client:
            return _get(client, "/borrowers", theme=theme)
    finally:
        bundle.close()


def _borrowers_empty(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        bundle.root()
        with _master_data_client(bundle) as client:
            return _get(client, "/borrowers", theme=theme)
    finally:
        bundle.close()


def _borrower_form_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        bundle.root()
        with _master_data_client(bundle) as client:
            return _get(client, "/borrowers/new", theme=theme)
    finally:
        bundle.close()


def _borrower_detail_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        with _master_data_client(bundle) as client:
            return _get(client, f"/borrowers/{borrower.reference}", theme=theme)
    finally:
        bundle.close()


def _facilities_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        bundle.facility(borrower)
        with _master_data_client(bundle) as client:
            return _get(client, "/facilities", theme=theme)
    finally:
        bundle.close()


def _facilities_empty(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        bundle.root()
        with _master_data_client(bundle) as client:
            return _get(client, "/facilities", theme=theme)
    finally:
        bundle.close()


def _facility_insights_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        bundle.facility(borrower)
        with _master_data_client(bundle) as client:
            return _get(client, "/facilities/insights", theme=theme)
    finally:
        bundle.close()


def _facility_insights_empty(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        bundle.root()
        with _master_data_client(bundle) as client:
            return _get(client, "/facilities/insights", theme=theme)
    finally:
        bundle.close()


def _facility_form_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        bundle.root()
        with _master_data_client(bundle) as client:
            return _get(client, "/facilities/new", theme=theme)
    finally:
        bundle.close()


def _facility_detail_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        facility = bundle.facility(borrower)
        with _master_data_client(bundle) as client:
            return _get(client, f"/facilities/{facility.reference}", theme=theme)
    finally:
        bundle.close()


def _portfolios_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        bundle.root()
        with _master_data_client(bundle) as client:
            return _get(client, "/portfolios", theme=theme)
    finally:
        bundle.close()


def _portfolios_empty(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        _prime(bundle.session)
        with _master_data_client(bundle) as client:
            return _get(client, "/portfolios", theme=theme)
    finally:
        bundle.close()


def _portfolio_form_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        with _master_data_client(bundle) as client:
            return _get(client, "/portfolios/new", theme=theme)
    finally:
        bundle.close()


def _portfolio_detail_rest(theme: str) -> str:
    bundle = _MasterDataBundle()
    try:
        portfolio = bundle.root()
        with _master_data_client(bundle) as client:
            return _get(client, f"/portfolios/{portfolio.id}", theme=theme)
    finally:
        bundle.close()


# ---------------------------------------------------------------------------
# covenants/* — built on the same `RegistryService.register` pattern
# `tests/integration/test_why_panel.py::_Bundle` establishes, with maker
# checker off so a single `register()` call yields an immediately live
# version (matching that fixture's own reasoning for disabling it).
# ---------------------------------------------------------------------------


@dataclass
class _CovenantWorld:
    session: Session
    principal: Principal
    scope: Scope
    facility_id: object
    registry: RegistryService
    reference: str = "CV-T083"


def _covenant_world(session: Session) -> _CovenantWorld:
    from covenant_radar.db.models.borrower import Borrower
    from covenant_radar.db.models.facility import Facility

    principal = Principal.user(
        uuid4(),
        (
            Permission.VIEW_COVENANT,
            Permission.REGISTER_COVENANT,
            Permission.APPROVE_COVENANT,
            Permission.RECORD_WAIVER,
        ),
    )
    portfolio = Portfolio.create(
        code="T083-COV",
        name="T083 covenant root",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t083-cov-portfolio",
    )
    session.add(portfolio)
    session.flush()
    borrower = Borrower(
        reference="B-T083",
        legal_name="T083 Covenant Borrower Private Limited",
        portfolio_id=portfolio.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t083-cov-borrower",
    )
    session.add(borrower)
    session.flush()
    facility = Facility(
        reference="F-T083",
        borrower_id=borrower.id,
        facility_type="term_loan",
        sanctioned_limit=Decimal("1000"),
        currency="INR",
        sanction_date=date(2025, 1, 1),
        effective_from=date(2025, 1, 1),
        outstanding=Decimal("700"),
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t083-cov-facility",
    )
    session.add(facility)
    session.flush()
    scope = Scope.from_paths(principal.id, [portfolio.path])
    registry = RegistryService(
        session,
        audit=_Audit(),
        clock=FixedClock(_NOW),
        request_id="rq-t083-cov-registry",
        maker_checker_enabled=False,
        scope_resolver=lambda _principal: scope,
    )
    return _CovenantWorld(
        session=session,
        principal=principal,
        scope=scope,
        facility_id=facility.id,
        registry=registry,
    )


def _register_covenant(world: _CovenantWorld) -> None:
    terms = CovenantVersionTerms(
        definition_ref="leverage_ratio",
        custom_formula=None,
        threshold=Decimal("2.5"),
        direction="max",
        unit="x",
        frequency="quarterly",
        test_basis="standalone",
        effective_from=date(2025, 1, 1),
    )
    world.registry.register(
        world.principal,
        facility_id=world.facility_id,
        reference=world.reference,
        name="Leverage ratio",
        covenant_class="financial",
        terms=terms,
        scope=world.scope,
    )


def _covenants_client(world: _CovenantWorld) -> TestClient:
    app = create_app(
        routers=(create_covenants_router(world.registry),),
        principal_resolver=lambda _request: world.principal,
    )
    return TestClient(app)


def _covenants_rest(theme: str) -> str:
    with _sqlite_session() as session:
        world = _covenant_world(session)
        _register_covenant(world)
        with _covenants_client(world) as client:
            return _get(client, "/covenants", theme=theme)


def _covenants_empty(theme: str) -> str:
    with _sqlite_session() as session:
        world = _covenant_world(session)
        with _covenants_client(world) as client:
            return _get(client, "/covenants", theme=theme)


def _covenant_form_rest(theme: str) -> str:
    with _sqlite_session() as session:
        world = _covenant_world(session)
        with _covenants_client(world) as client:
            return _get(client, "/covenants/new", theme=theme)


def _covenant_detail_rest(theme: str) -> str:
    with _sqlite_session() as session:
        world = _covenant_world(session)
        _register_covenant(world)
        with _covenants_client(world) as client:
            return _get(client, f"/covenants/{world.reference}", theme=theme)


def _covenant_approvals_rest(theme: str) -> str:
    with _sqlite_session() as session:
        world = _covenant_world(session)
        with _covenants_client(world) as client:
            return _get(client, "/covenants/approvals", theme=theme)


# ---------------------------------------------------------------------------
# documents/_review.html, _viewer.html
# ---------------------------------------------------------------------------


def _document_review_rest(theme: str) -> str:
    with tempfile.TemporaryDirectory() as tmp_path:
        fixture = _DocumentFixture(Path(tmp_path))
        try:
            document = fixture.upload()
            fixture.session.add(
                DocumentPage(
                    id=new_id(),
                    document_id=document.id,
                    page_number=1,
                    text="OCR text needing review.",
                    ocr_confidence=Decimal("0.40"),
                    needs_review=True,
                    width=612,
                    height=792,
                    created_at=_NOW,
                    updated_at=_NOW,
                    created_by_id=fixture.principal.id,
                    updated_by_id=fixture.principal.id,
                    request_id="rq-t083-review-page",
                )
            )
            fixture.session.flush()
            fixture.service.scope_resolver = lambda _principal: fixture.scope
            app = create_app(
                routers=(create_documents_router(fixture.service),),
                principal_resolver=lambda _request: fixture.principal,
            )
            with TestClient(app) as client:
                return _get(client, "/documents/review", theme=theme)
        finally:
            fixture.close()


def _document_review_empty(theme: str) -> str:
    with tempfile.TemporaryDirectory() as tmp_path:
        fixture = _DocumentFixture(Path(tmp_path))
        try:
            fixture.service.scope_resolver = lambda _principal: fixture.scope
            app = create_app(
                routers=(create_documents_router(fixture.service),),
                principal_resolver=lambda _request: fixture.principal,
            )
            with TestClient(app) as client:
                return _get(client, "/documents/review", theme=theme)
        finally:
            fixture.close()


def _document_viewer_rest(theme: str) -> str:
    with tempfile.TemporaryDirectory() as tmp_path:
        fixture = _DocumentFixture(Path(tmp_path))
        try:
            document = fixture.upload()
            text = "Sanctioned limit is INR 10 crore for the cash credit facility."
            fixture.session.add(
                DocumentPage(
                    id=new_id(),
                    document_id=document.id,
                    page_number=1,
                    text=text,
                    ocr_confidence=None,
                    needs_review=False,
                    width=612,
                    height=792,
                    created_at=_NOW,
                    updated_at=_NOW,
                    created_by_id=fixture.principal.id,
                    updated_by_id=fixture.principal.id,
                    request_id="rq-t083-viewer-page",
                )
            )
            fixture.session.flush()
            fixture.service.scope_resolver = lambda _principal: fixture.scope
            start = text.index("cash credit facility")
            end = start + len("cash credit facility")
            app = create_app(
                routers=(create_documents_router(fixture.service),),
                principal_resolver=lambda _request: fixture.principal,
            )
            with TestClient(app) as client:
                return _get(
                    client,
                    f"/documents/{document.id}/view",
                    theme=theme,
                    params={"page": 1, "start": start, "end": end},
                )
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# why/panel.html, _drawer.html, _stage.html — `tests/integration/
# test_why_panel.py::_Bundle`, exactly as `test_why_panel_a11y.py` reuses it.
# ---------------------------------------------------------------------------


def _why_client(bundle: _WhyBundle) -> TestClient:
    app = create_app(
        routers=(create_why_router(bundle.session),),
        principal_resolver=lambda _request: bundle.principal,
    )
    return TestClient(app)


def _why_not_run(theme: str) -> str:
    bundle = _WhyBundle()
    try:
        with _why_client(bundle) as client:
            return _get(client, f"/why/covenant_test/{bundle.covenant_test.id}", theme=theme)
    finally:
        bundle.close()


def _why_model_decided(theme: str) -> str:
    bundle = _WhyBundle()
    try:
        bundle.write_model_stage()
        with _why_client(bundle) as client:
            return _get(client, f"/why/covenant_test/{bundle.covenant_test.id}", theme=theme)
    finally:
        bundle.close()


def _why_drawer_fragment(theme: str) -> str:
    bundle = _WhyBundle()
    try:
        with _why_client(bundle) as client:
            return _get(
                client,
                f"/why/covenant_test/{bundle.covenant_test.id}",
                theme=theme,
                headers={"HX-Request": "true"},
            )
    finally:
        bundle.close()


# ---------------------------------------------------------------------------
# borrower/index.html + its fragments — `tests/integration/
# test_case_file.py::_Fixture`, `test_forecast_panel.py`, `test_evidence_
# margin.py`.
# ---------------------------------------------------------------------------


def _borrower_index_rest(theme: str) -> str:
    fixture = _CaseFileFixture()
    try:
        fixture.triage()
        fixture.forecast()
        fixture.test()
        fixture.schedule()
        with fixture.client() as client:
            return _get(client, f"/borrowers/{fixture.borrower.reference}", theme=theme)
    finally:
        fixture.close()


def _borrower_index_with_forecast(theme: str) -> str:
    fixture = _CaseFileFixture()
    try:
        fixture.triage()
        _forecast(fixture, 30)
        _forecast(fixture, 60, crossing_date=date(2026, 10, 29), crossing_day=60)
        _forecast(fixture, 90, crossing_date=date(2026, 10, 29), crossing_day=60)
        _path(fixture)
        with fixture.client() as client:
            return _get(client, f"/borrowers/{fixture.borrower.reference}", theme=theme)
    finally:
        fixture.close()


def _borrower_index_financials(theme: str) -> str:
    """The case file with its financials tab populated.

    The other three borrower states file no statements, so they render that
    tab's empty state and never exercise its statement table, ratio cards or
    series charts — the parts of the panel with sticky headers, an SVG text
    equivalent and a colour-coded verdict strip to get wrong.
    """

    fixture = _CaseFileFixture()
    try:
        fixture.triage()
        _case_file_financials(fixture)
        with fixture.client() as client:
            return _get(client, f"/borrowers/{fixture.borrower.reference}", theme=theme)
    finally:
        fixture.close()


def _borrower_index_evidence(theme: str) -> str:
    fixture = _EvidenceFixture()
    fixture.evidence(
        evidence_type="stock_shortfall", decay_factor=Decimal("0"), counts_toward_pressure=False
    )
    with fixture.client(
        permissions=(Permission.VIEW_BORROWER, Permission.UPLOAD_DOCUMENT)
    ) as client:
        return _get(client, f"/borrowers/{fixture.borrower.reference}", theme=theme)


def _borrower_memo_unavailable(theme: str) -> str:
    """Render the real memo-action fragment when no provider is configured.

    The fragment is a separately reachable POST response, so it needs its
    own manifest entry rather than relying on the case-file page that hosts
    its swap target.
    """

    fixture = _CaseFileFixture()
    try:
        fixture.principal = Principal.user(
            fixture.principal.id,
            (Permission.VIEW_BORROWER, Permission.GENERATE_MEMO),
        )
        with fixture.client() as client:
            client.cookies.set("covenant_radar_theme", theme)
            response = client.post("/memos", data={"borrower_ref": fixture.borrower.reference})
            assert response.status_code < 500, response.text[:500]
            return response.text
    finally:
        fixture.close()


# ---------------------------------------------------------------------------
# intake/index.html — `tests/integration/test_intake_screen.py`.
# ---------------------------------------------------------------------------


def _intake_passing(theme: str) -> str:
    with tempfile.TemporaryDirectory() as tmp_path:
        fixture = _IntakeFixture(Path(tmp_path))
        try:
            document = fixture.document()
            client = fixture.client(generator=_intake_generator)
            client.cookies.set("covenant_radar_theme", theme)
            response = client.post(
                "/intake/proposals",
                data={
                    "document_id": str(document.id),
                    "facility_ref": fixture.bundle.facility.reference,
                },
            )
            assert response.status_code < 500, response.text[:500]
            return response.text
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# admin/_catalogue.html, admin/users/index.html, admin/config/index.html
# ---------------------------------------------------------------------------


def _catalogue_rest(theme: str) -> str:
    with _sqlite_session() as session:
        principal = Principal.user(uuid4(), (Permission.PROPOSE_THRESHOLDS,))
        service = CatalogueService(
            session, audit=_Audit(), clock=FixedClock(_NOW), maker_checker_enabled=False
        )
        service.save(
            principal,
            {
                "id": "T083-WAIVE",
                "role_tag": "risk",
                "text": "Grant a short waiver",
                "effect_model": "level_shift",
                "effect_parameters": {"amount": "-0.10"},
                "applicable_covenant_classes": ("financial",),
                "assumptions": ("The borrower cures within the waiver window.",),
                "requires_approval": False,
                "is_active": True,
            },
        )
        app = create_app(
            routers=(create_catalogue_router(service),),
            principal_resolver=lambda _request: principal,
        )
        with TestClient(app) as client:
            return _get(client, "/admin/catalogue", theme=theme)


def _catalogue_empty(theme: str) -> str:
    with _sqlite_session() as session:
        principal = Principal.user(uuid4(), (Permission.PROPOSE_THRESHOLDS,))
        service = CatalogueService(session, audit=_Audit(), maker_checker_enabled=False)
        app = create_app(
            routers=(create_catalogue_router(service),),
            principal_resolver=lambda _request: principal,
        )
        with TestClient(app) as client:
            return _get(client, "/admin/catalogue", theme=theme)


def _admin_users_rest(theme: str) -> str:
    with _sqlite_session() as session:
        admin = Principal.user(uuid4(), (Permission.MANAGE_USERS,))
        session.add(
            AppUser(
                id=admin.id,
                username="t083-admin",
                email="t083-admin@example.com",
                full_name="T083 Admin",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t083-admin",
            )
        )
        session.flush()
        service = AdminUsersService(
            session, audit=_Audit(), clock=FixedClock(_NOW), request_id="rq-t083-admin-users"
        )
        created = service.create_user(
            admin,
            username="analyst",
            email="analyst@example.com",
            full_name="Risk Analyst",
            password="Correct-Horse-123!",
        )
        app = create_app(
            routers=(create_admin_users_router(service),),
            principal_resolver=lambda _request: admin,
        )
        with TestClient(app) as client:
            return _get(client, f"/admin/users/{created.id}", theme=theme)


def _admin_ops_rest(theme: str) -> str:
    with tempfile.TemporaryDirectory() as tmp_path:
        world = _AdminOpsWorld(Path(tmp_path))
        try:
            with world.client() as client:
                return _get(client, "/admin/jobs", theme=theme)
        finally:
            world.close()


def _admin_config_rest(theme: str) -> str:
    with _sqlite_session() as session:
        principal = Principal.user(
            uuid4(), (Permission.PROPOSE_THRESHOLDS, Permission.APPROVE_THRESHOLDS)
        )
        app = create_app(
            routers=(create_admin_config_router(session),),
            principal_resolver=lambda _request: principal,
        )
        with TestClient(app) as client:
            return _get(client, "/admin/config", theme=theme)


# ---------------------------------------------------------------------------
# audit/index.html, reconstruction.html, bundle_status.html —
# `tests/integration/test_audit_screens.py::_World`.
# ---------------------------------------------------------------------------


def _audit_index_rest(theme: str) -> str:
    world = _AuditWorld()
    try:
        world.record_events(5)
        with world.client() as client:
            return _get(client, "/audit", theme=theme)
    finally:
        world.close()


def _audit_reconstruction_rest(theme: str) -> str:
    world = _AuditWorld()
    try:
        world.record_events(5)
        with world.client() as client:
            return _get(client, f"/audit/warnings/{world.forecast.id}", theme=theme)
    finally:
        world.close()


def _audit_bundle_status_rest(theme: str) -> str:
    world = _AuditWorld()
    try:
        world.record_events(5)
        with world.client() as client:
            client.cookies.set("covenant_radar_theme", theme)
            response = client.post(f"/audit/warnings/{world.forecast.id}/bundle")
            assert response.status_code < 500, response.text[:500]
            return response.text
    finally:
        world.close()


# ---------------------------------------------------------------------------
# simulator/index.html — `tests/integration/test_simulator_screen.py`.
# ---------------------------------------------------------------------------


def _simulator_rest(theme: str) -> str:
    fixture = _SimulatorFixture()
    try:
        fixture.add_intervention("OPTION")
        with fixture.client() as client:
            return _get(client, f"/simulator/{fixture.forecast.id}", theme=theme)
    finally:
        fixture.close()


# ---------------------------------------------------------------------------
# statements/_quarantine_review.html (empty state — no import pipeline
# needed to reach it), _restate.html (the bare form). `_restate_result.html`
# is documented at its manifest entry below.
# ---------------------------------------------------------------------------


def _statements_client(service: object, principal: Principal) -> TestClient:
    app = create_app(
        routers=(create_statements_router(service),),
        principal_resolver=lambda _request: principal,
    )
    return TestClient(app)


def _quarantine_review_empty(theme: str) -> str:
    from covenant_radar.services.statements import StatementImportService

    with _sqlite_session() as session:
        principal = Principal.user(
            uuid4(), (Permission.RESOLVE_QUARANTINE, Permission.CORRECT_SOURCE_DATA)
        )
        service = StatementImportService(session, audit=_Audit(), clock=FixedClock(_NOW))
        with _statements_client(service, principal) as client:
            return _get(client, "/statements/quarantine", theme=theme)


def _financial_statements_import_rest(theme: str) -> str:
    from covenant_radar.services.statements import StatementImportService

    with _sqlite_session() as session:
        principal = Principal.user(uuid4(), (Permission.INGEST_FINANCIAL_STATEMENTS,))
        service = StatementImportService(session, audit=_Audit(), clock=FixedClock(_NOW))
        with _statements_client(service, principal) as client:
            return _get(client, "/financial-statements", theme=theme)


def _restate_form_rest(theme: str) -> str:
    from covenant_radar.services.statements import StatementImportService

    with _sqlite_session() as session:
        principal = Principal.user(uuid4(), (Permission.CORRECT_SOURCE_DATA,))
        service = StatementImportService(session, audit=_Audit(), clock=FixedClock(_NOW))
        with _statements_client(service, principal) as client:
            return _get(client, "/statements/restate", theme=theme)


def _restate_result_direct(theme: str) -> str:
    """`_restate_result.html` renders only from `POST /statements/restate`'s
    success path, which requires an existing `FinancialPeriod` produced by
    the full chart/mapping import pipeline (`T-026`) — reproducing that here
    would duplicate `tests/integration/test_restatement.py` rather than
    audit a screen. Render the real template directly instead, the same
    technique `tests/e2e/test_component_gallery.py` uses for `_states/
    component_gallery.html`, with a `result` object shaped exactly like
    `StatementImportService.restate_period`'s real return value."""
    from covenant_radar.ingestion.statements.restate import DependentTest, RestatementResult

    template_root = (
        Path(__file__).resolve().parents[2] / "src" / "covenant_radar" / "web" / "templates"
    )
    environment = Environment(
        loader=FileSystemLoader(str(template_root)), autoescape=select_autoescape(("html", "xml"))
    )
    principal = Principal.user(uuid4(), (Permission.CORRECT_SOURCE_DATA,))
    result = RestatementResult(
        borrower_id=uuid4(),
        fy_label="FY26Q4",
        previous_period_id=uuid4(),
        previous_version=1,
        new_period_id=uuid4(),
        new_version=2,
        reason="Corrected drawing power after the borrower's revised statement.",
        flagged_tests=(
            DependentTest(
                covenant_test_id=uuid4(),
                covenant_version_id=uuid4(),
                as_of_date=date(2026, 3, 31),
                verdict="watch",
            ),
        ),
    )
    template = environment.get_template("screens/statements/_restate_result.html")
    return template.render(
        request=_FakeRequest(),
        principal=principal,
        locale="en",
        theme=theme,
        text_direction="ltr",
        labels={
            "restate_title": "Restate a financial period",
            "restate_success": "Restatement recorded.",
            "flagged_tests": "Covenant tests flagged for recomputation",
            "no_flagged_tests": "No covenant test read the superseded period.",
            "back_to_quarantine": "Back to quarantine review",
            "reason": "Reason",
        },
        csrf_token="",
        rows=(),
        error="",
        column_prefix="col__",
        result=result,
    )


class _FakeRequest:
    """A minimal stand-in for Jinja's `request` global when a template is
    rendered directly rather than through a live ASGI request (see
    `_restate_result_direct` above)."""

    class _URL:
        path = "/statements/restate"

    url = _URL()
    cookies: dict[str, str] = {}


# ---------------------------------------------------------------------------
# queue/index.html — `tests/integration/test_queue_screen.py::_Fixture`.
# ---------------------------------------------------------------------------


def _queue_rest(theme: str) -> str:
    fixture = _QueueFixture()
    try:
        portfolio = fixture.portfolio("ORDER")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, reference="B-T083Q")
        version = fixture.covenant_version(borrower, "CV-T083Q")
        fixture.forecast(run, version, 30, crossing=None)
        fixture.entry(run, borrower, rank=1)
        with fixture.client() as client:
            return _get(client, "/", theme=theme)
    finally:
        fixture.close()


def _queue_filtered(theme: str) -> str:
    """The queue inside a band slice: chips, saved-view region, selection bar.

    Those regions only render once a filter is in force, so the unfiltered
    `rest` state never reaches them.
    """

    fixture = _QueueFixture()
    try:
        portfolio = fixture.portfolio("ORDER-FILTERED")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, reference="B-T083F")
        version = fixture.covenant_version(borrower, "CV-T083F")
        fixture.forecast(run, version, 30, crossing=None)
        fixture.entry(run, borrower, rank=1, band="act")
        with fixture.client() as client:
            return _get(client, "/?band=act", theme=theme)
    finally:
        fixture.close()


def _queue_empty_scope(theme: str) -> str:
    fixture = _QueueFixture()
    try:
        with fixture.client() as client:
            return _get(client, "/", theme=theme)
    finally:
        fixture.close()


def _queue_no_run(theme: str) -> str:
    fixture = _QueueFixture()
    try:
        portfolio = fixture.portfolio("ORDER-NORUN")
        fixture.grant_scope(portfolio)
        with fixture.client() as client:
            return _get(client, "/", theme=theme)
    finally:
        fixture.close()


# ---------------------------------------------------------------------------
# Search, notifications and export states added by the live-workspace shell.
# Search and notifications use their feature fixtures; export screens use a
# minimal durable job row through the real bulk router.
# ---------------------------------------------------------------------------


def _search_rest(theme: str) -> str:
    bundle = _SearchBundle()
    try:
        with bundle.client() as client:
            return _get(client, "/search?q=global-search-token", theme=theme)
    finally:
        bundle.close()


def _notifications_rest(theme: str) -> str:
    fixture = _NotificationFixture()
    try:
        fixture.notification(subject_type="borrower", subject_id=fixture.borrower.id)
        with fixture.client(include_borrower_route=True) as client:
            return _get(client, "/notifications", theme=theme)
    finally:
        fixture.close()


def _export_status_rest(theme: str) -> str:
    from covenant_radar.db.models import JobRun
    from covenant_radar.scheduler.ledger import RUNNING
    from covenant_radar.services.bulk import BulkService
    from covenant_radar.services.export import ExportService
    from covenant_radar.web.routes.bulk import create_bulk_router
    from tests.integration.test_bulk_operations import _Store

    with _sqlite_session() as session:
        principal = Principal.user(
            uuid4(),
            (Permission.VIEW_CASE, Permission.UPDATE_CASE, Permission.EXPORT_EVIDENCE),
        )
        session.add(
            AppUser(
                id=principal.id,
                username="export-reviewer",
                email="export-reviewer@example.test",
                full_name="Export Reviewer",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t083-export-user",
            )
        )
        export_id = new_id()
        session.add(
            JobRun(
                id=new_id(),
                job_name=f"exports.bulk.{export_id.hex}",
                run_id=str(export_id),
                trigger="bulk-export",
                started_at=_NOW,
                state=RUNNING,
                attempt=1,
                metrics={"format": "csv", "row_count": 24, "filter": {}},
                created_at=_NOW,
                updated_at=_NOW,
                created_by_id=principal.id,
                updated_by_id=principal.id,
                request_id="rq-t083-export",
            )
        )
        session.flush()
        service = ExportService(session, store=_Store(), audit=_Audit(), clock=FixedClock(_NOW))
        router = create_bulk_router(
            BulkService(session, audit=_Audit()),
            export_service=service,
        )
        app = create_app(routers=(router,), principal_resolver=lambda _request: principal)
        with TestClient(app) as client:
            return _get(client, f"/exports/{export_id}", theme=theme)


def _bulk_result_rest(theme: str) -> str:
    from covenant_radar.db.models import Borrower, Case
    from covenant_radar.services.bulk import BulkService

    with _sqlite_session() as session:
        principal = Principal.user(
            uuid4(),
            (Permission.VIEW_CASE, Permission.UPDATE_CASE, Permission.EXPORT_EVIDENCE),
        )
        session.add(
            AppUser(
                id=principal.id,
                username="bulk-reviewer",
                email="bulk-reviewer@example.test",
                full_name="Bulk Reviewer",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t083-bulk-user",
            )
        )
        portfolio = Portfolio.create(
            code="T083-BULK",
            name="Bulk review portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t083-bulk-portfolio",
        )
        session.add(portfolio)
        session.flush()
        borrower = Borrower(
            id=new_id(),
            reference="B-T083-BULK",
            legal_name="Bulk Review Borrower",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t083-bulk-borrower",
        )
        case = Case(
            id=new_id(),
            reference="C-T083-BULK",
            borrower_id=borrower.id,
            state="open",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t083-bulk-case",
        )
        session.add_all((borrower, case))
        session.flush()
        scope = Scope.from_paths(principal.id, (portfolio.path,))
        bulk = BulkService(session, audit=_Audit(), scope_resolver=lambda _principal: scope)
        report = bulk.execute(
            principal,
            (case.id,),
            "state",
            value={"state": "in_progress", "reason": None},
            now=_NOW,
        )
        from covenant_radar.web.routes.bulk import _LABELS
        from starlette.requests import Request

        app = create_app(routers=(), principal_resolver=lambda _request: principal)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
                "root_path": "",
                "path": "/bulk",
                "raw_path": b"/bulk",
                "query_string": b"",
                "headers": (),
                "app": app,
            }
        )
        template = app.state.template_env.get_template("screens/exports/bulk_result.html")
        return template.render(
            request=request,
            principal=principal,
            locale="en",
            theme=theme,
            text_direction="ltr",
            labels=_LABELS,
            csrf_token="",
            report=report,
        )


# ---------------------------------------------------------------------------
# certificates/index.html — built directly on the covenant/schedule chain
# `_covenant_world` above already establishes, following the same
# direct-ORM-row convention every fixture in this module uses.
# ---------------------------------------------------------------------------


def _certificates_rest(theme: str) -> str:
    from covenant_radar.db.models.borrower import Borrower

    with _sqlite_session() as session:
        world = _covenant_world(session)
        _register_covenant(world)
        covenant = world.registry.get_covenant(world.principal, world.reference)
        version_row = world.registry.list_versions(world.principal, covenant.id)[-1]
        schedule = CovenantSchedule(
            id=new_id(),
            covenant_version_id=version_row.id,
            due_date=date(2026, 3, 31),
            state="pending",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t083-schedule",
        )
        session.add(schedule)
        session.flush()

        borrower = session.query(Borrower).filter_by(reference="B-T083").one()
        certificate_request = CertificateRequest(
            id=new_id(),
            covenant_schedule_id=schedule.id,
            borrower_id=borrower.id,
            due_date=date(2026, 3, 31),
            state="requested",
            requested_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t083-certificate",
        )
        session.add(certificate_request)
        session.flush()

        principal = Principal.user(world.principal.id, (Permission.VIEW_COVENANT,))
        service = CertificateService(
            session,
            audit=_Audit(),
            clock=FixedClock(_NOW),
            scope_resolver=lambda _p: world.scope,
        )
        app = create_app(
            routers=(create_certificates_router(service),),
            principal_resolver=lambda _request: principal,
        )
        with TestClient(app) as client:
            return _get(client, "/certificates", theme=theme)


def _certificates_empty(theme: str) -> str:
    with _sqlite_session() as session:
        world = _covenant_world(session)
        principal = Principal.user(world.principal.id, (Permission.VIEW_COVENANT,))
        service = CertificateService(
            session,
            audit=_Audit(),
            clock=FixedClock(_NOW),
            scope_resolver=lambda _p: world.scope,
        )
        app = create_app(
            routers=(create_certificates_router(service),),
            principal_resolver=lambda _request: principal,
        )
        with TestClient(app) as client:
            return _get(client, "/certificates", theme=theme)


# ---------------------------------------------------------------------------
# cases/index.html, detail.html — `tests/integration/
# test_case_screens.py::_Fixture`.
# ---------------------------------------------------------------------------


def _cases_index_rest(theme: str) -> str:
    fixture = _CasesFixture()
    try:
        with fixture.client() as client:
            return _get(client, "/cases", theme=theme)
    finally:
        fixture.close()


def _cases_detail_rest(theme: str) -> str:
    fixture = _CasesFixture()
    try:
        with fixture.client() as client:
            return _get(client, f"/cases/{fixture.case.reference}", theme=theme)
    finally:
        fixture.close()


# ---------------------------------------------------------------------------
# governance/index.html — `tests/integration/
# test_governance_screens.py::_World`.
# ---------------------------------------------------------------------------


def _governance_rest(theme: str) -> str:
    world = _GovernanceWorld()
    try:
        world.add_proposal(maker_id=world.risk_head_id)
        with world.client(principal=world.risk_head) as client:
            return _get(client, "/governance", theme=theme)
    finally:
        world.close()


# ---------------------------------------------------------------------------
# The manifest.
# ---------------------------------------------------------------------------

SCREENS: tuple[ScreenCase, ...] = (
    ScreenCase(
        "auth_sign_in", ("screens/auth/sign_in.html",), (ScreenState("rest", _sign_in_rest),)
    ),
    ScreenCase(
        "auth_change_password",
        ("screens/auth/change_password.html",),
        (ScreenState("rest", _change_password_rest),),
    ),
    ScreenCase(
        "auth_mfa_enrol", ("screens/auth/mfa_enrol.html",), (ScreenState("rest", _mfa_enrol_rest),)
    ),
    ScreenCase(
        "auth_mfa_verify",
        ("screens/auth/mfa_verify.html",),
        (ScreenState("rest", _mfa_verify_rest),),
    ),
    ScreenCase("shell_404", ("screens/_404.html",), (ScreenState("rest", _not_found_rest),)),
    ScreenCase("shell_500", ("screens/_500.html",), (ScreenState("rest", _server_error_rest),)),
    ScreenCase(
        "master_data_borrowers",
        ("screens/master_data/borrowers.html",),
        (ScreenState("rest", _borrowers_rest), ScreenState("empty", _borrowers_empty)),
    ),
    ScreenCase(
        "master_data_borrower_form",
        ("screens/master_data/borrower_form.html",),
        (ScreenState("create", _borrower_form_rest),),
    ),
    ScreenCase(
        "master_data_borrower_detail",
        ("screens/master_data/borrower_detail.html",),
        (ScreenState("rest", _borrower_detail_rest),),
    ),
    ScreenCase(
        "master_data_facilities",
        ("screens/master_data/facilities.html",),
        (ScreenState("rest", _facilities_rest), ScreenState("empty", _facilities_empty)),
    ),
    ScreenCase(
        "master_data_facility_insights",
        ("screens/master_data/facility_insights.html",),
        (
            ScreenState("rest", _facility_insights_rest),
            ScreenState("empty", _facility_insights_empty),
        ),
    ),
    ScreenCase(
        "master_data_facility_form",
        ("screens/master_data/facility_form.html",),
        (ScreenState("create", _facility_form_rest),),
    ),
    ScreenCase(
        "master_data_facility_detail",
        ("screens/master_data/facility_detail.html",),
        (ScreenState("rest", _facility_detail_rest),),
    ),
    ScreenCase(
        "master_data_portfolios",
        ("screens/master_data/portfolios.html",),
        (ScreenState("rest", _portfolios_rest), ScreenState("empty", _portfolios_empty)),
    ),
    ScreenCase(
        "master_data_portfolio_form",
        ("screens/master_data/portfolio_form.html",),
        (ScreenState("create", _portfolio_form_rest),),
    ),
    ScreenCase(
        "master_data_portfolio_detail",
        ("screens/master_data/portfolio_detail.html",),
        (ScreenState("rest", _portfolio_detail_rest),),
    ),
    ScreenCase(
        "covenants_list",
        ("screens/covenants/covenants.html",),
        (ScreenState("rest", _covenants_rest), ScreenState("empty", _covenants_empty)),
    ),
    ScreenCase(
        "covenant_form",
        ("screens/covenants/covenant_form.html",),
        (ScreenState("create", _covenant_form_rest),),
    ),
    ScreenCase(
        "covenant_detail",
        ("screens/covenants/covenant_detail.html",),
        (ScreenState("rest", _covenant_detail_rest),),
    ),
    ScreenCase(
        "covenant_approvals",
        ("screens/covenants/approvals.html",),
        (ScreenState("empty", _covenant_approvals_rest),),
    ),
    ScreenCase(
        "document_review",
        ("screens/documents/_review.html",),
        (ScreenState("rest", _document_review_rest), ScreenState("empty", _document_review_empty)),
    ),
    ScreenCase(
        "document_viewer",
        ("screens/documents/_viewer.html",),
        (ScreenState("rest", _document_viewer_rest),),
    ),
    ScreenCase(
        "why_panel",
        ("screens/why/panel.html", "screens/why/_stage.html"),
        (ScreenState("not_run", _why_not_run), ScreenState("model_decided", _why_model_decided)),
    ),
    ScreenCase(
        "why_drawer",
        ("screens/why/_drawer.html",),
        (ScreenState("rest", _why_drawer_fragment),),
        fragment=True,
    ),
    ScreenCase(
        "borrower_index",
        (
            "screens/borrower/index.html",
            "screens/borrower/_header.html",
            "screens/borrower/_covenants.html",
            "screens/borrower/_financials.html",
            "screens/borrower/_signals.html",
            "screens/borrower/_forecast.html",
            "screens/borrower/_horizon.html",
            "screens/borrower/_evidence.html",
            "screens/borrower/_documents.html",
            "screens/borrower/_actions.html",
        ),
        (
            ScreenState("rest", _borrower_index_rest),
            ScreenState("with_forecast", _borrower_index_with_forecast),
            ScreenState("with_financials", _borrower_index_financials),
            ScreenState("with_evidence", _borrower_index_evidence),
        ),
    ),
    ScreenCase(
        "borrower_memo",
        ("screens/borrower/_memo.html",),
        (ScreenState("not_configured", _borrower_memo_unavailable),),
        fragment=True,
    ),
    ScreenCase(
        "intake_index", ("screens/intake/index.html",), (ScreenState("passing", _intake_passing),)
    ),
    ScreenCase(
        "admin_catalogue",
        ("screens/admin/_catalogue.html",),
        (ScreenState("rest", _catalogue_rest), ScreenState("empty", _catalogue_empty)),
    ),
    ScreenCase(
        "admin_users",
        ("screens/admin/users/index.html",),
        (ScreenState("rest", _admin_users_rest),),
    ),
    ScreenCase(
        "admin_config",
        ("screens/admin/config/index.html",),
        (ScreenState("rest", _admin_config_rest),),
    ),
    ScreenCase(
        "admin_ops", ("screens/admin/ops/index.html",), (ScreenState("rest", _admin_ops_rest),)
    ),
    ScreenCase(
        "audit_index", ("screens/audit/index.html",), (ScreenState("rest", _audit_index_rest),)
    ),
    ScreenCase(
        "audit_reconstruction",
        ("screens/audit/reconstruction.html",),
        (ScreenState("rest", _audit_reconstruction_rest),),
    ),
    ScreenCase(
        "audit_bundle_status",
        ("screens/audit/bundle_status.html",),
        (ScreenState("rest", _audit_bundle_status_rest),),
    ),
    ScreenCase(
        "simulator_index",
        ("screens/simulator/index.html",),
        (ScreenState("rest", _simulator_rest),),
    ),
    ScreenCase(
        "statements_quarantine_review",
        ("screens/statements/_quarantine_review.html",),
        (ScreenState("empty", _quarantine_review_empty),),
    ),
    ScreenCase(
        "financial_statements_import",
        ("screens/statements/_import.html",),
        (ScreenState("rest", _financial_statements_import_rest),),
    ),
    ScreenCase(
        "statements_restate",
        ("screens/statements/_restate.html",),
        (ScreenState("rest", _restate_form_rest),),
    ),
    ScreenCase(
        "statements_restate_result",
        ("screens/statements/_restate_result.html",),
        (ScreenState("direct", _restate_result_direct),),
    ),
    ScreenCase(
        "queue_index",
        (
            "screens/queue/index.html",
            "screens/queue/_workspace.html",
            "screens/queue/_summary.html",
            "screens/queue/_filters.html",
            "screens/queue/_ledger.html",
            "screens/queue/_selection_bar.html",
        ),
        (
            ScreenState("rest", _queue_rest),
            ScreenState("filtered", _queue_filtered),
            ScreenState("empty_scope", _queue_empty_scope),
            ScreenState("no_run", _queue_no_run),
        ),
    ),
    ScreenCase(
        "notifications_index",
        ("screens/notifications/index.html",),
        (ScreenState("rest", _notifications_rest),),
    ),
    ScreenCase(
        "search_index",
        ("screens/search/index.html",),
        (ScreenState("rest", _search_rest),),
    ),
    ScreenCase(
        "bulk_result",
        ("screens/exports/bulk_result.html",),
        (ScreenState("complete", _bulk_result_rest),),
    ),
    ScreenCase(
        "export_status",
        ("screens/exports/status.html",),
        (ScreenState("running", _export_status_rest),),
    ),
    ScreenCase(
        "certificates_index",
        ("screens/certificates/index.html",),
        (ScreenState("rest", _certificates_rest), ScreenState("empty", _certificates_empty)),
    ),
    ScreenCase(
        "cases_index", ("screens/cases/index.html",), (ScreenState("rest", _cases_index_rest),)
    ),
    ScreenCase(
        "cases_detail", ("screens/cases/detail.html",), (ScreenState("rest", _cases_detail_rest),)
    ),
    ScreenCase(
        "governance_index",
        ("screens/governance/index.html",),
        (ScreenState("rest", _governance_rest),),
    ),
)


COVERED_TEMPLATES: frozenset[str] = frozenset(
    template for case in SCREENS for template in case.templates
)


__all__ = ["SCREENS", "COVERED_TEMPLATES", "ScreenCase", "ScreenState"]
