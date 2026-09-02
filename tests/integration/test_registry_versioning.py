"""Integration coverage for `T-031`'s covenant registry: `register`,
`amend`, `live_at`, and the immutability rule's three enforcement points
(`plan.md §5.5`, `spec §R-05.a`).

Runs entirely against an in-memory SQLite database — the same technique
`tests/integration/test_master_data.py` uses. `db/models/covenant.py`
writes its immutability trigger for both SQLite and PostgreSQL, and
`tests/unit/test_model_domain.py` already proves the trigger itself against
SQLite in isolation; this file proves the trigger again here specifically
because it is one of the two facts `test_trigger_refuses_direct_update` and
`test_repository_has_no_update_for_frozen_columns` must show *together*, on
a row this task's own service produced.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.covenant import CovenantVersionRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


class _Audit:
    """A minimal, in-memory stand-in for the `C-60` audit port."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object, str]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, payload, actor, request_id))
        return object()


class _Bundle:
    """One in-memory database, one registered facility, one registry
    service — the fixture every test below builds on."""

    def __init__(self, *, maker_checker_enabled: bool = False) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.principal = Principal.user(
            uuid4(), (Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT)
        )

        portfolio = Portfolio.create(
            code="ROOT",
            name="Root",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-0000000000000001",
        )
        self.session.add(portfolio)
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [portfolio.path])

        borrower = Borrower(
            reference="B-000001",
            legal_name="Acme Pvt Ltd",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-0000000000000002",
        )
        self.session.add(borrower)
        self.session.flush()

        self.facility = Facility(
            reference="F-000001",
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-0000000000000003",
        )
        self.session.add(self.facility)
        self.session.flush()

        # `covenant_version.registered_by_id` is a required FK to
        # `app_user`, so the principal registering covenants must be a real
        # row, sharing its id with `self.principal`.
        user = AppUser(
            id=self.principal.id,
            username="maker",
            email="maker@example.com",
            full_name="Maker",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-0000000000000004",
        )
        self.session.add(user)
        self.session.flush()

        self.service = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t031-test-0000001",
            scope_resolver=lambda _principal: self.scope,
            maker_checker_enabled=maker_checker_enabled,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def terms(self, **overrides: object) -> CovenantVersionTerms:
        values: dict[str, object] = {
            "definition_ref": "leverage_ratio",
            "custom_formula": None,
            "threshold": Decimal("2.5"),
            "direction": "max",
            "unit": "x",
            "frequency": "quarterly",
            "test_basis": "standalone",
            "effective_from": date(2026, 1, 1),
        }
        values.update(overrides)
        return CovenantVersionTerms(**values)


def test_amend_creates_version_old_intact() -> None:
    bundle = _Bundle()
    try:
        registered = bundle.service.register(
            bundle.principal,
            facility_id=bundle.facility.id,
            reference="CV-000001",
            name="Leverage covenant",
            covenant_class="financial",
            terms=bundle.terms(effective_from=date(2026, 1, 1)),
            scope=bundle.scope,
        )
        original_id = registered.version.id
        original_threshold = registered.version.threshold

        amended = bundle.service.amend(
            bundle.principal,
            "CV-000001",
            terms=bundle.terms(
                threshold=Decimal("3.0"),
                effective_from=date(2026, 4, 1),
            ),
            scope=bundle.scope,
        )

        # The old version is intact: same id, same threshold, only status
        # and effective_to changed.
        assert amended.previous_version.id == original_id
        assert amended.previous_version.threshold == original_threshold
        assert amended.previous_version.status == "superseded"
        assert amended.previous_version.effective_to == date(2026, 4, 1)

        # The new version is a distinct row, one number higher, live from
        # exactly where the old one closed.
        assert amended.version.id != original_id
        assert amended.version.version_no == amended.previous_version.version_no + 1
        assert amended.version.threshold == Decimal("3.0")
        assert amended.version.effective_from == date(2026, 4, 1)
        assert amended.version.status == "live"

        versions = bundle.service.versions.for_covenant(registered.covenant.id, scope=bundle.scope)
        assert [version.version_no for version in versions] == [1, 2]
    finally:
        bundle.close()


def test_historical_test_references_old_version() -> None:
    bundle = _Bundle()
    try:
        registered = bundle.service.register(
            bundle.principal,
            facility_id=bundle.facility.id,
            reference="CV-000001",
            name="Leverage covenant",
            covenant_class="financial",
            terms=bundle.terms(effective_from=date(2026, 1, 1)),
            scope=bundle.scope,
        )
        old_version_id = registered.version.id

        # A test was recorded against version 1 before it was ever amended
        # — the ordinary path, since a covenant is usually tested many
        # times before its terms are next revisited.
        historical_test = CovenantTest(
            id=new_id(),
            covenant_version_id=old_version_id,
            as_of_date=date(2026, 3, 31),
            value=Decimal("2.10"),
            threshold_used=Decimal("2.5"),
            headroom_pct=Decimal("16.0000"),
            verdict="pass",
            computed_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-0000000000000005",
        )
        bundle.session.add(historical_test)
        bundle.session.flush()

        # The version that test was computed against is marked tested —
        # the trigger now protects its terms — and only then is it amended.
        registered.version.tested_at_least_once = True
        bundle.session.commit()

        bundle.service.amend(
            bundle.principal,
            "CV-000001",
            terms=bundle.terms(threshold=Decimal("3.0"), effective_from=date(2026, 4, 1)),
            scope=bundle.scope,
        )

        bundle.session.expire_all()
        reloaded_test = bundle.session.get(CovenantTest, historical_test.id)
        assert reloaded_test is not None
        assert reloaded_test.covenant_version_id == old_version_id

        reloaded_old_version = bundle.session.get(CovenantVersion, old_version_id)
        assert reloaded_old_version is not None
        assert reloaded_old_version.threshold == Decimal("2.50000000")
        assert reloaded_old_version.status == "superseded"
    finally:
        bundle.close()


