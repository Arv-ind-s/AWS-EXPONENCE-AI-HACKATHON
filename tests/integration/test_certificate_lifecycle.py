"""Integration coverage for `T-039`'s certificate receipt, review and
overdue-evidence lifecycle."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict, NotFound
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower, BorrowerContact
from covenant_radar.db.models.covenant import CovenantSchedule
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import CertificateRequest, EvidenceItem
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.certificates.requirements import CERTIFICATE_TEST_BASIS
from covenant_radar.domain.covenants.evaluate import PeriodFacts
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.domain.ratios.compute import RatioResult
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.certificates import CertificateService
from covenant_radar.services.engine import EngineService
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
_AS_OF = date(2026, 6, 20)
_DUE_DATE = date(2026, 6, 30)
_LEAD_TIME_DAYS = 14
_GRACE_DAYS = 5


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
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _RecordingAudit()
        self.principal = Principal.user(
            uuid4(),
            (
                Permission.VIEW_COVENANT,
                Permission.REGISTER_COVENANT,
                Permission.UPLOAD_DOCUMENT,
                Permission.RECORD_WAIVER,
                Permission.INGEST_DATA,
            ),
        )

        portfolio = Portfolio.create(
            code="ROOT",
            name="Root portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t039-portfolio",
        )
        self.session.add(portfolio)
        self.session.flush()
        self.portfolio = portfolio
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="t039-user",
                email="t039-user@example.com",
                full_name="T039 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t039-user",
            )
        )
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [portfolio.path])

        self.borrower = Borrower(
            reference="B-T039",
            legal_name="T039 Certificate Borrower Private Limited",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t039-borrower",
        )
        self.session.add(self.borrower)
        self.session.flush()
        self.session.add(
            BorrowerContact(
                borrower_id=self.borrower.id,
                name_enc="contact-enc",
                email_enc="contact@example.com",
                is_primary=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t039-contact",
            )
        )
        self.facility = Facility(
            reference="F-T039",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            outstanding=Decimal("700"),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t039-facility",
        )
        self.session.add(self.facility)
        self.session.flush()

        self.registry = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t039-registry",
            maker_checker_enabled=False,
        )
        self.engine_service = EngineService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t039-engine",
            scope_resolver=lambda _principal: self.scope,
        )
        self.service = CertificateService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t039-certificates",
            scope_resolver=lambda _principal: self.scope,
        )

    def register_covenant(self, reference: str, *, definition_ref: str = "leverage_ratio"):
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
                frequency="quarterly",
                test_basis=CERTIFICATE_TEST_BASIS,
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
            request_id="rq-t039-schedule",
            created_by_id=self.principal.id,
            updated_by_id=self.principal.id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def add_document(self) -> Document:
        document = Document(
            id=uuid4(),
            borrower_id=self.borrower.id,
            doc_type="compliance_certificate",
            filename="certificate.pdf",
            content_hash=uuid4().hex,
            byte_size=1024,
            mime_type="application/pdf",
            storage_key=f"documents/{uuid4().hex}.pdf",
            uploaded_by_id=self.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t039-document",
        )
        self.session.add(document)
        self.session.flush()
        return document

    def test_covenant(self, version_id: UUID, *, as_of: date = _DUE_DATE):
        return self.engine_service.test(
            self.principal,
            covenant_version_id=version_id,
            period=PeriodFacts(period_label="FY26Q4", as_of_date=as_of),
            ratio=RatioResult(
                code="leverage_ratio",
                value=Decimal("2.0"),
                computable=True,
                reason=None,
                inputs_used={
                    "total_debt": Decimal("500"),
                    "tangible_net_worth": Decimal("250"),
                },
                band_breached=False,
            ),
            scope=self.scope,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_acceptance_links_to_consuming_tests() -> None:
    bundle = _Bundle()
    try:
        version = bundle.register_covenant("CV-T039-A")
        bundle.add_schedule(version.id)
        generated = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )
        request_id = generated.raised[0].id
        document = bundle.add_document()

        bundle.service.receive(
            bundle.principal, request_id, document_id=document.id, scope=bundle.scope
        )
        accepted = bundle.service.accept(bundle.principal, request_id, scope=bundle.scope)
        assert accepted.state == "accepted"

        bundle.test_covenant(version.id)

        schedule = bundle.session.scalar(
            select(CovenantSchedule).where(CovenantSchedule.covenant_version_id == version.id)
        )
        assert schedule is not None
        assert schedule.certificate_id == request_id
        assert schedule.test_id is not None

        stored_request = bundle.session.get(CertificateRequest, request_id)
        assert stored_request is not None
        assert stored_request.document_id == document.id
    finally:
        bundle.close()


def test_rejection_unlinks_and_flags_recomputation() -> None:
    bundle = _Bundle()
    try:
        version = bundle.register_covenant("CV-T039-B")
        original_schedule = bundle.add_schedule(version.id)
        generated = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )
        request_id = generated.raised[0].id
        document = bundle.add_document()

        bundle.service.receive(
            bundle.principal, request_id, document_id=document.id, scope=bundle.scope
        )
        bundle.test_covenant(version.id)
        bundle.service.accept(bundle.principal, request_id, scope=bundle.scope)

        rejected = bundle.service.reject(
            bundle.principal,
            request_id,
            reason="Certificate covers the wrong period.",
            scope=bundle.scope,
        )

        assert rejected.state == "rejected"
        assert rejected.rejection_reason == "Certificate covers the wrong period."

        bundle.session.refresh(original_schedule)
        assert original_schedule.certificate_id is None

        recomputation_rows = bundle.session.scalars(
            select(CovenantSchedule).where(
                CovenantSchedule.covenant_version_id == version.id,
                CovenantSchedule.state == "due",
                CovenantSchedule.id != original_schedule.id,
            )
        ).all()
        assert len(recomputation_rows) == 1

        event_types = {event[0] for event in bundle.audit.events}
        assert "certificate_accepted" in event_types
        assert "certificate_rejected" in event_types
    finally:
        bundle.close()


def test_late_receipt_flags_prior_test() -> None:
    bundle = _Bundle()
    try:
        version = bundle.register_covenant("CV-T039-C")
        schedule = bundle.add_schedule(version.id)
        generated = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )
        request_id = generated.raised[0].id

        # The covenant is tested before the certificate ever arrives.
        bundle.test_covenant(version.id)
        bundle.session.refresh(schedule)
        assert schedule.state == "tested"
        assert schedule.test_id is not None

        document = bundle.add_document()
        bundle.service.receive(
            bundle.principal, request_id, document_id=document.id, scope=bundle.scope
        )

        recomputation_rows = bundle.session.scalars(
            select(CovenantSchedule).where(
                CovenantSchedule.covenant_version_id == version.id,
                CovenantSchedule.state == "due",
                CovenantSchedule.id != schedule.id,
            )
        ).all()
        assert len(recomputation_rows) == 1
    finally:
        bundle.close()


def test_overdue_creates_evidence_item() -> None:
    bundle = _Bundle()
    try:
        version = bundle.register_covenant("CV-T039-D")
        bundle.add_schedule(version.id)
        bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )

        overdue = bundle.service.sweep_overdue(
            bundle.principal,
            as_of=date(2026, 7, 20),
            grace_days=_GRACE_DAYS,
            scope=bundle.scope,
        )

        assert len(overdue) == 1
        assert overdue[0].state == "overdue"

        item = bundle.session.scalar(
            select(EvidenceItem).where(
                EvidenceItem.borrower_id == bundle.borrower.id,
                EvidenceItem.evidence_type == "certificate_overdue",
            )
        )
        assert item is not None
        assert item.state in {"transient", "sustained"}
    finally:
        bundle.close()


def test_satisfied_overdue_supersedes_not_deletes() -> None:
    bundle = _Bundle()
    try:
        version = bundle.register_covenant("CV-T039-E")
        bundle.add_schedule(version.id)
        generated = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )
        request_id = generated.raised[0].id
        bundle.service.sweep_overdue(
            bundle.principal,
            as_of=date(2026, 7, 20),
            grace_days=_GRACE_DAYS,
            scope=bundle.scope,
        )

        original = bundle.session.scalar(
            select(EvidenceItem).where(
                EvidenceItem.borrower_id == bundle.borrower.id,
                EvidenceItem.evidence_type == "certificate_overdue",
            )
        )
        assert original is not None
        original_id = original.id

        document = bundle.add_document()
        received = bundle.service.receive(
            bundle.principal, request_id, document_id=document.id, scope=bundle.scope
        )
        assert received.state == "received"

        bundle.session.refresh(original)
        assert original.state == "superseded"
        assert original.superseded_by_id is not None

        # Retained, never deleted.
        still_present = bundle.session.get(EvidenceItem, original_id)
        assert still_present is not None

        successor = bundle.session.get(EvidenceItem, original.superseded_by_id)
        assert successor is not None
        assert successor.evidence_type == "certificate_satisfied"

        total_certificate_related = bundle.session.scalar(
            select(func.count(EvidenceItem.id)).where(
                EvidenceItem.borrower_id == bundle.borrower.id
            )
        )
        assert total_certificate_related == 2
    finally:
        bundle.close()


def test_one_document_covers_several_requests() -> None:
    bundle = _Bundle()
    try:
        first_version = bundle.register_covenant("CV-T039-F1")
        second_version = bundle.register_covenant("CV-T039-F2")
        bundle.add_schedule(first_version.id, due_date=date(2026, 6, 30))
        bundle.add_schedule(second_version.id, due_date=date(2026, 9, 30))

        first_generation = bundle.service.generate(
            bundle.principal, as_of=date(2026, 6, 20), lead_time_days=14, scope=bundle.scope
        )
        second_generation = bundle.service.generate(
            bundle.principal, as_of=date(2026, 9, 20), lead_time_days=14, scope=bundle.scope
        )
        first_request_id = first_generation.raised[0].id
        second_request_id = second_generation.raised[0].id
        assert first_request_id != second_request_id

        document = bundle.add_document()

        first_received = bundle.service.receive(
            bundle.principal, first_request_id, document_id=document.id, scope=bundle.scope
        )
        second_received = bundle.service.receive(
            bundle.principal, second_request_id, document_id=document.id, scope=bundle.scope
        )

        assert first_received.document_id == document.id
        assert second_received.document_id == document.id

        linked_count = bundle.session.scalar(
            select(func.count(CertificateRequest.id)).where(
                CertificateRequest.document_id == document.id
            )
        )
        assert linked_count == 2
    finally:
        bundle.close()


def test_receive_refuses_unknown_document() -> None:
    bundle = _Bundle()
    try:
        version = bundle.register_covenant("CV-T039-G")
        bundle.add_schedule(version.id)
        generated = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )
        request_id = generated.raised[0].id

        with pytest.raises(NotFound):
            bundle.service.receive(
                bundle.principal, request_id, document_id=uuid4(), scope=bundle.scope
            )
    finally:
        bundle.close()


def test_accept_refuses_from_requested_state() -> None:
    bundle = _Bundle()
    try:
        version = bundle.register_covenant("CV-T039-H")
        bundle.add_schedule(version.id)
        generated = bundle.service.generate(
            bundle.principal, as_of=_AS_OF, lead_time_days=_LEAD_TIME_DAYS, scope=bundle.scope
        )
        request_id = generated.raised[0].id

        with pytest.raises(Conflict):
            bundle.service.accept(bundle.principal, request_id, scope=bundle.scope)
    finally:
        bundle.close()
