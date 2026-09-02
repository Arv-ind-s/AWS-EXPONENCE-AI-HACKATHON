"""Integration coverage for T-086 OCR, confidence gating, and corrections."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from covenant_radar.db.models.document import DocumentPage, DocumentSpan
from covenant_radar.documents.ocr import (
    OcrPageResult,
    OcrPipeline,
    RenderedPage,
    spans_from_text,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from tests.integration.test_document_upload import _Fixture

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


class _Renderer:
    def render(self, _document: bytes, page) -> RenderedPage:
        return RenderedPage(b"test-image", 1000, 1000)


class _Recognizer:
    def __init__(self, confidence: Decimal = Decimal("0.80")) -> None:
        self.confidence = confidence
        self.pages: list[int] = []

    def recognize(
        self,
        _image: RenderedPage,
        *,
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> OcrPageResult:
        del page_width, page_height
        self.pages.append(page_number)
        text = f"OCR page {page_number} current ratio 1.20"
        return OcrPageResult(
            page_number,
            text,
            self.confidence,
            spans_from_text(page_number, text),
        )


class _FailingRecognizer:
    def recognize(
        self,
        _image: RenderedPage,
        *,
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> OcrPageResult:
        del page_number, page_width, page_height
        raise RuntimeError("OCR worker returned an unreadable image")


def _pipeline(recognizer: _Recognizer) -> OcrPipeline:
    return OcrPipeline(engine=recognizer, renderer=_Renderer())


def _extract_with_pipeline(fixture: _Fixture, pipeline: OcrPipeline):
    fixture.service.ocr_pipeline = pipeline
    document = fixture.upload(content=_blank_pdf())
    fixture.service.extract_document(fixture.principal, document.id, scope=fixture.scope)
    fixture.session.expire_all()
    return document


def test_only_textless_pages_ocrd(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    recognizer = _Recognizer()
    fixture.service.ocr_pipeline = _pipeline(recognizer)
    try:
        document = fixture.upload(content=_mixed_pdf())
        fixture.service.extract_document(fixture.principal, document.id, scope=fixture.scope)

        assert recognizer.pages == [2]
        pages = tuple(
            fixture.session.scalars(
                select(DocumentPage)
                .where(DocumentPage.document_id == document.id)
                .order_by(DocumentPage.page_number)
            ).all()
        )
        assert pages[0].text is not None
        assert pages[0].ocr_confidence is None
        assert pages[1].text == "OCR page 2 current ratio 1.20"
    finally:
        fixture.close()


def test_confidence_stored_per_page(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = _extract_with_pipeline(
            fixture,
            _pipeline(_Recognizer(confidence=Decimal("0.8765"))),
        )
        page = fixture.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == 1,
            )
        )
        assert page is not None
        assert page.ocr_confidence == Decimal("0.8765")
    finally:
        fixture.close()


def test_page_at_floor_used(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = _extract_with_pipeline(fixture, _pipeline(_Recognizer()))
        page = fixture.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == 1,
            )
        )
        assert page is not None
        assert page.needs_review is False
        assert page.ocr_confidence == Decimal("0.8000")
        detection_pages = fixture.service.list_detection_pages(
            fixture.principal, scope=fixture.scope
        )
        assert len(detection_pages) == 1
    finally:
        fixture.close()


def test_below_floor_flagged_and_excluded_from_detection(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = _extract_with_pipeline(
            fixture,
            _pipeline(_Recognizer(confidence=Decimal("0.7999"))),
        )
        page = fixture.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == 1,
            )
        )
        assert page is not None
        assert page.needs_review is True
        assert page.text is not None
        assert fixture.service.list_detection_pages(fixture.principal, scope=fixture.scope) == ()
        active_spans = fixture.service.lookup_spans(
            fixture.principal,
            document.id,
            1,
            0,
            len(page.text),
            scope=fixture.scope,
        )
        assert active_spans == ()
    finally:
        fixture.close()


def test_capability_absent_flags_with_reason(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = _extract_with_pipeline(
            fixture,
            OcrPipeline.disabled("OCR executable is unavailable on this host."),
        )
        page = fixture.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == 1,
            )
        )
        assert page is not None
        assert page.needs_review is True
        assert page.ocr_confidence is None
        queue = fixture.service.list_review_pages(fixture.principal, scope=fixture.scope)
        assert len(queue) == 1
        assert "OCR" in queue[0].reason
        audit = fixture.audit.events[-1]
        assert audit[0] == "document_ocr_processed"
        assert audit[2]["capability_available"] is False
        assert audit[2]["capability_detail"] == "OCR executable is unavailable on this host."
    finally:
        fixture.close()


def test_recognizer_failure_is_held_for_review_not_raised(tmp_path: Path) -> None:
    """A page-level OCR failure must not prevent the document from being indexed safely."""

    fixture = _Fixture(tmp_path)
    try:
        pipeline = OcrPipeline(engine=_FailingRecognizer(), renderer=_Renderer())
        document = _extract_with_pipeline(fixture, pipeline)
        page = fixture.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == 1,
            )
        )
        assert page is not None
        assert page.text is None
        assert page.needs_review is True
        assert fixture.service.list_detection_pages(fixture.principal, scope=fixture.scope) == ()
        queue = fixture.service.list_review_pages(fixture.principal, scope=fixture.scope)
        assert len(queue) == 1
        assert queue[0].reason == "OCR is unavailable or did not return readable text."
        assert "unreadable image" in fixture.audit.events[-1][2]["pages"][0]["reason"]
    finally:
        fixture.close()


def test_correction_stored_as_new_version_original_retained(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = _extract_with_pipeline(
            fixture,
            _pipeline(_Recognizer(confidence=Decimal("0.7999"))),
        )
        original = "OCR page 1 current ratio 1.20"
        corrected = "Corrected page 1 current ratio 1.20"
        page = fixture.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == 1,
            )
        )
        assert page is not None and page.text == original
        corrected_page = fixture.service.correct_page(
            Principal.user(
                fixture.principal.id,
                (
                    Permission.VIEW_DOCUMENT,
                    Permission.UPLOAD_DOCUMENT,
                    Permission.CORRECT_SOURCE_DATA,
                ),
            ),
            document.id,
            1,
            corrected,
            expected_version=document.version,
            scope=fixture.scope,
        )
        assert corrected_page.text == corrected
        assert corrected_page.needs_review is False
        assert corrected_page.ocr_confidence == Decimal("1.0000")
        stored_spans = tuple(
            fixture.session.scalars(
                select(DocumentSpan)
                .where(
                    DocumentSpan.document_id == document.id,
                    DocumentSpan.page_number == 1,
                )
                .order_by(DocumentSpan.created_at, DocumentSpan.id)
            ).all()
        )
        assert any(
            span.text == original and span.span_type.startswith("page_text_version:")
            for span in stored_spans
        )
        assert any(
            span.text == corrected and span.span_type.startswith("page_text_version:")
            for span in stored_spans
        )
        detection_pages = fixture.service.list_detection_pages(
            fixture.principal, scope=fixture.scope
        )
        assert len(detection_pages) == 1
        assert fixture.audit.events[-1][0] == "document_page_corrected"
    finally:
        fixture.close()


def test_correction_accepts_browser_crlf_line_endings(tmp_path: Path) -> None:
    """A browser submits textarea line breaks as CRLF; the correction must not be rejected."""

    fixture = _Fixture(tmp_path)
    try:
        document = _extract_with_pipeline(
            fixture,
            _pipeline(_Recognizer(confidence=Decimal("0.7999"))),
        )
        submitted = "4.1 Interest Coverage Ratio\r\n\r\nNot less than 2.50 : 1.00."
        corrected_page = fixture.service.correct_page(
            Principal.user(
                fixture.principal.id,
                (
                    Permission.VIEW_DOCUMENT,
                    Permission.UPLOAD_DOCUMENT,
                    Permission.CORRECT_SOURCE_DATA,
                ),
            ),
            document.id,
            1,
            submitted,
            expected_version=document.version,
            scope=fixture.scope,
        )
        assert "\r" not in corrected_page.text
        assert corrected_page.text == "4.1 Interest Coverage Ratio\n\nNot less than 2.50 : 1.00."
        assert corrected_page.needs_review is False
    finally:
        fixture.close()


def _mixed_pdf() -> bytes:
    reader = PdfReader(BytesIO(_fixture_pdf()))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _fixture_pdf() -> bytes:
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "documents" / "two_column_sanction.pdf"
    )
    return fixture_path.read_bytes()