def test_repository_has_no_update_for_frozen_columns() -> None:
    # No general-purpose update/save method exists at all: the repository
    # cannot be asked to change a version's terms, only to add a new row or
    # close an existing one through the one narrow helper below.
    for forbidden_name in ("update", "save", "set", "patch", "update_terms"):
        assert not hasattr(CovenantVersionRepository, forbidden_name)

    bundle = _Bundle()
    try:
        registered = bundle.service.register(
            bundle.principal,
            facility_id=bundle.facility.id,
            reference="CV-000001",
            name="Leverage covenant",
            covenant_class="financial",
            terms=bundle.terms(),
            scope=bundle.scope,
        )
        version = registered.version
        before = {
            column.name: getattr(version, column.name)
            for column in CovenantVersion.__table__.columns
            if column.name not in {"status", "effective_to", "updated_at", "version"}
        }

        bundle.service.versions.close_and_supersede(version, effective_to=date(2026, 6, 1))
        bundle.session.flush()

        # `close_and_supersede` touched exactly `status` and `effective_to`
        # — every other column, including every one the immutability
        # trigger protects, is untouched.
        after = {
            column.name: getattr(version, column.name)
            for column in CovenantVersion.__table__.columns
            if column.name not in {"status", "effective_to", "updated_at", "version"}
        }
        assert before == after
        assert version.status == "superseded"
        assert version.effective_to == date(2026, 6, 1)
    finally:
        bundle.close()


def test_trigger_refuses_direct_update() -> None:
    bundle = _Bundle()
    try:
        registered = bundle.service.register(
            bundle.principal,
            facility_id=bundle.facility.id,
            reference="CV-000001",
            name="Leverage covenant",
            covenant_class="financial",
            terms=bundle.terms(),
            scope=bundle.scope,
        )
        version = registered.version

        # Not yet tested: the repository still offers no update method, but
        # a direct ORM mutation is not yet refused by the trigger either.
        version.threshold = Decimal("9.99")
        bundle.session.commit()

        version.tested_at_least_once = True
        bundle.session.commit()

        # Now frozen. A stray write that never went near the registry
        # service or its repository — run "from anywhere", exactly as
        # `db/models/covenant.py` promises — is refused by the trigger
        # itself, the enforcement point beneath the repository's own
        # absence of an update method.
        version.threshold = Decimal("1.00")
        with pytest.raises(IntegrityError, match="immutable"):
            bundle.session.commit()
        bundle.session.rollback()

        bundle.session.expire_all()
        reloaded = bundle.session.get(CovenantVersion, version.id)
        assert reloaded is not None
        assert reloaded.threshold == Decimal("9.99000000")
    finally:
        bundle.close()


def test_overlapping_ranges_refused() -> None:
    bundle = _Bundle()
    try:
        bundle.service.register(
            bundle.principal,
            facility_id=bundle.facility.id,
            reference="CV-000001",
            name="Leverage covenant",
            covenant_class="financial",
            terms=bundle.terms(effective_from=date(2026, 1, 1)),
            scope=bundle.scope,
        )
        bundle.service.amend(
            bundle.principal,
            "CV-000001",
            terms=bundle.terms(threshold=Decimal("3.0"), effective_from=date(2026, 4, 1)),
            scope=bundle.scope,
        )

        # Version 2 has been live from 2026-04-01. Backdating a third
        # version to start before that overlaps version 2's own range and
        # must be refused, naming both the version it collides with and the
        # one being proposed, with nothing written.
        with pytest.raises(Conflict) as exc_info:
            bundle.service.amend(
                bundle.principal,
                "CV-000001",
                terms=bundle.terms(threshold=Decimal("4.0"), effective_from=date(2026, 2, 1)),
                scope=bundle.scope,
            )
        message = str(exc_info.value)
        assert "version 2" in message
        assert "version 3" in message

        versions = bundle.service.versions.for_covenant(
            bundle.service.covenants.by_reference("CV-000001", scope=bundle.scope).id,
            scope=bundle.scope,
        )
        assert [version.version_no for version in versions] == [1, 2]
    finally:
        bundle.close()


def test_live_at_before_first_version_is_empty() -> None:
    bundle = _Bundle()
    try:
        bundle.service.register(
            bundle.principal,
            facility_id=bundle.facility.id,
            reference="CV-000001",
            name="Leverage covenant",
            covenant_class="financial",
            terms=bundle.terms(effective_from=date(2026, 1, 1)),
            scope=bundle.scope,
        )

        before_any_version = bundle.service.live_at(
            bundle.principal, bundle.facility.id, date(2020, 1, 1), scope=bundle.scope
        )
        assert before_any_version == ()

        on_and_after_registration = bundle.service.live_at(
            bundle.principal, bundle.facility.id, date(2026, 1, 1), scope=bundle.scope
        )
        assert len(on_and_after_registration) == 1
        assert on_and_after_registration[0].version_no == 1
    finally:
        bundle.close()
