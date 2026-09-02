"""Integration coverage for T-097's protected covenant intake screen."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from covenant_radar.ai.errors import ProviderUnavailable
from covenant_radar.asgi import create_app
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.document import DocumentPage, DocumentSpan
from covenant_radar.db.repositories.trace import TraceRepository, TraceSubject
from covenant_radar.documents.store import FileSystemDocumentStore
from covenant_radar.domain.intake.candidates import ClauseCandidate
from covenant_radar.domain.intake.proposal import StageOneProposal, parse_stage1_reply
from covenant_radar.security.crypto import FieldEncryptor
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.security.uploads import ScanResult
from covenant_radar.services.documents import DocumentService
from covenant_radar.services.intake import ProposedClause
from covenant_radar.web.routes.intake import create_intake_router
from tests.integration.test_intake_service import _BASE_REPLY, _Bundle, _context, _proposal

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_SOURCE_TEXT = "DSCR shall not fall below 1.5 times, tested quarterly."
_PERMISSIONS = (
    Permission.RUN_INTAKE,
    Permission.REGISTER_COVENANT,
    Permission.VIEW_COVENANT,
    Permission.VIEW_DOCUMENT,
    Permission.UPLOAD_DOCUMENT,
)


def _proposal_for_candidate(
    candidate: ClauseCandidate, *, threshold: str = "1.5x"
) -> StageOneProposal:
    payload = {**_BASE_REPLY, "threshold": threshold, "source_quote": candidate.text}
    return parse_stage1_reply(candidate, json.dumps(payload))


def _generator(
    candidates: tuple[ClauseCandidate, ...], *, threshold: str = "1.5x"
) -> tuple[StageOneProposal, ...]:
    return tuple(
        _proposal_for_candidate(candidate, threshold=threshold) for candidate in candidates
    )


class _ScreenFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.bundle = _Bundle()
        self.bundle.officer = Principal.user(self.bundle.officer.id, _PERMISSIONS)
        self.document_service = DocumentService(
            self.bundle.session,
            store=FileSystemDocumentStore(
                tmp_path / "documents",
                encryptor=FieldEncryptor({"intake-screen": b"I" * 32}, "intake-screen"),
            ),
            audit=self.bundle.audit,
            scanner=lambda _content: ScanResult(clean=True, engine="test-scanner"),
            request_id="rq-t097-document-000001",
            scope_resolver=lambda principal: self.bundle.scopes[principal.id],
        )

    @property
    def session(self):
        return self.bundle.session

    @property
    def principal(self) -> Principal:
        return self.bundle.officer

    def document(self, *, needs_review: bool = False):
        document = self.bundle.add_document("screen-000001")
        document.extraction_state = "complete"
        document.page_count = 1
        document.ocr_applied = False
        self.session.flush()
        page = DocumentPage(
            id=new_id(),
            document_id=document.id,
            page_number=1,
            text=_SOURCE_TEXT,
            ocr_confidence=None,
            needs_review=needs_review,
            width=612,
            height=792,
            created_at=_NOW,
            updated_at=_NOW,
            created_by_id=self.principal.id,
            updated_by_id=self.principal.id,
            request_id="rq-t097-page-000001",
        )
        self.session.add(page)
        if not needs_review:
            self.session.add(
                DocumentSpan(
                    id=new_id(),
                    document_id=document.id,
                    page_number=1,
                    start_offset=0,
                    end_offset=len(_SOURCE_TEXT),
                    text=_SOURCE_TEXT,
                    span_type="body",
                    created_at=_NOW,
                    updated_at=_NOW,
                    created_by_id=self.principal.id,
                    updated_by_id=self.principal.id,
                    request_id="rq-t097-span-000001",
                )
            )
        self.session.flush()
        return document

    def client(self, *, generator=None) -> TestClient:
        self.bundle.service.scope_resolver = lambda principal: self.bundle.scopes[principal.id]
        router = create_intake_router(
            self.bundle.service,
            self.document_service,
            proposal_generator=generator,
            context_factory=lambda _principal, _facility: _context(),
        )
        app = create_app(
            routers=(router,),
            principal_resolver=lambda _request: self.principal,
        )
        return TestClient(app)

    def close(self) -> None:
        self.bundle.close()


def test_clean_clause_renders_all_green_and_confirm(tmp_path: Path) -> None:
    fixture = _ScreenFixture(tmp_path)
    try:
        document = fixture.document()
        client = fixture.client(generator=_generator)

        response = client.post(
            "/intake/proposals",
            data={
                "document_id": str(document.id),
                "facility_ref": fixture.bundle.facility.reference,
            },
        )

        assert response.status_code == 200
        assert response.text.count('class="verdict-mark verdict-mark--passed"') == 6
        assert "Confirm covenant" in response.text
        stage_one = TraceRepository(fixture.session).read(
            TraceSubject("borrower", fixture.bundle.facility.borrower_id)
        )[0]
        assert stage_one.not_run is False
        assert stage_one.decider == "model"
        assert stage_one.rule_or_prompt_version == "stage1_extract.v1"
        assert stage_one.outputs["proposals"][0]["verification_passed"] is True
        proposal_id = fixture.bundle.service.proposals_for_document(
            fixture.principal, document.id, scope=fixture.bundle.scope()
        )[0].row.id
        confirmation = client.post(
            f"/intake/proposals/{proposal_id}/submit",
            data={
                "test_basis": "standalone",
                "reference": "CV-T097-001",
                "name": "DSCR covenant",
                "covenant_class": "financial",
            },
            follow_redirects=False,
        )
        assert confirmation.status_code == 303
        assert confirmation.headers["location"] == "/covenants/CV-T097-001"
    finally:
        fixture.close()


def test_upload_returns_document_and_extraction_state(tmp_path: Path) -> None:
    fixture = _ScreenFixture(tmp_path)
    try:
        client = fixture.client(generator=_generator)
        response = client.post(
            "/documents",
            headers={"accept": "application/json"},
            data={
                "borrower_ref": f"B-{fixture.bundle.facility.reference.removeprefix('F-')}",
                "facility_ref": fixture.bundle.facility.reference,
                "doc_type": "sanction_letter",
            },
            files={"file": ("uploaded.pdf", b"%PDF-1.7\ntruncated", "application/pdf")},
        )

        assert response.status_code == 202
        payload = response.json()
        assert UUID(payload["document_id"])
        assert payload["extraction_state"] == "failed"
        assert payload["error"]
        assert response.headers["location"].startswith("/intake?document_id=")
    finally:
        fixture.close()


def test_failed_proposal_struck_with_check_named(tmp_path: Path) -> None:
    fixture = _ScreenFixture(tmp_path)
    try:
        document = fixture.document()
        client = fixture.client(
            generator=lambda candidates: _generator(candidates, threshold="50x")
        )
        response = client.post(
            "/intake/proposals",
            data={
                "document_id": str(document.id),
                "facility_ref": fixture.bundle.facility.reference,
            },
        )

        assert response.status_code == 200
        assert 'data-verdict="struck"' in response.text
        assert "<del>" in response.text
        assert "Threshold Plausible" in response.text
    finally:
        fixture.close()


def test_no_confirm_control_in_markup_when_failed(tmp_path: Path) -> None:
    fixture = _ScreenFixture(tmp_path)
    try:
        document = fixture.document()
        client = fixture.client(
            generator=lambda candidates: _generator(candidates, threshold="50x")
        )
        response = client.post(
            "/intake/proposals",
            data={
                "document_id": str(document.id),
                "facility_ref": fixture.bundle.facility.reference,
            },
        )

        assert response.status_code == 200
        assert "Confirm covenant" not in response.text
        assert "Save and re-run verification" in response.text
        proposal_id = fixture.bundle.service.proposals_for_document(
            fixture.principal, document.id, scope=fixture.bundle.scope()
        )[0].row.id
        failed_submit = client.post(
            f"/intake/proposals/{proposal_id}/submit",
            headers={"accept": "application/json"},
            data={"test_basis": "standalone"},
        )
        assert failed_submit.status_code == 409
        assert "threshold_plausible" in failed_submit.json()["failed_checks"]
    finally:
        fixture.close()


def test_provider_down_renders_hand_entry_with_verification(tmp_path: Path) -> None:
    fixture = _ScreenFixture(tmp_path)
    try:
        document = fixture.document()
        client = fixture.client(
            generator=lambda _candidates: (_ for _ in ()).throw(ProviderUnavailable("test"))
        )
        response = client.post(
            "/intake/proposals",
            data={
                "document_id": str(document.id),
                "facility_ref": fixture.bundle.facility.reference,
            },
        )

        assert response.status_code == 200
        assert 'data-hand-entry="true"' in response.text
        assert "model provider is unavailable" in response.text
        assert "code verification remains active" in response.text
        assert "intake-proposal__proposal" not in response.text

        hand_entry = client.post(
            "/intake/proposals",
            data={
                "facility_ref": fixture.bundle.facility.reference,
                "clause_text": _SOURCE_TEXT,
                "definition": "dscr",
                "threshold": "1.5x",
                "direction": "above",
                "unit": "ratio",
                "frequency": "quarterly",
                "effective_from": "2026-04-01",
                "source_quote": _SOURCE_TEXT,
            },
        )
        assert hand_entry.status_code == 200
        assert 'data-hand-entry="true"' in hand_entry.text
        assert 'data-proposal-id="' in hand_entry.text
    finally:
        fixture.close()


def test_review_pending_candidate_not_proposed(tmp_path: Path) -> None:
    fixture = _ScreenFixture(tmp_path)
    try:
        document = fixture.document(needs_review=True)
        client = fixture.client(generator=_generator)
        response = client.post(
            "/intake/proposals",
            data={
                "document_id": str(document.id),
                "facility_ref": fixture.bundle.facility.reference,
            },
        )

        assert response.status_code == 200
        assert 'data-candidate-state="pending-review"' in response.text
        assert "intake-proposal__proposal" not in response.text
        assert "needs OCR review" in response.text
    finally:
        fixture.close()


def test_bulk_confirm_excludes_failures(tmp_path: Path) -> None:
    fixture = _ScreenFixture(tmp_path)
    try:
        document = fixture.document()
        passing = fixture.bundle.propose(document_id=document.id)
        failed = fixture.bundle.service.propose_from_document(
            fixture.principal,
            facility_id=fixture.bundle.facility.id,
            clauses=(
                ProposedClause(
                    proposal=_proposal(threshold="50x"),
                ),
            ),
            context=_context(),
            document_id=document.id,
            force_reextraction=True,
            scope=fixture.bundle.scope(),
        )[0]
        client = fixture.client(generator=_generator)
        response = client.get(f"/intake/{document.id}")

        assert response.status_code == 200
        assert str(passing.row.id) in response.text
        assert str(failed.row.id) in response.text
        bulk_marker = response.text.split('id="intake-bulk"', 1)[1]
        assert str(passing.row.id) in bulk_marker
        assert str(failed.row.id) not in bulk_marker
    finally:
        fixture.close()
