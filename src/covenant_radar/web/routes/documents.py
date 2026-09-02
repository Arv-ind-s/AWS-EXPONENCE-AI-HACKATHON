"""Document review routes for OCR pages held out of clause detection."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import DomainError, ValidationError
from covenant_radar.documents.classify import (
    DOCUMENT_TYPES,
    UNCLASSIFIED_DOC_TYPE,
    ClassificationResult,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.documents import DocumentService
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 1_100_000
_READ = requires(Permission.VIEW_DOCUMENT)
_CORRECT = requires(Permission.CORRECT_SOURCE_DATA)
_OVERRIDE = requires(Permission.CORRECT_SOURCE_DATA)
_READ_DEP = Depends(_READ)
_CORRECT_DEP = Depends(_CORRECT)
_OVERRIDE_DEP = Depends(_OVERRIDE)

_LABELS = {
    "title": "Document review",
    "heading": "Pages requiring review",
    "empty": "No document pages require review.",
    "capability": "OCR capability",
    "available": "Available",
    "unavailable": "Unavailable",
    "document": "Document",
    "page": "Page",
    "reason": "Review reason",
    "extracted_text": "Extracted text",
    "correction": "Corrected text",
    "correct": "Save correction",
    "required": "Corrected text is required",
    "form_error": "The correction could not be saved",
    "source_retained": "The original page text is retained in provenance.",
    "viewer_title": "Document viewer",
    "classification": "Classification",
    "unclassified": "Unclassified — needs manual selection",
    "no_text": "No extracted text is available for this page.",
    "span_missing": "The requested passage could not be located precisely on this page.",
    "corrected_note": "This page's text was corrected during review; the current text is shown.",
    "overridden_by": "Manually classified as",
    "override_heading": "Manual classification",
    "override_doc_type": "Document type",
    "override_reason": "Reason",
    "override_submit": "Save classification",
    "override_required": "A document type and reason are required.",
    "back_to_review": "Back to document review",
}

_DOC_TYPE_LABELS = {
    "sanction_letter": "Sanction letter",
    "amendment": "Amendment",
    "compliance_certificate": "Compliance certificate",
    "stock_statement": "Stock statement",
    "other": "Other",
}


def create_documents_router(
    service: DocumentService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build protected OCR review and correction routes over one service."""
    if not isinstance(service, DocumentService):
        raise TypeError("create_documents_router requires a DocumentService.")
    router = APIRouter(tags=["documents-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/documents/review", response_class=HTMLResponse, name="document_review")
    async def document_review(
        request: Request,
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        rows = service.list_review_pages(principal)
        return _render(
            request,
            fallback_environment,
            principal=principal,
            rows=rows,
            error="",
            capability=service.ocr_pipeline.capability,
        )

    @router.post(
        "/documents/{document_id}/pages/{page_number}/correct",
        response_class=HTMLResponse,
        name="document_page_correct",
    )
    async def document_page_correct(
        request: Request,
        document_id: UUID,
        page_number: int,
        principal: Principal = _CORRECT_DEP,
        _viewer: Principal = _READ_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.correct_page(
                principal,
                document_id,
                page_number,
                values.get("corrected_text", ""),
                expected_version=_optional_positive_int(values.get("expected_version")),
            )
        except DomainError as error:
            rows = service.list_review_pages(principal)
            return _render(
                request,
                fallback_environment,
                principal=principal,
                rows=rows,
                error=error.message,
                status_code=422,
                capability=service.ocr_pipeline.capability,
            )
        return RedirectResponse("/documents/review", status_code=303)

    @router.get(
        "/documents/{document_id}/view",
        response_class=HTMLResponse,
        name="document_viewer",
    )
    async def document_viewer(
        request: Request,
        document_id: UUID,
        page: int = 1,
        start: int | None = None,
        end: int | None = None,
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        document = service.get_document(principal, document_id)
        page_row = service.get_page(principal, document_id, page)
        return _render_viewer(
            request,
            fallback_environment,
            principal=principal,
            document=document,
            page_row=page_row,
            classification=service.classify_document(principal, document_id),
            override=service.get_classification_override(principal, document_id),
            was_corrected=service.page_was_corrected(principal, document_id, page),
            highlight=_resolve_highlight(page_row.text, start, end),
            span_start=start,
            span_end=end,
            error="",
        )

    @router.post(
        "/documents/{document_id}/classification/override",
        response_class=HTMLResponse,
        name="document_classification_override",
    )
    async def document_classification_override(
        request: Request,
        document_id: UUID,
        principal: Principal = _OVERRIDE_DEP,
        _viewer: Principal = _READ_DEP,
    ) -> Response:
        values = await _form_values(request)
        page = _optional_positive_int(values.get("page")) or 1
        try:
            service.override_classification(
                principal,
                document_id,
                values.get("doc_type", ""),
                values.get("reason", ""),
            )
        except DomainError as error:
            document = service.get_document(principal, document_id)
            page_row = service.get_page(principal, document_id, page)
            return _render_viewer(
                request,
                fallback_environment,
                principal=principal,
                document=document,
                page_row=page_row,
                classification=service.classify_document(principal, document_id),
                override=service.get_classification_override(principal, document_id),
                was_corrected=service.page_was_corrected(principal, document_id, page),
                highlight=None,
                span_start=None,
                span_end=None,
                error=error.message,
                status_code=422,
            )
        return RedirectResponse(f"/documents/{document_id}/view?page={page}", status_code=303)

    return router


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    rows: object,
    error: str,
    capability: object,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/documents/_review.html")
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    theme = theme_for_request(request)
    values = {
        "request": request,
        "principal": principal,
        "locale": locale,
        "theme": theme,
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "rows": rows,
        "error": error,
        "capability": capability,
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


def _render_viewer(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    document: object,
    page_row: object,
    classification: ClassificationResult,
    override: object,
    was_corrected: bool,
    highlight: str | None,
    span_start: int | None,
    span_end: int | None,
    error: str,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/documents/_viewer.html")
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    theme = theme_for_request(request)
    values = {
        "request": request,
        "principal": principal,
        "locale": locale,
        "theme": theme,
        "text_direction": "ltr",
        "labels": _LABELS,
        "doc_type_labels": _DOC_TYPE_LABELS,
        "doc_type_options": tuple((value, _DOC_TYPE_LABELS[value]) for value in DOCUMENT_TYPES),
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "document": document,
        "page": page_row,
        "classification": classification,
        "classification_confidence_display": f"{classification.confidence:.2f}",
        "unclassified_doc_type": UNCLASSIFIED_DOC_TYPE,
        "override": override,
        "was_corrected": was_corrected,
        "highlight": highlight,
        "span_start": span_start,
        "span_end": span_end,
        "error": error,
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


def _resolve_highlight(text: str | None, start: int | None, end: int | None) -> str | None:
    """Resolve a highlighted excerpt from the page's current, live text.

    Offsets are validated against the page's *current* text, not a stored
    span row. That is deliberate: after a reviewer correction replaces a
    page's text, an old span link's offsets may no longer align, and a link
    must still open the corrected page rather than fail. The correction is
    surfaced separately by ``was_corrected``.
    """
    if text is None or start is None or end is None:
        return None
    if start < 0 or end <= start or end > len(text):
        return None
    return text[start:end]


async def _form_values(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted correction is too large.", field="corrected_text")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/x-www-form-urlencoded" or not content_type:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError(
                "The submitted correction is not valid UTF-8.", field="corrected_text"
            ) from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values and key != "csrf_token"}
    form = await request.form()
    values: dict[str, str] = {}
    for key, value in form.multi_items():
        if key != "csrf_token" and isinstance(value, str):
            values[key] = value
    return values


def _optional_positive_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValidationError(
            "expected_version must be a positive integer.", field="expected_version"
        ) from error
    if parsed < 1:
        raise ValidationError(
            "expected_version must be a positive integer.", field="expected_version"
        )
    return parsed


__all__ = ["create_documents_router"]
