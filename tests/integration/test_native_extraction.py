"""Integration tests for T-085 native PDF extraction and persistence."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select

from covenant_radar.db.models.document import DocumentPage, DocumentSpan
from covenant_radar.documents.extract_native import NativePdfExtractionError
from tests.integration.test_document_upload import _Fixture

pytestmark = pytest.mark.integration


def test_two_column_reading_order(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = fixture.upload(content=_fixture_pdf())

        result = fixture.service.extract_document(
            fixture.principal, document.id, scope=fixture.scope
        )

        assert result.pages[0].text == (
            "Current ratio shall be maintained above 1.20\nLEFT TWO\nLEFT THREE\n"
            "RIGHT ONE\nRIGHT TWO\nRIGHT THREE"
        )
        assert [span.text for span in result.pages[0].spans] == [
            "Current ratio shall be maintained above 1.20",
            "LEFT TWO",
            "LEFT THREE",
            "RIGHT ONE",
            "RIGHT TWO",
            "RIGHT THREE",
        ]
    finally:
        fixture.close()


def test_no_text_page_marked_for_ocr(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = fixture.upload(content=_pdf_bytes(""))

        result = fixture.service.extract_document(
            fixture.principal, document.id, scope=fixture.scope
        )
        pages = tuple(
            fixture.session.scalars(
                select(DocumentPage).where(DocumentPage.document_id == document.id)
            ).all()
        )

        assert result.pages[0].text is None
        assert result.pages[0].needs_ocr is True
        assert pages[0].text is None
        assert pages[0].needs_review is True
        assert (
            fixture.session.scalar(
                select(func.count(DocumentSpan.id)).where(DocumentSpan.document_id == document.id)
            )
            == 0
        )
    finally:
        fixture.close()


def test_damaged_pdf_refused_naming_page(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = fixture.upload(content=b"%PDF-1.7\nnot a valid PDF")

        with pytest.raises(NativePdfExtractionError, match=r"page 1"):
            fixture.service.extract_document(fixture.principal, document.id, scope=fixture.scope)

        fixture.session.expire_all()
        stored = fixture.session.get(type(document), document.id)
        assert stored is not None
        assert stored.extraction_state == "failed"
        assert (
            fixture.session.scalar(
                select(func.count(DocumentPage.id)).where(DocumentPage.document_id == document.id)
            )
            == 0
        )
        assert (
            fixture.session.scalar(
                select(func.count(DocumentSpan.id)).where(DocumentSpan.document_id == document.id)
            )
            == 0
        )
    finally:
        fixture.close()


def test_known_clause_span_resolves_to_expected_page_and_offsets(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    clause = "Current ratio shall be maintained above 1.20"
    try:
        document = fixture.upload(content=_fixture_pdf())

        result = fixture.service.extract_document(
            fixture.principal, document.id, scope=fixture.scope
        )
        start = result.pages[0].text.index(clause) if result.pages[0].text else -1
        end = start + len(clause)
        spans = fixture.service.lookup_spans(
            fixture.principal,
            document.id,
            1,
            start,
            end,
            scope=fixture.scope,
        )

        assert len(spans) == 1
        assert spans[0].page_number == 1
        assert spans[0].start_offset == start
        assert spans[0].end_offset == end
        assert spans[0].text == clause
        assert spans[0].bbox is not None
    finally:
        fixture.close()


def _fixture_pdf() -> bytes:
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "documents" / "two_column_sanction.pdf"
    )
    return fixture_path.read_bytes()


def _pdf_bytes(commands: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    if commands:
        stream = DecodedStreamObject()
        stream.set_data(commands.encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
