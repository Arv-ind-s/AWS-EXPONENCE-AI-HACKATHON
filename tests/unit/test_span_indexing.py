"""Unit tests for T-085's bounds-checked span index."""

from __future__ import annotations

import pytest

from covenant_radar.documents.extract_native import NativePdfExtractor
from covenant_radar.documents.spans import SpanBoundsError, SpanIndex, TextSpan


def test_span_lookup_by_offsets() -> None:
    index = SpanIndex({1: "Current ratio above 1.20\nQuarterly testing"})
    first = TextSpan(1, 0, 24, "Current ratio above 1.20", (10, 20, 200, 40))
    second = TextSpan(1, 25, 42, "Quarterly testing", (10, 50, 200, 70))
    index.add(first)
    index.add(second)

    assert index.lookup(1, 8, 18) == (first,)
    assert index.lookup(1, 20, 35) == (first, second)


def test_out_of_bounds_refused() -> None:
    index = SpanIndex({3: "A short page"})

    with pytest.raises(SpanBoundsError, match=r"page 3.*0:12"):
        index.lookup(3, 0, 13)


def test_rotation_normalised_and_recorded() -> None:
    data = _single_page_pdf("ROTATED PAGE", rotation=90)

    result = NativePdfExtractor().extract(data)
    page = result.pages[0]

    assert page.rotation == 90
    assert (page.width, page.height) == (792.0, 612.0)
    assert page.text == "ROTATED\nPAGE"
    assert all(0 <= coordinate for span in page.spans for coordinate in span.bbox or ())


def _single_page_pdf(text: str, *, rotation: int = 0) -> bytes:
    """Build a tiny native-text PDF without a test-only PDF generator dependency."""
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

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
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 730 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    if rotation:
        page[NameObject("/Rotate")] = NumberObject(rotation)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
