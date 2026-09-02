"""Integration coverage for `T-038`'s certificate request generation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import CovenantSchedule
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser, Role, UserPortfolioScope, UserRole
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import CertificateRequest
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.certificates.requirements import CERTIFICATE_TEST_BASIS
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.certificates import CertificateService
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
_AS_OF = date(2026, 6, 20)
_DUE_DATE = date(2026, 6, 30)
_LEAD_TIME_DAYS = 14


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: object,
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, dict(payload)))
        return object()


class _Bundle:
    """One portfolio, one borrower, one facility, and the plumbing to
    register live certificate-basis covenants and hand-place their
    `covenant_schedule` occurrences — the testing calendar's own generation
    (`T-035`) is out of scope for these tests, which exercise only what
    `T-038` does with occurrences that already exist.
    """

    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _RecordingAudit()
        self.principal = Principal.user(
            uuid4(), (Permission.VIEW_COVENANT, Permission.REGISTER_COVENANT)
        )

        portfolio = Portfolio.create(
            code="ROOT",
            name="Root portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t038-portfolio",
        )
        self.session.add(portfolio)
        self.session.flush()
        self.portfolio = portfolio
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="t038-user",
                email="t038-user@example.com",
                full_name="T038 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t038-user",
            )
        )
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [portfolio.path])

        self.borrower = Borrower(
            reference="B-T038",
            legal_name="T038 Certificate Borrower Private Limited",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t038-borrower",
        )
        self.session.add(self.borrower)
        self.session.flush()
        self.facility = Facility(
            reference="F-T038",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            outstanding=Decimal("700"),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t038-facility",
        )
        self.session.add(self.facility)
        self.session.flush()

        self.registry = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t038-registry",
            maker_checker_enabled=False,
        )
        self.service = CertificateService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t038-certificates",
            scope_resolver=lambda _principal: self.scope,
        )

    def register_covenant(
        self,
        reference: str,
        *,
        definition_ref: str = "leverage_ratio",
        frequency: str = "quarterly",
        test_basis: str = CERTIFICATE_TEST_BASIS,
    ):
        registered = self.registry.register(
            self.principal,
            facility_id=self.facility.id,
            reference=reference,
            name=f"{reference} covenant",
            covenant_class="financial",
            terms=CovenantVersionTerms(
                definition_ref=definition_ref,
                custom_formula=None,
                threshold=Decimal("2.5"),
                direction="max",
                unit="x",
                frequency=frequency,
                test_basis=test_basis,
                effective_from=date(2025, 1, 1),
            ),
            scope=self.scope,
        )
        return registered.version

    def add_schedule(
        self, covenant_version_id: UUID, *, due_date: date = _DUE_DATE, state: str = "due"
    ) -> CovenantSchedule:
        row = CovenantSchedule(
            id=uuid4(),
            covenant_version_id=covenant_version_id,
            due_date=due_date,
            state=state,
            test_id=None,
            certificate_id=None,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t038-schedule",
            created_by_id=self.principal.id,
            updated_by_id=self.principal.id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def add_contact(self, *, is_primary: bool = True) -> None:
        from covenant_radar.db.models.borrower import BorrowerContact

        self.session.add(
            BorrowerContact(
                borrower_id=self.borrower.id,
                name_enc="contact-enc",
                email_enc="contact@example.com",
                is_primary=is_primary,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t038-contact",
            )
        )
        self.session.flush()

    def add_relationship_manager(self) -> UUID:
        role = Role(
            code="relationship_manager",
            name="Relationship Manager",
            is_system=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t038-role",
        )
        self.session.add(role)
        self.session.flush()
        rm_user_id = uuid4()
        self.session.add(
            AppUser(
                id=rm_user_id,
                username="t038-rm",
                email="t038-rm@example.com",
                full_name="T038 RM",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t038-rm-user",
            )
        )
        self.session.flush()
        self.session.add(
            UserRole(
                user_id=rm_user_id,
                role_id=role.id,
                granted_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t038-rm-role",
            )
        )
        self.session.add(
            UserPortfolioScope(
                user_id=rm_user_id,
                portfolio_id=self.portfolio.id,
                include_descendants=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t038-rm-scope",
            )
        )
        self.session.flush()
        return rm_user_id

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_generation_idempotent() -> None:
    bundle = _Bundle()
    try:
        bundle.add_contact()
        version = bundle.register_covenant("CV-T038-A")
        bundle.add_schedule(version.id)

        first = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )
        second = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )

        assert len(first.raised) == 1
        assert second.raised == ()
        total = bundle.session.scalar(select(func.count(CertificateRequest.id)))
        assert total == 1
    finally:
        bundle.close()


def test_covenants_grouped_into_one_request() -> None:
    bundle = _Bundle()
    try:
        bundle.add_contact()
        leverage = bundle.register_covenant("CV-T038-LEV", definition_ref="leverage_ratio")
        utilisation = bundle.register_covenant("CV-T038-UTIL", definition_ref="utilisation")
        leverage_schedule = bundle.add_schedule(leverage.id)
        utilisation_schedule = bundle.add_schedule(utilisation.id)

        result = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )

        assert len(result.raised) == 1
        total = bundle.session.scalar(select(func.count(CertificateRequest.id)))
        assert total == 1

        bundle.session.refresh(leverage_schedule)
        bundle.session.refresh(utilisation_schedule)
        assert leverage_schedule.certificate_id == result.raised[0].id
        assert utilisation_schedule.certificate_id == result.raised[0].id
    finally:
        bundle.close()


def test_due_dates_match_calendar() -> None:
    bundle = _Bundle()
    try:
        bundle.add_contact()
        version = bundle.register_covenant("CV-T038-B")
        bundle.add_schedule(version.id, due_date=date(2026, 9, 30))

        result = bundle.service.generate(
            bundle.principal,
            as_of=date(2026, 9, 20),
            lead_time_days=_LEAD_TIME_DAYS,
            scope=bundle.scope,
        )

        assert len(result.raised) == 1
        assert result.raised[0].due_date == date(2026, 9, 30)
    finally:
        bundle.close()


def test_missing_contact_assigns_to_rm() -> None:
    bundle = _Bundle()
    try:
        rm_user_id = bundle.add_relationship_manager()
        version = bundle.register_covenant("CV-T038-C")
        bundle.add_schedule(version.id)

        result = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )

        assert len(result.raised) == 1
        assert result.raised[0].state == "requested"

        raised_events = [
            event for event in bundle.audit.events if event[0] == "certificate_request_raised"
        ]
        assert len(raised_events) == 1
        payload = raised_events[0][2]
        assert payload["contact_id"] is None
        assert payload["notification_target"] == "relationship_manager"
        assert str(rm_user_id) in payload["notified_user_ids"]
    finally:
        bundle.close()


def test_retired_covenant_cancels_request() -> None:
    bundle = _Bundle()
    try:
        bundle.add_contact()
        version = bundle.register_covenant("CV-T038-D")
        bundle.add_schedule(version.id)

        first = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )
        assert len(first.raised) == 1
        request_id = first.raised[0].id

        bundle.registry.retire(bundle.principal, "CV-T038-D", scope=bundle.scope)

        second = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )

        assert len(second.cancelled) == 1
        assert second.cancelled[0].id == request_id
        assert second.cancelled[0].state == "rejected"
        assert second.cancelled[0].rejection_reason is not None
        assert "retired" in second.cancelled[0].rejection_reason

        stored = bundle.session.get(CertificateRequest, request_id)
        assert stored is not None
        assert stored.state == "rejected"
    finally:
        bundle.close()


def test_lead_time_longer_than_frequency_refused_at_generation() -> None:
    bundle = _Bundle()
    try:
        bundle.add_contact()
        version = bundle.register_covenant("CV-T038-E", frequency="quarterly")
        bundle.add_schedule(version.id)

        from covenant_radar.core.errors import ValidationError

        with pytest.raises(ValidationError):
            bundle.service.generate(
                bundle.principal, as_of=_AS_OF, lead_time_days=90, scope=bundle.scope
            )

        # Nothing was persisted by the refused call.
        total = bundle.session.scalar(select(func.count(CertificateRequest.id)))
        assert total == 0
    finally:
        bundle.close()


def test_non_certificate_covenant_produces_no_request() -> None:
    bundle = _Bundle()
    try:
        bundle.add_contact()
        version = bundle.register_covenant("CV-T038-F", test_basis="standalone")
        bundle.add_schedule(version.id)

        result = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )

        assert result.raised == ()
        total = bundle.session.scalar(select(func.count(CertificateRequest.id)))
        assert total == 0
    finally:
        bundle.close()
