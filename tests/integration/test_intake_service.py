"""Integration coverage for `T-096`: proposal persistence, correction,
abandonment, duplicate-document detection, amendment offering and audit —
`spec §R-06`, `C-05`, `C-06`.

Uses the same self-contained, in-memory SQLite bundle
`tests/integration/test_registry_service.py` already established for this
area, rather than the `COVENANT_RADAR_DATABASE_URL`-requiring PostgreSQL
fixture: `IntakeService` composes `RegistryService` directly, and neither
depends on anything PostgreSQL-specific.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict, NotFound
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.intake import CovenantProposal
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.intake.candidates import CandidateLine, ClauseCandidate
from covenant_radar.domain.intake.proposal import StageOneProposal, parse_stage1_reply
from covenant_radar.domain.intake.verify import VerificationContext
from covenant_radar.domain.ratios.definitions import FacilityFacts
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.intake import IntakeService, ProposalRecord, ProposedClause
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_SANCTION_DATE = date(2026, 1, 1)

_BASE_REPLY: dict[str, object] = {
    "definition": "dscr",
    "custom_formula": None,
    "threshold": "1.5x",
    "direction": "above",
    "unit": "ratio",
    "currency": None,
    "frequency": "quarterly",
    "effective_from": "2026-04-01",
    "effective_to": None,
    "exceptions": [],
    "cure_period_days": 90,
    "source_quote": "DSCR shall not fall below 1.5 times, tested quarterly.",
}


def _candidate(text: str) -> ClauseCandidate:
    line = CandidateLine(page_number=1, start_offset=0, end_offset=len(text), text=text)
    return ClauseCandidate(
        start_page=1,
        start_offset=0,
        end_page=1,
        end_offset=len(text),
        text=text,
        matched_rules=("ratio:dscr", "threshold:0"),
        lines=(line,),
    )


def _proposal(**overrides: object) -> StageOneProposal:
    payload = {**_BASE_REPLY, **overrides}
    text = str(payload["source_quote"])
    return parse_stage1_reply(_candidate(text), json.dumps(payload))


def _context(**overrides: object) -> VerificationContext:
    values: dict[str, object] = {
        "statement_lines": {
            "cash_flow_debt_service": Decimal("150"),
            "finance_cost": Decimal("100"),
        },
        "period_complete": True,
        "facility_facts": FacilityFacts(),
        "facility_sanction_date": _SANCTION_DATE,
        "facility_currency": "INR",
    }
    values.update(overrides)
    return VerificationContext(**values)  # type: ignore[arg-type]


class _Audit:
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

    def event_types(self) -> list[str]:
        return [event[0] for event in self.events]


class _Bundle:
    def __init__(self, *, maker_checker_enabled: bool = False) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.officer = Principal.user(
            uuid4(),
            (Permission.RUN_INTAKE, Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT),
        )
        self.scopes: dict[UUID, Scope] = {}
        self.portfolio = self._add_portfolio("ROOT")
        self._add_user(self.officer, "officer")
        self.scopes[self.officer.id] = Scope.from_paths(self.officer.id, [self.portfolio.path])
        self.facility = self._add_facility(self.portfolio, "000001")
        self.registry = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t096-registry-000001",
            scope_resolver=lambda principal: self.scopes[principal.id],
            maker_checker_enabled=maker_checker_enabled,
        )
        self.service = IntakeService(
            self.session,
            audit=self.audit,
            registry=self.registry,
            clock=FixedClock(_NOW),
            request_id="rq-t096-intake-000001",
            scope_resolver=lambda principal: self.scopes[principal.id],
        )

    def _add_portfolio(self, code: str) -> Portfolio:
        portfolio = Portfolio.create(
            code=code,
            name=f"Portfolio {code}",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-portfolio-{code.lower()}",
        )
        self.session.add(portfolio)
        self.session.flush()
        return portfolio

    def _add_user(self, principal: Principal, username: str) -> None:
        self.session.add(
            AppUser(
                id=principal.id,
                username=username,
                email=f"{username}@example.com",
                full_name=username.title(),
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-user-{username}",
            )
        )
        self.session.flush()

    def _add_facility(self, portfolio: Portfolio, suffix: str) -> Facility:
        borrower = Borrower(
            reference=f"B-{suffix}",
            legal_name=f"Borrower {suffix}",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-borrower-{suffix}",
        )
        self.session.add(borrower)
        self.session.flush()
        facility = Facility(
            reference=f"F-{suffix}",
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=_SANCTION_DATE,
            effective_from=_SANCTION_DATE,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-facility-{suffix}",
        )
        self.session.add(facility)
        self.session.flush()
        return facility

    def add_document(self, suffix: str = "000001") -> Document:
        document = Document(
            id=new_id(),
            borrower_id=self.facility.borrower_id,
            facility_id=self.facility.id,
            doc_type="sanction_letter",
            filename=f"sanction-{suffix}.pdf",
            content_hash=f"hash-{suffix}",
            byte_size=1024,
            mime_type="application/pdf",
            storage_key=f"documents/{suffix}",
            uploaded_by_id=self.officer.id,
            created_at=_NOW,
            updated_at=_NOW,
            created_by_id=self.officer.id,
            updated_by_id=self.officer.id,
            request_id=f"rq-document-{suffix}",
        )
        self.session.add(document)
        self.session.flush()
        return document

    def scope(self) -> Scope:
        return self.scopes[self.officer.id]

    def propose(self, *, document_id: UUID | None = None, **overrides: object) -> ProposalRecord:
        clause = ProposedClause(proposal=_proposal(**overrides))
        records = self.service.propose_from_document(
            self.officer,
            facility_id=self.facility.id,
            clauses=(clause,),
            context=_context(),
            document_id=document_id,
            scope=self.scope(),
        )
        return records[0]

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_correction_reverifies_all_six() -> None:
    bundle = _Bundle()
    try:
        record = bundle.propose(threshold="50x")
        assert record.outcome.all_passed is False
        assert "threshold_plausible" in record.outcome.failed_checks
        original_version = record.row.version

        corrected = bundle.service.correct(
            bundle.officer,
            record.row.id,
            proposal=_proposal(threshold="1.5x"),
            context=_context(),
            scope=bundle.scope(),
        )
        assert len(corrected.outcome.verification.checks) == 6
        assert corrected.outcome.all_passed is True
        assert corrected.row.version == original_version + 1

        [corrected_event] = [
            event for event in bundle.audit.events if event[0] == "intake_proposal_corrected"
        ]
        assert corrected_event[2]["all_checks_rerun"] is True
    finally:
        bundle.close()


def test_existing_covenant_offered_as_amendment() -> None:
    bundle = _Bundle(maker_checker_enabled=False)
    try:
        first = bundle.propose(effective_from="2026-01-15")
        assert first.outcome.all_passed is True
        registered = bundle.service.submit(
            bundle.officer,
            first.row.id,
            test_basis="standalone",
            reference="CV-000001",
            name="DSCR covenant",
            covenant_class="financial",
        )
        assert registered.was_amendment is False
        assert registered.version.version_no == 1

        second = bundle.propose(effective_from="2026-04-01")
        target = bundle.service.find_amendment_target(
            bundle.officer, second.row.id, scope=bundle.scope()
        )
        assert target is not None
        assert target.id == registered.covenant.id

        amended = bundle.service.submit(bundle.officer, second.row.id, test_basis="standalone")
        assert amended.was_amendment is True
        assert amended.covenant.id == registered.covenant.id
        assert amended.version.version_no == 2
    finally:
        bundle.close()


def test_abandoned_proposal_retained() -> None:
    bundle = _Bundle()
    try:
        record = bundle.propose(threshold="50x")
        proposal_id = record.row.id

        abandoned = bundle.service.abandon(
            bundle.officer,
            proposal_id,
            reason="Superseded by a corrected re-read.",
            scope=bundle.scope(),
        )
        assert abandoned.row.status == "abandoned"
        assert abandoned.row.abandon_reason == "Superseded by a corrected re-read."
        assert len(abandoned.outcome.verification.checks) == 6

        still_there = bundle.session.get(CovenantProposal, proposal_id)
        assert still_there is not None
        assert still_there.status == "abandoned"

        with pytest.raises(Conflict):
            bundle.service.abandon(bundle.officer, proposal_id, scope=bundle.scope())
    finally:
        bundle.close()


def test_resubmitted_document_shows_prior_proposals() -> None:
    bundle = _Bundle()
    try:
        document = bundle.add_document()
        first = bundle.propose(document_id=document.id, threshold="1.5x")

        again = bundle.service.propose_from_document(
            bundle.officer,
            facility_id=bundle.facility.id,
            clauses=(ProposedClause(proposal=_proposal(threshold="2.0x")),),
            context=_context(),
            document_id=document.id,
            scope=bundle.scope(),
        )
        assert [record.row.id for record in again] == [first.row.id]

        count = bundle.session.execute(
            select(CovenantProposal).where(CovenantProposal.document_id == document.id)
        ).scalars().all()
        assert len(count) == 1

        reextracted = bundle.service.propose_from_document(
            bundle.officer,
            facility_id=bundle.facility.id,
            clauses=(ProposedClause(proposal=_proposal(threshold="2.0x")),),
            context=_context(),
            document_id=document.id,
            force_reextraction=True,
            scope=bundle.scope(),
        )
        assert reextracted[0].row.id != first.row.id

        count_after = bundle.session.execute(
            select(CovenantProposal).where(CovenantProposal.document_id == document.id)
        ).scalars().all()
        assert len(count_after) == 2
    finally:
        bundle.close()


def test_every_step_audited() -> None:
    bundle = _Bundle()
    try:
        created = bundle.propose(threshold="50x")
        bundle.service.correct(
            bundle.officer,
            created.row.id,
            proposal=_proposal(threshold="1.5x"),
            context=_context(),
            scope=bundle.scope(),
        )
        bundle.service.submit(
            bundle.officer,
            created.row.id,
            test_basis="standalone",
            reference="CV-000002",
            name="DSCR covenant",
            covenant_class="financial",
        )

        abandon_target = bundle.propose(threshold="50x", effective_from="2026-05-01")
        bundle.service.abandon(bundle.officer, abandon_target.row.id, scope=bundle.scope())

        event_types = bundle.audit.event_types()
        for expected in (
            "intake_proposal_created",
            "intake_proposal_corrected",
            "intake_proposal_confirmed",
            "intake_proposal_abandoned",
        ):
            assert expected in event_types, f"{expected} was not audited"
    finally:
        bundle.close()


def test_correct_requires_open_status() -> None:
    bundle = _Bundle()
    try:
        record = bundle.propose(threshold="1.5x")
        bundle.service.submit(
            bundle.officer,
            record.row.id,
            test_basis="standalone",
            reference="CV-000003",
            name="DSCR covenant",
            covenant_class="financial",
        )
        with pytest.raises(Conflict):
            bundle.service.correct(
                bundle.officer,
                record.row.id,
                proposal=_proposal(threshold="1.6x"),
                context=_context(),
                scope=bundle.scope(),
            )
    finally:
        bundle.close()


def test_submit_unknown_proposal_not_found() -> None:
    bundle = _Bundle()
    try:
        with pytest.raises(NotFound):
            bundle.service.submit(bundle.officer, uuid4(), test_basis="standalone")
    finally:
        bundle.close()
