"""Security tests for `T-067`'s audit-emission coverage guarantee.

Every state-changing service method — and the one privileged personal-data
read (`MasterDataService.reveal_identity`) — must either reach an audit
call (directly, or through a same-class private helper it calls) or be
named in `covenant_radar.audit.events.AUDIT_EXEMPTIONS` with a reason.

`_CLASSIFICATION` enumerates every public method of every scanned service
class as ``"audited"`` or ``"read_only"``.  `test_every_state_changing_...`
walks each class through `inspect`, so a new public method that is not yet
classified fails the test by name — it can never silently ship uncovered.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AUDIT_EXEMPTIONS, validate_exemptions
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import AuthorizationError
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, EvidenceItem, Portfolio
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.signals.evidence import SignalEventFacts
from covenant_radar.security.crypto import FieldEncryptor, HMACFingerprinter
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.approvals import ApprovalService
from covenant_radar.services.auth import AuthService
from covenant_radar.services.documents import DocumentService
from covenant_radar.services.engine import EngineService
from covenant_radar.services.ingestion import SignalIngestionService
from covenant_radar.services.intake import IntakeDetectionService, IntakeService
from covenant_radar.services.ledger import LedgerService
from covenant_radar.services.master_data import MasterDataService
from covenant_radar.services.registry import RegistryService
from covenant_radar.services.scoring import ForecastScoringService
from covenant_radar.services.triage import TriageService

pytestmark = pytest.mark.security

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)

_AUDITED: Final[str] = "audited"
_READ_ONLY: Final[str] = "read_only"

# Every service whose methods are subject to `T-067`'s coverage guarantee.
# Repositories and web/API routes are deliberately excluded: `plan.md §3.3`
# assigns emission to services only, so a repository or route calling audit
# directly would itself be the bug this suite is not designed to find.
_SCANNED_CLASSES: Final[tuple[type, ...]] = (
    AuthService,
    ApprovalService,
    MasterDataService,
    RegistryService,
    EngineService,
    DocumentService,
    IntakeDetectionService,
    IntakeService,
    SignalIngestionService,
    LedgerService,
    ForecastScoringService,
    TriageService,
)

# `(class name, method name)` -> "audited" | "read_only". Keyed on the
# underlying function's own `__name__`, so an alias assignment such as
# ``authenticate = sign_in`` never needs a second entry (see
# `_public_methods`).
_CLASSIFICATION: Final[dict[tuple[str, str], str]] = {
    # AuthService (`services/auth.py`).
    ("AuthService", "sign_in"): _AUDITED,
    ("AuthService", "verify_mfa"): _AUDITED,
    ("AuthService", "begin_mfa_enrollment"): _AUDITED,
    ("AuthService", "complete_mfa_enrollment"): _AUDITED,
    ("AuthService", "change_password"): _AUDITED,
    ("AuthService", "sign_out"): _AUDITED,
    ("AuthService", "refresh_session"): _AUDITED,
    ("AuthService", "validate_session"): _READ_ONLY,
    ("AuthService", "revoke_sessions_for_role_change"): _AUDITED,
    # ApprovalService (`services/approvals.py`).
    ("ApprovalService", "submit"): _AUDITED,
    ("ApprovalService", "decide"): _AUDITED,
    ("ApprovalService", "list_pending"): _AUDITED,
    ("ApprovalService", "expire"): _AUDITED,
    ("ApprovalService", "expire_pending"): _AUDITED,
    # MasterDataService (`services/master_data.py`).
    ("MasterDataService", "list_borrowers"): _READ_ONLY,
    ("MasterDataService", "get_borrower"): _READ_ONLY,
    ("MasterDataService", "get_borrower_by_id"): _READ_ONLY,
    ("MasterDataService", "list_facilities"): _READ_ONLY,
    ("MasterDataService", "list_facility_listings"): _READ_ONLY,
    ("MasterDataService", "count_facilities"): _READ_ONLY,
    ("MasterDataService", "facility_filter_values"): _READ_ONLY,
    ("MasterDataService", "facility_book"): _READ_ONLY,
    ("MasterDataService", "facility_revisions"): _READ_ONLY,
    ("MasterDataService", "list_facilities_for_borrower"): _READ_ONLY,
    ("MasterDataService", "get_facility"): _READ_ONLY,
    ("MasterDataService", "get_facility_as_of"): _READ_ONLY,
    ("MasterDataService", "list_portfolios"): _READ_ONLY,
    ("MasterDataService", "get_portfolio"): _READ_ONLY,
    ("MasterDataService", "reveal_identity"): _AUDITED,
    ("MasterDataService", "create_borrower"): _AUDITED,
    ("MasterDataService", "update_borrower"): _AUDITED,
    ("MasterDataService", "deactivate_borrower"): _AUDITED,
    ("MasterDataService", "create_facility"): _AUDITED,
    ("MasterDataService", "update_facility"): _AUDITED,
    ("MasterDataService", "deactivate_facility"): _AUDITED,
    ("MasterDataService", "create_portfolio"): _AUDITED,
    ("MasterDataService", "update_portfolio"): _AUDITED,
    # RegistryService (`services/registry.py`).
    ("RegistryService", "register"): _AUDITED,
    ("RegistryService", "amend"): _AUDITED,
    ("RegistryService", "register_exception"): _AUDITED,
    ("RegistryService", "request_waiver"): _AUDITED,
    ("RegistryService", "approve_waiver"): _AUDITED,
    ("RegistryService", "reject_waiver"): _AUDITED,
    ("RegistryService", "retire"): _AUDITED,
    ("RegistryService", "decide_approval"): _AUDITED,
    ("RegistryService", "approve_covenant"): _AUDITED,
    ("RegistryService", "list_covenants"): _READ_ONLY,
    ("RegistryService", "get_covenant"): _READ_ONLY,
    ("RegistryService", "list_versions"): _READ_ONLY,
    ("RegistryService", "pending_approvals"): _READ_ONLY,
    ("RegistryService", "live_at"): _READ_ONLY,
    # EngineService (`services/engine.py`). `test_covenant`/`evaluate` are
    # thin wrapper `def`s (not `=` aliases) that both call `self.test(...)`.
    ("EngineService", "test"): _AUDITED,
    ("EngineService", "test_covenant"): _AUDITED,
    ("EngineService", "evaluate"): _AUDITED,
    ("EngineService", "queue_retest"): _AUDITED,
    # DocumentService (`services/documents.py`).
    ("DocumentService", "upload_document"): _AUDITED,
    ("DocumentService", "upload_file"): _AUDITED,
    ("DocumentService", "get_document"): _READ_ONLY,
    ("DocumentService", "stream_document"): _READ_ONLY,
    ("DocumentService", "list_documents"): _READ_ONLY,
    ("DocumentService", "list_review_pages"): _READ_ONLY,
    ("DocumentService", "review_queue"): _READ_ONLY,
    ("DocumentService", "list_detection_pages"): _READ_ONLY,
    ("DocumentService", "correct_page"): _AUDITED,
    ("DocumentService", "classify_document"): _READ_ONLY,
    ("DocumentService", "get_classification_override"): _READ_ONLY,
    ("DocumentService", "override_classification"): _AUDITED,
    ("DocumentService", "get_page"): _READ_ONLY,
    ("DocumentService", "page_was_corrected"): _READ_ONLY,
    ("DocumentService", "extract_document"): _AUDITED,
    ("DocumentService", "extract_native"): _AUDITED,
    ("DocumentService", "lookup_spans"): _READ_ONLY,
    ("DocumentService", "lookup_span"): _READ_ONLY,
    # IntakeDetectionService (`services/intake.py`).
    ("IntakeDetectionService", "detect_candidates"): _READ_ONLY,
    # IntakeService (`services/intake.py`).
    ("IntakeService", "propose_from_document"): _AUDITED,
    ("IntakeService", "proposals_for_document"): _READ_ONLY,
    ("IntakeService", "proposal"): _READ_ONLY,
    ("IntakeService", "correct"): _AUDITED,
    ("IntakeService", "abandon"): _AUDITED,
    ("IntakeService", "find_amendment_target"): _READ_ONLY,
    ("IntakeService", "submit"): _AUDITED,
    # SignalIngestionService (`services/ingestion.py`).
    ("SignalIngestionService", "ingest"): _AUDITED,
    # LedgerService (`services/ledger.py`).
    ("LedgerService", "revise"): _AUDITED,
    ("LedgerService", "read_as_of"): _READ_ONLY,
    ("LedgerService", "read_trace"): _READ_ONLY,
    # ForecastScoringService (`services/scoring.py`).
    ("ForecastScoringService", "score"): _AUDITED,
    # TriageService (`services/triage.py`).
    ("TriageService", "query"): _READ_ONLY,
    ("TriageService", "compare"): _READ_ONLY,
    ("TriageService", "persist_what_changed"): _AUDITED,
}


def _public_methods(cls: type) -> dict[str, Callable[..., object]]:
    """Return one entry per underlying function, keyed by its own name.

    An alias assignment (``authenticate = sign_in``) binds a second class
    attribute to the *same* function object.  Keying by the function's own
    ``__name__`` instead of the attribute name collapses that back to one
    entry automatically, so an alias never needs its own classification.
    """
    result: dict[str, Callable[..., object]] = {}
    for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        result.setdefault(member.__name__, member)
    return result


def _is_direct_audit_call(func: ast.expr) -> bool:
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "record" and isinstance(func.value, ast.Attribute):
        return (
            func.value.attr == "audit"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"
        )
    return (
        func.attr.startswith("_audit")
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _reaches_audit_call(
    cls: type,
    method: Callable[..., object],
    *,
    _seen: frozenset[str] = frozenset(),
) -> bool:
    """Whether `method`, or a same-class helper it calls, reaches an audit call.

    A coarse, sound-for-purpose static check: it asks only whether an audit
    call is *reachable* somewhere in the method's source (including inside
    a same-class private helper it references), not whether every branch
    executes one. The failure this test exists to catch is a method that
    never audits at all — `services/scoring.py` and `services/triage.py`
    before this task — not a branch that occasionally skips it.
    """
    source = textwrap.dedent(inspect.getsource(method))
    function_def = ast.parse(source).body[0]
    if not isinstance(function_def, ast.FunctionDef | ast.AsyncFunctionDef):
        raise TypeError(f"{method!r} did not parse as a function definition.")

    same_class_calls: set[str] = set()
    for node in ast.walk(function_def):
        if not isinstance(node, ast.Call):
            continue
        if _is_direct_audit_call(node.func):
            return True
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "self":
                same_class_calls.add(func.attr)

    for name in same_class_calls - _seen:
        helper = getattr(cls, name, None)
        if not inspect.isfunction(helper):
            continue
        if _reaches_audit_call(cls, helper, _seen=_seen | {name}):
            return True
    return False


def test_every_state_changing_method_records_or_is_exempt() -> None:
    validate_exemptions(AUDIT_EXEMPTIONS)

    unclassified: list[str] = []
    uncovered: list[str] = []
    for cls in _SCANNED_CLASSES:
        for name, method in _public_methods(cls).items():
            key = (cls.__name__, name)
            classification = _CLASSIFICATION.get(key)
            if classification is None:
                unclassified.append(f"{cls.__name__}.{name}")
                continue
            if classification != _AUDITED or key in AUDIT_EXEMPTIONS:
                continue
            if not _reaches_audit_call(cls, method):
                uncovered.append(f"{cls.__name__}.{name}")

    assert not unclassified, (
        "New service method(s) with no audit-coverage classification — add "
        "each to _CLASSIFICATION as 'audited' or 'read_only': "
        f"{', '.join(sorted(unclassified))}."
    )
    assert not uncovered, (
        "State-changing service method(s) reach no audit call and carry no "
        f"exemption: {', '.join(sorted(uncovered))}."
    )


def test_exemption_requires_reason() -> None:
    validate_exemptions(AUDIT_EXEMPTIONS)  # the real registry stays valid

    with pytest.raises(ValueError, match="non-empty reason"):
        validate_exemptions({("ExampleService", "example_method"): ""})
    with pytest.raises(ValueError, match="non-empty reason"):
        validate_exemptions({("ExampleService", "example_method"): "   "})

    validate_exemptions(
        {("ExampleService", "example_method"): "Audited by a collaborator, see docstring."}
    )


def _borrower_schema_engine() -> tuple[object, Session]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            Portfolio.__table__,
            Borrower.__table__,
            Facility.__table__,
            AuditEvent.__table__,
        ],
    )
    return engine, Session(engine)


def test_event_written_in_same_transaction() -> None:
    engine, session = _borrower_schema_engine()
    try:
        audit = AuditRecorder(
            AuditRepository(session), clock=FixedClock(_NOW), request_id="rq-t067-same-tx"
        )
        principal = Principal.user(
            uuid4(), (Permission.VIEW_BORROWER, Permission.CORRECT_SOURCE_DATA)
        )
        service = MasterDataService(
            session,
            audit=audit,
            clock=FixedClock(_NOW),
            scope_resolver=lambda _principal: Scope.empty(principal.id),
            request_id="rq-t067-same-tx",
        )
        portfolio = service.create_portfolio(
            principal, code="ROOT", name="Root", scope=Scope.empty(principal.id)
        )
        session.commit()  # the baseline portfolio is a settled fact
        scope = Scope.from_paths(principal.id, [portfolio.path])

        service.create_borrower(
            principal,
            reference="B-000001",
            legal_name="Borrower One",
            portfolio_id=portfolio.id,
            scope=scope,
        )

        assert session.scalar(select(func.count(Borrower.id))) == 1
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.event_type == "master_data_borrower_created"
                )
            )
            == 1
        )

        session.rollback()

        assert session.scalar(select(func.count(Borrower.id))) == 0
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.event_type == "master_data_borrower_created"
                )
            )
            == 0
        )
    finally:
        session.close()
        engine.dispose()


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object, str]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, dict(payload), actor, request_id))
        return object()


def test_privileged_personal_read_logged_with_purpose() -> None:
    engine, session = _borrower_schema_engine()
    try:
        audit = _RecordingAudit()
        encryptor = FieldEncryptor({"t067": b"k" * 32}, "t067")
        fingerprinter = HMACFingerprinter(b"f" * 32)
        principal = Principal.user(
            uuid4(),
            (
                Permission.VIEW_BORROWER,
                Permission.CORRECT_SOURCE_DATA,
                Permission.READ_PERSONAL_DATA,
            ),
        )
        service = MasterDataService(
            session,
            audit=audit,
            clock=FixedClock(_NOW),
            encryptor=encryptor,
            fingerprinter=fingerprinter,
            scope_resolver=lambda _principal: Scope.empty(principal.id),
            request_id="rq-t067-reveal",
        )
        portfolio = service.create_portfolio(
            principal, code="ROOT", name="Root", scope=Scope.empty(principal.id)
        )
        scope = Scope.from_paths(principal.id, [portfolio.path])
        borrower = service.create_borrower(
            principal,
            reference="B-000001",
            legal_name="Borrower One",
            portfolio_id=portfolio.id,
            cin="U12345MH2020PTC123456",
            pan="ABCDE1234F",
            scope=scope,
        )

        revealed = service.reveal_identity(
            principal, borrower.reference, purpose="kyc_refresh", scope=scope
        )
        assert revealed.cin == "U12345MH2020PTC123456"
        assert revealed.pan == "ABCDE1234F"

        access_events = [
            event for event in audit.events if event[0] == "master_data_personal_data_accessed"
        ]
        assert len(access_events) == 1
        _, _, payload, actor, _ = access_events[0]
        assert payload["purpose"] == "kyc_refresh"
        assert set(payload["fields"]) == {"cin", "pan"}
        assert actor == principal.id
        # The decrypted identity values must never reach the audit trail.
        assert "U12345MH2020PTC123456" not in str(payload)
        assert "ABCDE1234F" not in str(payload)

        unprivileged = Principal.user(uuid4(), (Permission.VIEW_BORROWER,))
        with pytest.raises(AuthorizationError):
            service.reveal_identity(
                unprivileged, borrower.reference, purpose="kyc_refresh", scope=scope
            )
    finally:
        session.close()
        engine.dispose()


def test_bulk_writes_detail_and_summary() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        audit = _RecordingAudit()
        user_id = uuid4()
        portfolio = Portfolio.create(
            code="T067",
            name="T067 portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t067-bulk-portfolio",
        )
        session.add(portfolio)
        session.flush()
        session.add(
            AppUser(
                id=user_id,
                username="t067-user",
                email="t067@example.com",
                full_name="T067 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t067-bulk-user",
            )
        )
        borrower = Borrower(
            reference="B-T067",
            legal_name="T067 Borrower Private Limited",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t067-bulk-borrower",
        )
        session.add(borrower)
        session.flush()
        prior = EvidenceItem(
            id=uuid4(),
            borrower_id=borrower.id,
            facility_id=None,
            family="payment",
            evidence_type="payment_delay",
            first_seen=date(2026, 8, 1),
            last_seen=date(2026, 8, 1),
            persistence_days=14,
            event_count_window=3,
            materiality_pct=Decimal("10"),
            decay_factor=Decimal("1"),
            state="sustained",
            counts_toward_pressure=True,
            source_event_ids=["delay-1"],
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t067-bulk-prior",
        )
        session.add(prior)
        session.flush()

        scope = Scope.from_paths(user_id, [portfolio.path])
        principal = Principal.user(user_id, (Permission.INGEST_DATA, Permission.VIEW_EVIDENCE))
        service = LedgerService(
            session, audit=audit, clock=FixedClock(_NOW), request_id="rq-t067-bulk-service"
        )
        received = SignalEventFacts(
            borrower_id=borrower.id,
            facility_id=None,
            event_date=date(2026, 8, 2),
            family="payment",
            event_type="payment_received",
            magnitude=Decimal("0"),
            payload={"is_adverse": False},
            event_id="received-1",
        )

        revision = service.revise(
            principal, borrower.id, [received], as_of=date(2026, 8, 2), scope=scope
        )
        assert revision.changed is True

        detail_events = [event for event in audit.events if event[0] == "evidence_superseded"]
        summary_events = [event for event in audit.events if event[0] == "evidence_ledger_revised"]
        assert len(detail_events) == len(revision.supersessions) >= 1
        assert len(summary_events) == 1
        summary_payload = summary_events[0][2]
        assert summary_payload["supersessions"] == len(revision.supersessions)
        assert summary_payload["evidence_items"] == len(revision.items)
    finally:
        session.close()
        engine.dispose()
