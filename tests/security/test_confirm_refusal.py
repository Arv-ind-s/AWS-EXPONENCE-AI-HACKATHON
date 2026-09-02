"""Security coverage for `T-096`'s structural refusal - `spec §16.1`:
confirming a covenant that failed verification is permitted to **no role in
any configuration**. `spec §R-06.b`: a direct request to confirm a failed
proposal returns a refusal naming the failed check, regardless of role,
session or API key.

Every role in `spec §16.1`'s access matrix is exercised here, each with
exactly the permissions that row actually grants it - including every role
that *can* register or approve a covenant, which is the meaningful proof:
holding every permission that exists still does not open a path to
confirming a proposal the six code verifications and the injection scan did
not pass.
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
from covenant_radar.core.errors import AuthorizationError
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant
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
from covenant_radar.services.intake import IntakeService, ProposalVerificationFailed, ProposedClause
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.security

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_SANCTION_DATE = date(2026, 1, 1)

# `spec §16.1`'s access matrix, row-accurate: every permission each role
# actually holds among the ones relevant to registering, approving or
# running intake on a covenant. `RUN_INTAKE`/`REGISTER_COVENANT` are the
# only two permissions that could conceivably put a role on the path to
# `IntakeService.submit`; `APPROVE_COVENANT` is included too so the "Cr.
# Approver" and "Risk Head" rows are proven with their real, full authority.
_ROLE_PERMISSIONS: dict[str, tuple[Permission, ...]] = {
    "relationship_manager": (Permission.VIEW_COVENANT,),
    "credit_officer": (
        Permission.RUN_INTAKE,
        Permission.REGISTER_COVENANT,
        Permission.VIEW_COVENANT,
    ),
    "credit_approver": (Permission.APPROVE_COVENANT, Permission.VIEW_COVENANT),
    "risk_analyst": (
        Permission.RUN_INTAKE,
        Permission.REGISTER_COVENANT,
        Permission.VIEW_COVENANT,
    ),
    "risk_head": (
        Permission.RUN_INTAKE,
        Permission.REGISTER_COVENANT,
        Permission.APPROVE_COVENANT,
        Permission.VIEW_COVENANT,
    ),
    "auditor": (Permission.VIEW_COVENANT,),
    "admin": (Permission.VIEW_COVENANT,),
    "steward": (Permission.RUN_INTAKE, Permission.VIEW_COVENANT),
}

# Roles the matrix's "Register / amend a covenant" row marks "yes" for - the
# only ones that ever reach `IntakeService.submit`'s authorization gate.
_ROLES_WITH_REGISTER_COVENANT = ("credit_officer", "risk_analyst", "risk_head")

_FAILING_REPLY: dict[str, object] = {
    "definition": "dscr",
    "custom_formula": None,
    "threshold": "50x",  # above dscr's plausible_max=20: fails threshold_plausible
    "direction": "above",
    "unit": "ratio",
    "currency": None,
    "frequency": "quarterly",
    "effective_from": "2026-04-01",
    "effective_to": None,
    "exceptions": [],
    "cure_period_days": 30,
    "source_quote": "DSCR shall not fall below 50 times, tested quarterly.",
}


def _failing_proposal() -> StageOneProposal:
    text = str(_FAILING_REPLY["source_quote"])
    line = CandidateLine(page_number=1, start_offset=0, end_offset=len(text), text=text)
    candidate = ClauseCandidate(
        start_page=1,
        start_offset=0,
        end_page=1,
        end_offset=len(text),
        text=text,
        matched_rules=("ratio:dscr", "threshold:0"),
        lines=(line,),
    )
    return parse_stage1_reply(candidate, json.dumps(_FAILING_REPLY))


def _context() -> VerificationContext:
    return VerificationContext(
        statement_lines={
            "cash_flow_debt_service": Decimal("150"),
            "finance_cost": Decimal("100"),
        },
        period_complete=True,
        facility_facts=FacilityFacts(),
        facility_sanction_date=_SANCTION_DATE,
        facility_currency="INR",
    )


class _Audit:
    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        return object()


class _Bundle:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.scopes: dict[UUID, Scope] = {}
        self.roles: dict[str, Principal] = {}
        self.portfolio = self._add_portfolio()
        for role, permissions in _ROLE_PERMISSIONS.items():
            principal = Principal.user(uuid4(), permissions)
            self.roles[role] = principal
            self._add_user(principal, role)
            self.scopes[principal.id] = Scope.from_paths(principal.id, [self.portfolio.path])
        # An API-key principal scoped exactly like the credit-officer role,
        # to prove the refusal also holds for "session or API key".
        self.api_key = Principal.api_key(uuid4(), _ROLE_PERMISSIONS["credit_officer"])
        self.scopes[self.api_key.id] = Scope.from_paths(self.api_key.id, [self.portfolio.path])

        self.facility = self._add_facility(self.portfolio, "000001")
        self.registry = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t096-security-registry",
            scope_resolver=lambda principal: self.scopes[principal.id],
            maker_checker_enabled=False,
        )
        self.service = IntakeService(
            self.session,
            audit=self.audit,
            registry=self.registry,
            clock=FixedClock(_NOW),
            request_id="rq-t096-security-intake",
            scope_resolver=lambda principal: self.scopes[principal.id],
        )

    def _add_portfolio(self) -> Portfolio:
        portfolio = Portfolio.create(
            code="ROOT",
            name="Portfolio ROOT",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-portfolio-root",
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

    def new_failing_proposal(self) -> CovenantProposal:
        creator = self.roles["credit_officer"]
        [record] = self.service.propose_from_document(
            creator,
            facility_id=self.facility.id,
            clauses=(ProposedClause(proposal=_failing_proposal()),),
            context=_context(),
            scope=self.scopes[creator.id],
        )
        assert record.outcome.all_passed is False
        return record.row

    def covenant_count(self) -> int:
        return len(self.session.execute(select(Covenant)).scalars().all())

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


@pytest.mark.parametrize("role", sorted(_ROLE_PERMISSIONS))
def test_confirm_failed_proposal_refused_for_every_role(role: str) -> None:
    """No role - not even one holding `REGISTER_COVENANT` and
    `APPROVE_COVENANT` together - can confirm a proposal that failed
    verification. A role without `REGISTER_COVENANT` is refused at
    authorization; a role with it is refused by the same unconditional,
    permission-independent check every other role hits."""
    bundle = _Bundle()
    try:
        principal = bundle.roles[role]
        scope = bundle.scopes[principal.id]
        proposal = bundle.new_failing_proposal()
        before = bundle.covenant_count()

        if role in _ROLES_WITH_REGISTER_COVENANT:
            with pytest.raises(ProposalVerificationFailed) as excinfo:
                bundle.service.submit(
                    principal,
                    proposal.id,
                    test_basis="standalone",
                    reference="CV-SHOULD-NOT-EXIST",
                    name="Should never register",
                    covenant_class="financial",
                    scope=scope,
                )
            assert "threshold_plausible" in excinfo.value.failed_checks
        else:
            with pytest.raises(AuthorizationError):
                bundle.service.submit(
                    principal,
                    proposal.id,
                    test_basis="standalone",
                    reference="CV-SHOULD-NOT-EXIST",
                    name="Should never register",
                    covenant_class="financial",
                    scope=scope,
                )

        assert bundle.covenant_count() == before
        refreshed = bundle.session.get(CovenantProposal, proposal.id)
        assert refreshed is not None
        assert refreshed.status == "open"
    finally:
        bundle.close()


def test_confirm_failed_proposal_refused_for_api_key() -> None:
    """The same refusal holds for a scoped API-key principal, not only a
    session user - `spec §R-06.b`'s "regardless of role, session or API
    key"."""
    bundle = _Bundle()
    try:
        scope = bundle.scopes[bundle.api_key.id]
        proposal = bundle.new_failing_proposal()
        before = bundle.covenant_count()

        with pytest.raises(ProposalVerificationFailed) as excinfo:
            bundle.service.submit(
                bundle.api_key,
                proposal.id,
                test_basis="standalone",
                reference="CV-SHOULD-NOT-EXIST",
                name="Should never register",
                covenant_class="financial",
                scope=scope,
            )
        assert "threshold_plausible" in excinfo.value.failed_checks
        assert bundle.covenant_count() == before
    finally:
        bundle.close()


def test_refusal_names_failed_checks() -> None:
    bundle = _Bundle()
    try:
        principal = bundle.roles["credit_officer"]
        scope = bundle.scopes[principal.id]
        proposal = bundle.new_failing_proposal()

        with pytest.raises(ProposalVerificationFailed) as excinfo:
            bundle.service.submit(
                principal,
                proposal.id,
                test_basis="standalone",
                reference="CV-SHOULD-NOT-EXIST",
                name="Should never register",
                covenant_class="financial",
                scope=scope,
            )

        error = excinfo.value
        assert error.failed_checks == ("threshold_plausible",)
        assert "threshold_plausible" in str(error)
    finally:
        bundle.close()
