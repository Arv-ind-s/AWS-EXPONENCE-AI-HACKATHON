"""Covenant-intake browser routes (T-097, contracts C-04 through C-07).

This module is an adapter only.  Upload validation/extraction belongs to
``DocumentService``; proposal verification and the confirm refusal belong to
``IntakeService``; and maker-checker approval remains in ``RegistryService``.
The route shapes those already-authoritative results for a reviewer and
never decides that a proposal is safe by inspecting a client-side value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from covenant_radar.ai.errors import ProviderError
from covenant_radar.api.deps import requires
from covenant_radar.core.errors import AuthorizationError, DomainError, NotFound, ValidationError
from covenant_radar.db.models.document import Document, DocumentSpan
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.repositories.base import RepositoryBase
from covenant_radar.db.repositories.facility import FacilityRepository
from covenant_radar.db.repositories.trace import TraceRepository, TraceSubject
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.documents.classify import DOCUMENT_TYPES
from covenant_radar.documents.extract_native import NativePdfExtractionError
from covenant_radar.documents.ocr import is_history_span
from covenant_radar.domain.intake.candidates import (
    CandidateLine,
    ClauseCandidate,
    DetectionResult,
)
from covenant_radar.domain.intake.proposal import (
    DIRECTION_WORDS,
    FREQUENCY_WORDS,
    UNIT_KINDS,
    StageOneProposal,
    parse_stage1_reply,
)
from covenant_radar.domain.intake.verify import VerificationContext
from covenant_radar.domain.ratios.definitions import FacilityFacts
from covenant_radar.domain.trace import stage_record
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.security.uploads import UploadRejected
from covenant_radar.services.documents import DocumentService
from covenant_radar.services.intake import (
    IntakeDetectionService,
    IntakeService,
    ProposalRecord,
    ProposalVerificationFailed,
    ProposedClause,
)
from covenant_radar.web.errors import status_for_error
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.intake import (
    IntakePendingReviewView,
    IntakeScreenView,
    build_document_view,
    build_proposal_view,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 128 * 1024
_MAX_CLAUSE_TEXT_LENGTH = 20_000
_MAX_EXCEPTIONS = 32
_MAX_MULTIPART_FIELDS = 24
_MAX_BULK_PROPOSALS = 100
_DEFAULT_DOC_TYPE = "sanction_letter"

_FORM_OPTIONS = {
    "doc_type": tuple((value, value.replace("_", " ").title()) for value in DOCUMENT_TYPES),
    "direction": (("", "Not supplied"), ("above", "Above"), ("below", "Below")),
    "unit": (
        ("", "Not supplied"),
        ("ratio", "Ratio"),
        ("percent", "Percent"),
        ("currency", "Currency"),
        ("days", "Days"),
        ("count", "Count"),
    ),
    "frequency": (
        ("", "Not supplied"),
        ("monthly", "Monthly"),
        ("quarterly", "Quarterly"),
        ("half_yearly", "Half yearly"),
        ("yearly", "Yearly"),
        ("event_driven", "Event driven"),
    ),
}

_RUN = requires(Permission.RUN_INTAKE)
_UPLOAD = requires(Permission.UPLOAD_DOCUMENT)
_REGISTER = requires(Permission.REGISTER_COVENANT)
_RUN_DEP = Depends(_RUN)
_UPLOAD_DEP = Depends(_UPLOAD)
_REGISTER_DEP = Depends(_REGISTER)

_LABELS: dict[str, str] = {
    "title": "Covenant intake",
    "heading": "Check covenant proposal",
    "upload_heading": "Upload a source document",
    "upload_file": "Sanction letter or source document",
    "borrower_ref": "Borrower reference",
    "facility_ref": "Facility reference",
    "doc_type": "Document type",
    "upload": "Upload and extract",
    "upload_status": "Document processing",
    "ocr": "OCR",
    "review_pages": "Pages requiring review",
    "pages": "Pages",
    "extraction_pending": "Accepted; extraction is pending.",
    "extraction_in_progress": "Text extraction is in progress.",
    "extraction_complete": "Text extraction is complete.",
    "extraction_failed": "Text extraction failed; review the error and upload a corrected file.",
    "document_accepted": (
        "Document accepted. Review its extraction status before proposing clauses."
    ),
    "ocr_complete": "OCR was applied.",
    "ocr_not_run": "OCR was not required.",
    "source_heading": "Source text",
    "proposal_heading": "Proposed fields",
    "source_document": "Source document",
    "source_page": "Page",
    "open_source": "Open source span",
    "hand_entry_source": "Hand-entered clause",
    "hand_entry_heading": "Enter covenant clause",
    "hand_entry_message": (
        "Enter the clause and its fields. The same code verifications will run "
        "before anything can be confirmed."
    ),
    "provider_unavailable": (
        "The model provider is unavailable. Hand entry is available and code "
        "verification remains active."
    ),
    "no_candidates": (
        "No covenant candidates were detected in this document. You can enter a clause manually."
    ),
    "pending_review": "Candidate review pending",
    "pending_review_message": (
        "This page needs OCR review and is excluded from proposal generation "
        "until a person corrects it."
    ),
    "verdicts": "Verification verdicts",
    "passed": "Passed",
    "struck": "Failed",
    "pending": "Pending",
    "input_safety": "Input safety",
    "input_safety_failed": "The clause was refused by the input-safety check.",
    "failed_checks": "Failed checks",
    "not_supplied": "Not supplied",
    "definition": "Definition",
    "custom_formula": "Custom formula",
    "threshold": "Threshold",
    "direction": "Direction",
    "unit": "Unit",
    "currency": "Currency",
    "frequency": "Testing frequency",
    "effective_from": "Effective from",
    "effective_to": "Effective to",
    "exceptions": "Exceptions",
    "cure_period_days": "Cure period (days)",
    "source_quote": "Source quote",
    "test_basis": "Test basis",
    "reference": "Covenant reference",
    "name": "Covenant name",
    "covenant_class": "Covenant class",
    "rerun": "Save and re-run verification",
    "verify": "Verify clause",
    "confirm": "Confirm covenant",
    "confirmed": "Confirmed",
    "abandoned": "Abandoned",
    "bulk_heading": "Confirm passing proposals",
    "bulk_message": "Only open proposals with every verification passed are included.",
    "bulk_confirm": "Confirm passing proposals",
    "approval_queue": "Sent to approval queue",
    "open_covenant": "Open covenant",
    "form_error": "The intake form needs correction.",
    "empty": "Upload a sanction letter or enter a clause to begin.",
    "detect": "Detect covenant clauses",
    "detect_message": (
        "Extraction is complete. Run clause detection to generate proposals from "
        "this document; every proposal is then verified by code before it can be "
        "confirmed."
    ),
    "detect_facility_hint": (
        "This document is not linked to a facility. Name the facility the detected "
        "covenants belong to."
    ),
    "detect_again": "Re-run clause detection",
    "detect_again_message": (
        "This document already has proposals; they are shown below. Re-running "
        "detection extracts and verifies the clauses again against the borrower's "
        "current statements."
    ),
}


def create_intake_router(
    intake_service: IntakeService,
    document_service: DocumentService,
    *,
    proposal_generator: Callable[[Sequence[ClauseCandidate]], Sequence[StageOneProposal]]
    | None = None,
    context_factory: Callable[[Principal, Facility], VerificationContext] | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected intake screen and its form actions.

    ``proposal_generator`` is the injected stage-1 adapter.  Keeping it out
    of this route makes provider-down mode deterministic and ensures the only
    model call remains the existing guarded T-094/T-089 path.  ``context_factory``
    supplies the caller's already-normalised statement snapshot to T-095;
    its conservative default fails verification when no statement snapshot is
    available rather than inventing one.
    """

    if not isinstance(intake_service, IntakeService):
        raise TypeError("create_intake_router requires an IntakeService.")
    if not isinstance(document_service, DocumentService):
        raise TypeError("create_intake_router requires a DocumentService.")
    if intake_service.session is not document_service.session:
        raise ValueError("Intake and document services must share the request session.")
    if proposal_generator is not None and not callable(proposal_generator):
        raise TypeError("proposal_generator must be callable or None.")
    if context_factory is not None and not callable(context_factory):
        raise TypeError("context_factory must be callable or None.")

    router = APIRouter(tags=["intake-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )
    detection_service = IntakeDetectionService(intake_service.session)
    facilities = FacilityRepository(intake_service.session)
    configured_scope_resolver = intake_service.scope_resolver or document_service.scope_resolver

    def request_scope(principal: Principal) -> Scope:
        return _request_scope(principal, intake_service.session, configured_scope_resolver)

    @router.get("/intake", response_class=HTMLResponse, name="intake_screen")
    @router.get("/intake/{document_id}", response_class=HTMLResponse, name="intake_document")
    async def intake_screen(
        request: Request,
        document_id: UUID | None = None,
        proposal_id: UUID | None = None,
        principal: Principal = _RUN_DEP,
    ) -> HTMLResponse:
        scope = request_scope(principal)
        view = _screen_for_request(
            principal,
            scope,
            document_id=document_id,
            proposal_id=proposal_id,
            error="",
            status_message="",
            provider_unavailable=False,
            hand_entry=document_id is None,
            form={},
            intake_service=intake_service,
            document_service=document_service,
            labels=_LABELS,
        )
        return _render(request, fallback_environment, view, principal=principal)

    @router.post("/documents", response_class=HTMLResponse, name="intake_document_upload")
    async def intake_document_upload(
        request: Request,
        principal: Principal = _UPLOAD_DEP,
    ) -> Response:
        values: dict[str, str] = {}
        try:
            form = await _upload_form(request, document_service)
            values = _string_values(form)
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise ValidationError("A source file is required.", field="file")
            borrower_ref = _required_text(values.get("borrower_ref"), "borrower_ref", maximum=20)
            doc_type = _choice(
                values.get("doc_type") or _DEFAULT_DOC_TYPE,
                "doc_type",
                DOCUMENT_TYPES,
            )
            scope = request_scope(principal)
            facility = _facility_from_values(
                values,
                facilities,
                scope,
                required=False,
            )
            document = document_service.upload_file(
                principal,
                borrower_ref=borrower_ref,
                doc_type=doc_type,
                upload=upload,
                facility_id=facility.id if facility is not None else None,
                scope=scope,
            )
            extraction_error = ""
            if document.mime_type.lower() == "application/pdf":
                try:
                    document_service.extract_document(principal, document.id, scope=scope)
                except NativePdfExtractionError as error:
                    extraction_error = error.message
            if _wants_json(request):
                return JSONResponse(
                    _document_payload(document, extraction_error=extraction_error),
                    status_code=202,
                    headers={"Location": f"/intake?document_id={document.id}"},
                )
            if principal.has(Permission.RUN_INTAKE):
                view = _screen_for_request(
                    principal,
                    scope,
                    document_id=document.id,
                    proposal_id=None,
                    error=extraction_error,
                    status_message=(
                        _LABELS["extraction_failed"]
                        if extraction_error
                        else _LABELS["document_accepted"]
                    ),
                    provider_unavailable=False,
                    hand_entry=False,
                    form={"facility_ref": values.get("facility_ref", "")},
                    intake_service=intake_service,
                    document_service=document_service,
                    labels=_LABELS,
                )
            else:
                view = _receipt_view(document, extraction_error, principal, labels=_LABELS)
            return _render(
                request, fallback_environment, view, principal=principal, status_code=202
            )
        except UploadRejected as error:
            if _wants_json(request):
                return JSONResponse(
                    {"error": "upload_rejected", "message": error.message},
                    status_code=error.status_code,
                )
            view = _empty_view(
                principal,
                error=error.message,
                status_message="",
                hand_entry=True,
                form=values,
            )
            return _render(
                request,
                fallback_environment,
                view,
                principal=principal,
                status_code=error.status_code,
            )
        except DomainError as error:
            if _wants_json(request):
                return JSONResponse(
                    {"error": error.code, "message": error.message},
                    status_code=status_for_error(error),
                )
            view = _empty_view(
                principal,
                error=error.message,
                status_message="",
                hand_entry=True,
                form=values,
            )
            return _render(
                request,
                fallback_environment,
                view,
                principal=principal,
                status_code=status_for_error(error),
            )

    @router.post("/intake/proposals", response_class=HTMLResponse, name="intake_proposals")
    async def intake_proposals(
        request: Request,
        principal: Principal = _RUN_DEP,
    ) -> Response:
        values = await _form_values(request)
        scope = request_scope(principal)
        try:
            document_id = _optional_uuid(values.get("document_id"), "document_id")
            clause_text = values.get("clause_text", "").strip()
            if document_id is not None and clause_text:
                raise ValidationError(
                    "Provide either document_id or clause_text, not both.", field="document_id"
                )
            if document_id is None and not clause_text:
                raise ValidationError(
                    "Paste or select the covenant text first.", field="clause_text"
                )
            if len(clause_text) > _MAX_CLAUSE_TEXT_LENGTH:
                raise HTTPException(
                    status_code=413,
                    detail=f"clause_text must be at most {_MAX_CLAUSE_TEXT_LENGTH} characters.",
                )

            if document_id is not None:
                document = document_service.get_document(principal, document_id, scope=scope)
                facility = _facility_for_document(document, values, facilities, scope)
                if document.extraction_state != "complete":
                    view = _screen_for_request(
                        principal,
                        scope,
                        document_id=document.id,
                        proposal_id=None,
                        error="",
                        status_message=(
                            "The document must finish extraction before proposals can be generated."
                        ),
                        provider_unavailable=False,
                        hand_entry=True,
                        form=values,
                        intake_service=intake_service,
                        document_service=document_service,
                        labels=_LABELS,
                    )
                    return _render(request, fallback_environment, view, principal=principal)
                detection = detection_service.detect_candidates(principal, document.id, scope=scope)
                if not detection.candidates:
                    view = _screen_for_request(
                        principal,
                        scope,
                        document_id=document.id,
                        proposal_id=None,
                        error="",
                        status_message=_LABELS["no_candidates"],
                        provider_unavailable=False,
                        hand_entry=True,
                        form=values,
                        intake_service=intake_service,
                        document_service=document_service,
                        labels=_LABELS,
                    )
                    return _render(request, fallback_environment, view, principal=principal)
                if proposal_generator is None:
                    return _provider_down_response(
                        request,
                        fallback_environment,
                        principal,
                        scope,
                        document,
                        form=values,
                        intake_service=intake_service,
                        document_service=document_service,
                        message=_LABELS["provider_unavailable"],
                    )
                try:
                    proposals = _generated_proposals(proposal_generator, detection)
                except ProviderError:
                    return _provider_down_response(
                        request,
                        fallback_environment,
                        principal,
                        scope,
                        document,
                        form=values,
                        intake_service=intake_service,
                        document_service=document_service,
                        message=_LABELS["provider_unavailable"],
                    )
                records = intake_service.propose_from_document(
                    principal,
                    facility_id=facility.id,
                    clauses=tuple(
                        ProposedClause(
                            proposal=proposal,
                            source_span_id=_source_span_id(
                                intake_service.session, document.id, proposal.candidate, scope
                            ),
                        )
                        for proposal in proposals
                    ),
                    context=_context_for(principal, facility, context_factory),
                    document_id=document.id,
                    # Without this a document that already carries proposals
                    # replays them forever: the service's duplicate-submission
                    # guard is right, but the screen had no way to ask for a
                    # genuine re-extraction after a correction upstream.
                    force_reextraction=_flag(values.get("force_reextraction")),
                    scope=scope,
                )
                _record_stage_one_trace(
                    intake_service,
                    principal,
                    facility,
                    document,
                    proposals,
                    records,
                )
                view = _screen_from_records(
                    principal,
                    scope,
                    records,
                    document=document,
                    error="",
                    status_message="Proposal verification completed from the stored source span.",
                    provider_unavailable=False,
                    hand_entry=False,
                    form=values,
                    intake_service=intake_service,
                    document_service=document_service,
                    labels=_LABELS,
                )
                return _render(request, fallback_environment, view, principal=principal)

            facility = _facility_from_values(values, facilities, scope, required=True)
            proposal = _hand_proposal(values)
            record = intake_service.propose_from_document(
                principal,
                facility_id=facility.id,
                clauses=(ProposedClause(proposal=proposal),),
                context=_context_for(principal, facility, context_factory),
                scope=scope,
            )[0]
            view = _screen_from_records(
                principal,
                scope,
                (record,),
                document=None,
                error="",
                status_message=(
                    "Hand-entered clause verified. Correct any struck field before retrying."
                ),
                provider_unavailable=False,
                hand_entry=True,
                form=values,
                intake_service=intake_service,
                document_service=document_service,
                labels=_LABELS,
            )
            return _render(request, fallback_environment, view, principal=principal)
        except HTTPException:
            raise
        except DomainError as error:
            if _wants_json(request):
                return JSONResponse(
                    {"error": error.code, "message": error.message},
                    status_code=status_for_error(error),
                )
            try:
                document_id = _optional_uuid(values.get("document_id"), "document_id")
            except ValidationError:
                document_id = None
            view = _screen_for_request(
                principal,
                scope,
                document_id=document_id,
                proposal_id=None,
                error=error.message,
                status_message="",
                provider_unavailable=False,
                hand_entry=document_id is None,
                form=values,
                intake_service=intake_service,
                document_service=document_service,
                labels=_LABELS,
            )
            return _render(
                request,
                fallback_environment,
                view,
                principal=principal,
                status_code=status_for_error(error),
            )

    @router.post(
        "/intake/proposals/{proposal_id}/submit",
        response_class=HTMLResponse,
        name="intake_proposal_submit",
    )
    async def intake_proposal_submit(
        request: Request,
        proposal_id: UUID,
        principal: Principal = _REGISTER_DEP,
    ) -> Response:
        values = await _form_values(request)
        scope = request_scope(principal)
        record: ProposalRecord | None = None
        try:
            record = intake_service.proposal(principal, proposal_id, scope=scope)
            facility = _facility_for_proposal(record.row.facility_id, facilities, scope)
            if values.get("correction") == "1":
                if not principal.has(Permission.RUN_INTAKE):
                    raise ValidationError(
                        "Corrected fields require RUN_INTAKE permission.", field="correction"
                    )
                corrected = _proposal_from_form(values, record.row)
                record = intake_service.correct(
                    principal,
                    proposal_id,
                    proposal=corrected,
                    context=_context_for(principal, facility, context_factory),
                    scope=scope,
                )
            submitted = intake_service.submit(
                principal,
                proposal_id,
                test_basis=_required_text(values.get("test_basis"), "test_basis", maximum=20),
                reference=_optional_text(values.get("reference")),
                name=_optional_text(values.get("name")),
                covenant_class=_optional_text(values.get("covenant_class")),
                scope=scope,
            )
        except DomainError as error:
            if _wants_json(request):
                payload: dict[str, object] = {"error": error.code, "message": error.message}
                if isinstance(error, ProposalVerificationFailed):
                    payload["failed_checks"] = list(error.failed_checks)
                return JSONResponse(payload, status_code=status_for_error(error))
            document_id = record.row.document_id if record is not None else None
            view = _screen_for_request(
                principal,
                scope,
                document_id=document_id,
                proposal_id=None if document_id is not None else proposal_id,
                error=error.message,
                status_message="",
                provider_unavailable=False,
                hand_entry=document_id is None,
                form=values,
                intake_service=intake_service,
                document_service=document_service,
                labels=_LABELS,
            )
            return _render(
                request,
                fallback_environment,
                view,
                principal=principal,
                status_code=status_for_error(error),
            )

        if submitted.approval_request is not None:
            return RedirectResponse("/covenants/approvals", status_code=303)
        return RedirectResponse(f"/covenants/{submitted.covenant.reference}", status_code=303)

    @router.post(
        "/intake/proposals/bulk-submit",
        response_class=HTMLResponse,
        name="intake_proposals_bulk_submit",
    )
    async def intake_proposals_bulk_submit(
        request: Request,
        principal: Principal = _REGISTER_DEP,
    ) -> Response:
        values = await _form_values(request)
        scope = request_scope(principal)
        proposal_ids = _uuid_list(values.get("proposal_ids"), "proposal_ids")
        if not proposal_ids:
            raise HTTPException(
                status_code=422, detail="At least one passing proposal is required."
            )
        records: list[ProposalRecord] = []
        try:
            for proposal_id in proposal_ids:
                record = intake_service.proposal(principal, proposal_id, scope=scope)
                if record.row.status != "open" or not record.outcome.all_passed:
                    raise ValidationError(
                        "Bulk confirmation accepts only open proposals with every check passed.",
                        field="proposal_ids",
                    )
                records.append(record)
            with intake_service.session.begin_nested():
                submitted = []
                for record in records:
                    submitted.append(
                        intake_service.submit(
                            principal,
                            record.row.id,
                            test_basis=_required_text(
                                values.get(f"test_basis_{record.row.id}"),
                                "test_basis",
                                maximum=20,
                            ),
                            reference=_optional_text(values.get(f"reference_{record.row.id}")),
                            name=_optional_text(values.get(f"name_{record.row.id}")),
                            covenant_class=_optional_text(
                                values.get(f"covenant_class_{record.row.id}")
                            ),
                            scope=scope,
                        )
                    )
        except DomainError as error:
            document_id = records[0].row.document_id if records else None
            view = _screen_for_request(
                principal,
                scope,
                document_id=document_id,
                proposal_id=None,
                error=error.message,
                status_message="",
                provider_unavailable=False,
                hand_entry=document_id is None,
                form=values,
                intake_service=intake_service,
                document_service=document_service,
                labels=_LABELS,
            )
            return _render(
                request,
                fallback_environment,
                view,
                principal=principal,
                status_code=status_for_error(error),
            )

        approval_needed = any(item.approval_request is not None for item in submitted)
        if approval_needed:
            return RedirectResponse("/covenants/approvals", status_code=303)
        return RedirectResponse("/covenants", status_code=303)

    return router


def _request_scope(
    principal: Principal,
    session: Session,
    resolver: Callable[[Principal], Scope] | None,
) -> Scope:
    """Resolve the same request scope used by the composed services."""
    resolved = resolver(principal) if resolver is not None else resolve_scope(principal, session)
    if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
        raise AuthorizationError(
            "The resolved intake scope does not belong to the authenticated principal."
        )
    return resolved


def _screen_for_request(
    principal: Principal,
    scope: Scope,
    *,
    document_id: UUID | None,
    proposal_id: UUID | None,
    error: str,
    status_message: str,
    provider_unavailable: bool,
    hand_entry: bool,
    form: Mapping[str, str],
    intake_service: IntakeService,
    document_service: DocumentService,
    labels: Mapping[str, str],
) -> IntakeScreenView:
    if document_id is not None:
        document = document_service.get_document(principal, document_id, scope=scope)
        records = intake_service.proposals_for_document(principal, document.id, scope=scope)
        return _screen_from_records(
            principal,
            scope,
            records,
            document=document,
            error=error,
            status_message=status_message,
            provider_unavailable=provider_unavailable,
            hand_entry=hand_entry,
            form=form,
            intake_service=intake_service,
            document_service=document_service,
            labels=labels,
        )
    if proposal_id is not None:
        record = intake_service.proposal(principal, proposal_id, scope=scope)
        document = (
            document_service.get_document(principal, record.row.document_id, scope=scope)
            if record.row.document_id is not None
            else None
        )
        return _screen_from_records(
            principal,
            scope,
            (record,),
            document=document,
            error=error,
            status_message=status_message,
            provider_unavailable=provider_unavailable,
            hand_entry=hand_entry,
            form=form,
            intake_service=intake_service,
            document_service=document_service,
            labels=labels,
        )
    return _empty_view(
        principal,
        error=error,
        status_message=status_message,
        hand_entry=hand_entry,
        form=form,
    )


def _screen_from_records(
    principal: Principal,
    scope: Scope,
    records: Sequence[ProposalRecord],
    *,
    document: Document | None,
    error: str,
    status_message: str,
    provider_unavailable: bool,
    hand_entry: bool,
    form: Mapping[str, str],
    intake_service: IntakeService,
    document_service: DocumentService,
    labels: Mapping[str, str],
) -> IntakeScreenView:
    source_documents = {record.row.document_id for record in records if record.row.document_id}
    if document is None and len(source_documents) == 1:
        document = document_service.get_document(
            principal,
            next(iter(source_documents)),
            scope=scope,
        )
    pending = _pending_reviews(document_service, principal, document, scope)
    document_view = (
        build_document_view(document, review_page_count=len(pending), labels=labels)
        if document is not None
        else None
    )
    proposal_views = tuple(
        build_proposal_view(
            record,
            source_span=(
                _source_span(intake_service.session, record.row.source_span_id, scope)
                if record.row.source_span_id is not None
                else None
            ),
            document=document,
            labels=labels,
            can_confirm=principal.has(Permission.REGISTER_COVENANT),
        )
        for record in records
    )
    bulk_ids = tuple(proposal.proposal_id for proposal in proposal_views if proposal.bulk_included)
    status = status_message or (labels["provider_unavailable"] if provider_unavailable else "")
    return IntakeScreenView(
        document=document_view,
        proposals=proposal_views,
        pending_reviews=pending,
        form=form,
        error=error,
        status_message=status,
        provider_unavailable=provider_unavailable,
        hand_entry=hand_entry,
        can_upload=principal.has(Permission.UPLOAD_DOCUMENT),
        can_run_intake=principal.has(Permission.RUN_INTAKE),
        bulk_proposal_ids=bulk_ids,
    )


def _empty_view(
    principal: Principal,
    *,
    error: str,
    status_message: str,
    hand_entry: bool,
    form: Mapping[str, str],
) -> IntakeScreenView:
    return IntakeScreenView(
        document=None,
        proposals=(),
        pending_reviews=(),
        form=form,
        error=error,
        status_message=status_message,
        provider_unavailable=False,
        hand_entry=hand_entry,
        can_upload=principal.has(Permission.UPLOAD_DOCUMENT),
        can_run_intake=principal.has(Permission.RUN_INTAKE),
        bulk_proposal_ids=(),
    )


def _receipt_view(
    document: Document,
    error: str,
    principal: Principal,
    *,
    labels: Mapping[str, str],
) -> IntakeScreenView:
    return IntakeScreenView(
        document=build_document_view(document, review_page_count=0, labels=labels),
        proposals=(),
        pending_reviews=(),
        form={},
        error=error,
        status_message=labels["extraction_failed"] if error else "Document accepted.",
        provider_unavailable=False,
        hand_entry=False,
        can_upload=principal.has(Permission.UPLOAD_DOCUMENT),
        can_run_intake=principal.has(Permission.RUN_INTAKE),
        bulk_proposal_ids=(),
    )


def _provider_down_response(
    request: Request,
    environment: Environment,
    principal: Principal,
    scope: Scope,
    document: Document,
    *,
    form: Mapping[str, str],
    intake_service: IntakeService,
    document_service: DocumentService,
    message: str,
) -> HTMLResponse:
    view = _screen_for_request(
        principal,
        scope,
        document_id=document.id,
        proposal_id=None,
        error="",
        status_message=message,
        provider_unavailable=True,
        hand_entry=True,
        form=form,
        intake_service=intake_service,
        document_service=document_service,
        labels=_LABELS,
    )
    return _render(request, environment, view, principal=principal)


def _pending_reviews(
    document_service: DocumentService,
    principal: Principal,
    document: Document | None,
    scope: Scope,
) -> tuple[IntakePendingReviewView, ...]:
    if document is None:
        return ()
    rows = tuple(
        item
        for item in document_service.list_review_pages(principal, scope=scope)
        if item.document.id == document.id
    )
    return tuple(
        IntakePendingReviewView(
            page_number=item.page.page_number,
            reason=item.reason,
            href=f"/documents/{document.id}/view?page={item.page.page_number}",
        )
        for item in rows
    )


def _source_span(
    session: Session,
    span_id: UUID,
    scope: Scope,
) -> DocumentSpan | None:
    return RepositoryBase(session, DocumentSpan).get(span_id, scope=scope)


def _source_span_id(
    session: Session,
    document_id: UUID,
    candidate: ClauseCandidate,
    scope: Scope,
) -> UUID | None:
    line = candidate.lines[0]
    ownership = ownership_path_for(DocumentSpan)
    statement = ownership.apply(select(DocumentSpan)).where(
        DocumentSpan.document_id == document_id,
        DocumentSpan.page_number == line.page_number,
        DocumentSpan.start_offset == line.start_offset,
        DocumentSpan.end_offset == line.end_offset,
        scope.predicate(ownership.path_column),
    )
    for row in session.execute(statement).scalars():
        if not is_history_span(row.span_type):
            return row.id
    return None


def _generated_proposals(
    generator: Callable[[Sequence[ClauseCandidate]], Sequence[StageOneProposal]],
    detection: DetectionResult,
) -> tuple[StageOneProposal, ...]:
    generated = tuple(generator(detection.candidates))
    if len(generated) != len(detection.candidates):
        raise RuntimeError("The stage-1 proposal generator returned an incomplete result set.")
    for expected, proposal in zip(detection.candidates, generated, strict=True):
        if not isinstance(proposal, StageOneProposal) or proposal.candidate != expected:
            raise RuntimeError(
                "The stage-1 proposal generator returned an invalid candidate mapping."
            )
    return generated


def _record_stage_one_trace(
    intake_service: IntakeService,
    principal: Principal,
    facility: Facility,
    document: Document,
    proposals: Sequence[StageOneProposal],
    records: Sequence[ProposalRecord],
) -> None:
    """Persist the model-backed intake decision for the borrower why-panel.

    The model call ledger answers operational questions about a request.  The
    borrower trace answers the reviewer-facing question of what the intake
    stage proposed and what the deterministic verification accepted or
    refused.  Both are required: previously the former existed but no stage-1
    trace was written, so the why-panel could only report "not run".
    """

    if len(proposals) != len(records):
        raise RuntimeError("Cannot trace stage 1 with an incomplete proposal result set.")

    items: list[dict[str, object]] = []
    sources: list[dict[str, str]] = [{"type": "document", "id": str(document.id)}]
    for proposal, record in zip(proposals, records, strict=True):
        row = record.row
        if row.source_span_id is not None:
            sources.append({"type": "document_span", "id": str(row.source_span_id)})
        sources.append({"type": "covenant_proposal", "id": str(row.id)})
        items.append(
            {
                "proposal_id": str(row.id),
                "parseable": proposal.parseable,
                "parse_error": proposal.parse_error,
                "definition_ref": proposal.definition_ref,
                "threshold": proposal.threshold,
                "unit": proposal.unit,
                "direction": proposal.direction,
                "frequency": proposal.frequency,
                "effective_from": proposal.effective_from,
                "effective_to": proposal.effective_to,
                "verification_passed": record.outcome.all_passed,
                "failed_checks": list(record.outcome.failed_checks),
                "injection_detected": record.outcome.injection_detected,
                "refusal_message": record.outcome.refusal_message,
            }
        )

    TraceRepository(intake_service.session, request_id=intake_service.request_id).write(
        TraceSubject("borrower", facility.borrower_id),
        stage_record(
            1,
            "model",
            {
                "document_id": str(document.id),
                "candidate_count": len(proposals),
                "candidates": [
                    {
                        "start_page": proposal.candidate.start_page,
                        "end_page": proposal.candidate.end_page,
                        "matched_rules": list(proposal.candidate.matched_rules),
                    }
                    for proposal in proposals
                ],
            },
            {"proposal_count": len(items), "proposals": items},
            "stage1_extract.v1",
            (),
            Decimal("1"),
            sources,
        ),
        actor_id=principal.id,
    )


def _hand_proposal(values: Mapping[str, str]) -> StageOneProposal:
    clause_text = _required_text(
        values.get("clause_text"), "clause_text", maximum=_MAX_CLAUSE_TEXT_LENGTH
    )
    return _parse_form_proposal(values, _candidate_for_text(clause_text))


def _proposal_from_form(values: Mapping[str, str], row: Any) -> StageOneProposal:
    clause_text = _required_text(
        values.get("clause_text") or getattr(row, "clause_text", ""),
        "clause_text",
        maximum=_MAX_CLAUSE_TEXT_LENGTH,
    )
    return _parse_form_proposal(values, _candidate_for_text(clause_text))


def _parse_form_proposal(
    values: Mapping[str, str],
    candidate: ClauseCandidate,
) -> StageOneProposal:
    exceptions = [
        item.strip()
        for item in values.get("exceptions", "").replace(",", "\n").splitlines()
        if item.strip()
    ]
    if len(exceptions) > _MAX_EXCEPTIONS:
        raise ValidationError(
            f"exceptions must contain at most {_MAX_EXCEPTIONS} entries.", field="exceptions"
        )
    payload = {
        "definition": _optional_text(values.get("definition")),
        "custom_formula": _optional_text(values.get("custom_formula")),
        "threshold": _optional_text(values.get("threshold")),
        "direction": _choice_or_none(values.get("direction"), DIRECTION_WORDS, "direction"),
        "unit": _choice_or_none(values.get("unit"), UNIT_KINDS, "unit"),
        "currency": _optional_text(values.get("currency")),
        "frequency": _choice_or_none(values.get("frequency"), FREQUENCY_WORDS, "frequency"),
        "effective_from": _optional_text(values.get("effective_from")),
        "effective_to": _optional_text(values.get("effective_to")),
        "exceptions": exceptions,
        "cure_period_days": _optional_text(values.get("cure_period_days")),
        "source_quote": _required_text(
            values.get("source_quote") or candidate.text,
            "source_quote",
            maximum=4_000,
        ),
    }
    return parse_stage1_reply(candidate, json.dumps(payload, ensure_ascii=False))


def _candidate_for_text(text: str) -> ClauseCandidate:
    line = CandidateLine(page_number=1, start_offset=0, end_offset=len(text), text=text)
    return ClauseCandidate(
        start_page=1,
        start_offset=0,
        end_page=1,
        end_offset=len(text),
        text=text,
        matched_rules=("hand_entry",),
        lines=(line,),
    )


def _flag(value: object) -> bool:
    """Read a checkbox/hidden form flag as a boolean."""
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "on", "yes"}


def _context_for(
    principal: Principal,
    facility: Facility,
    factory: Callable[[Principal, Facility], VerificationContext] | None,
) -> VerificationContext:
    if factory is not None:
        context = factory(principal, facility)
        if not isinstance(context, VerificationContext):
            raise TypeError("context_factory must return a VerificationContext.")
        return context
    return VerificationContext(
        statement_lines={},
        period_complete=False,
        facility_facts=FacilityFacts(
            sanctioned_limit=facility.sanctioned_limit,
            outstanding=facility.outstanding,
            drawing_power=facility.drawing_power,
        ),
        facility_sanction_date=facility.sanction_date,
        facility_currency=facility.currency,
    )


def _facility_for_document(
    document: Document,
    values: Mapping[str, str],
    facilities: FacilityRepository,
    scope: Scope,
) -> Facility:
    # A document uploaded against a facility already carries the authoritative
    # answer, so `facility_ref` is only *required* when the document has none.
    # Demanding it either way left the document-driven detection control with
    # no value it could send, since the screen never shows the reference.
    facility = _facility_from_values(
        values, facilities, scope, required=document.facility_id is None
    )
    if document.facility_id is not None:
        stored = facilities.get(document.facility_id, scope=scope)
        if stored is None:
            raise NotFound("The document's facility was not found within the current scope.")
        if facility is not None and facility.id != stored.id:
            raise ValidationError(
                "facility_ref does not match the uploaded document's facility.",
                field="facility_ref",
            )
        return stored
    if facility is None:
        raise ValidationError("facility_ref is required for this document.", field="facility_ref")
    return facility


def _facility_from_values(
    values: Mapping[str, str],
    facilities: FacilityRepository,
    scope: Scope,
    *,
    required: bool,
) -> Facility | None:
    raw = (values.get("facility_ref") or values.get("facility_id") or "").strip()
    if not raw:
        if required:
            raise ValidationError("facility_ref is required.", field="facility_ref")
        return None
    try:
        facility = facilities.get(UUID(raw), scope=scope)
    except ValueError:
        facility = facilities.by_reference(raw, scope=scope)
    if facility is None:
        raise NotFound(f"Facility {raw!r} was not found within the current scope.")
    return facility


def _facility_for_proposal(
    facility_id: UUID,
    facilities: FacilityRepository,
    scope: Scope,
) -> Facility:
    facility = facilities.get(facility_id, scope=scope)
    if facility is None:
        raise NotFound("The proposal's facility was not found within the current scope.")
    return facility


async def _upload_form(request: Request, document_service: DocumentService) -> FormData:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "multipart/form-data":
        raise ValidationError("The upload must use multipart/form-data.", field="file")
    max_part_size = document_service.scans.guard.policy.max_bytes + 64 * 1024
    return await request.form(
        max_files=1,
        max_fields=_MAX_MULTIPART_FIELDS,
        max_part_size=max_part_size,
    )


async def _form_values(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted form is too large.", field="form")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type in {"application/x-www-form-urlencoded", ""}:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError("The submitted form is not valid UTF-8.", field="form") from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values and key != "csrf_token"}
    if content_type == "multipart/form-data":
        async with request.form(max_files=0, max_fields=_MAX_MULTIPART_FIELDS) as form:
            return _string_values(form)
    raise ValidationError("The submitted form encoding is not supported.", field="form")


def _string_values(form: FormData) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in form.multi_items():
        if key == "csrf_token" or not isinstance(value, str):
            continue
        values[key] = value
    return values


def _required_text(value: str | None, field: str, *, maximum: int) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise ValidationError(f"{field} is required.", field=field)
    if len(normalized) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    return normalized


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _choice(value: str, field: str, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise ValidationError(f"{field} must be one of {', '.join(sorted(allowed))}.", field=field)
    return value


def _choice_or_none(value: str | None, allowed: Sequence[str], field: str) -> str | None:
    normalized = _optional_text(value)
    return None if normalized is None else _choice(normalized, field, allowed)


def _optional_uuid(value: str | None, field: str) -> UUID | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    try:
        return UUID(normalized)
    except ValueError as error:
        raise ValidationError(f"{field} must be a UUID.", field=field) from error


def _uuid_list(value: str | None, field: str) -> tuple[UUID, ...]:
    normalized = _optional_text(value)
    if normalized is None:
        return ()
    result: list[UUID] = []
    for item in normalized.split(","):
        parsed = _optional_uuid(item, field)
        if parsed is None:
            raise ValidationError(f"{field} contains an empty UUID.", field=field)
        if parsed not in result:
            result.append(parsed)
        if len(result) > _MAX_BULK_PROPOSALS:
            raise ValidationError(
                f"{field} must contain at most {_MAX_BULK_PROPOSALS} proposal ids.",
                field=field,
            )
    return tuple(result)


def _document_payload(document: Document, *, extraction_error: str) -> dict[str, object]:
    return {
        "document_id": str(document.id),
        "extraction_state": document.extraction_state,
        "ocr_applied": document.ocr_applied,
        "page_count": document.page_count,
        "error": extraction_error or None,
    }


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "").lower()


def _render(
    request: Request,
    fallback_environment: Environment,
    view: IntakeScreenView,
    *,
    principal: Principal,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/intake/index.html")
    values = {
        "request": request,
        "principal": principal,
        "locale": request.cookies.get("covenant_radar_locale", "en"),
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "view": view,
        "form": view.form,
        "error": view.error,
        "can_register": principal.has(Permission.REGISTER_COVENANT),
        "form_options": _FORM_OPTIONS,
    }
    response = HTMLResponse(template.render(**values), status_code=status_code)
    response.headers["Vary"] = "HX-Request, HX-Target"
    return response


__all__ = ["create_intake_router"]
