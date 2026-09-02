"""Integration test for T-093: clause-candidate detection recall over the
labelled fixture documents, exercised through the real extraction pipeline
and the scoped `IntakeDetectionService` — not the domain function directly,
so a mismatch between what native extraction actually produces and what the
detector expects would fail here even if the unit tests still passed.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from covenant_radar.domain.intake.candidates import (
    DEFAULT_RECALL_FLOOR,
    ClauseCandidate,
    DetectionResult,
    measure_recall,
)
from covenant_radar.services.intake import IntakeDetectionService
from tests.integration.test_document_upload import _Fixture

pytestmark = pytest.mark.integration

# The labelled ground truth: every line below is a real financial covenant in
# Indian sanction-letter idiom, seeded verbatim into one of the two fixture
# documents alongside ordinary boilerplate. Recall is how many of these five
# a full extract-then-detect run recovers.
_GROUND_TRUTH: tuple[str, ...] = (
    "Current ratio shall be maintained above 1.20",
    "DSCR shall not fall below 1.25 times at all times",
    "Total outside liabilities shall not exceed 3.00 times tangible net worth",
    "Minimum net worth shall not fall below 50.00 crore at all times",
    "Promoter shareholding shall not fall below 51.00 percent",
)

_DOCUMENT_A_PAGES: tuple[tuple[str, ...], ...] = (
    (
        "This sanction letter records our approval of your credit facility.",
        "Current ratio shall be maintained above 1.20",
        "DSCR shall not fall below 1.25 times at all times",
    ),
    (
        "Please find enclosed the sanction letter for your reference.",
        "Total outside liabilities shall not exceed 3.00 times tangible net worth",
        "Yours sincerely, Branch Manager",
    ),
)

_DOCUMENT_B_PAGES: tuple[tuple[str, ...], ...] = (
    (
        "The borrower shall submit the stock statement every month.",
        "Minimum net worth shall not fall below 50.00 crore at all times",
        "Promoter shareholding shall not fall below 51.00 percent",
        "This sanction is subject to the terms and conditions mentioned herein.",
    ),
)


def test_recall_on_fixture_documents_meets_floor(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        service = IntakeDetectionService(fixture.session)
        all_candidates: tuple[ClauseCandidate, ...] = ()
        rules_tried: tuple[str, ...] = ()

        for pages, filename in (
            (_DOCUMENT_A_PAGES, "fixture-a.pdf"),
            (_DOCUMENT_B_PAGES, "fixture-b.pdf"),
        ):
            document = fixture.upload(filename=filename, content=_pdf_document(pages))
            fixture.service.extract_document(fixture.principal, document.id, scope=fixture.scope)
            result = service.detect_candidates(fixture.principal, document.id, scope=fixture.scope)
            all_candidates += result.candidates
            rules_tried = result.rules_tried

        combined = DetectionResult(candidates=all_candidates, rules_tried=rules_tried)
        report = measure_recall(combined, _GROUND_TRUTH)

        assert report.recall >= DEFAULT_RECALL_FLOOR, (
            f"Recall {report.recall} fell below the floor {DEFAULT_RECALL_FLOOR}; "
            f"missed: {report.missed}"
        )
    finally:
        fixture.close()


def _pdf_document(pages: tuple[tuple[str, ...], ...]) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for lines in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        stream = DecodedStreamObject()
        stream.set_data(_content_stream(lines).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _content_stream(lines: tuple[str, ...]) -> str:
    parts = ["BT", "/F1 11 Tf", "72 740 Td"]
    for index, line in enumerate(lines):
        if index > 0:
            parts.append("0 -18 Td")
        parts.append(f"({line}) Tj")
    parts.append("ET")
    return " ".join(parts)
