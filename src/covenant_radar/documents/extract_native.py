"""Native PDF text extraction with deterministic layout-aware spans.

This adapter deliberately treats a PDF as untrusted input.  pypdf performs a
strict structural preflight, while pdfplumber supplies word coordinates.  A
complete result is built in memory and returned only after every page has
been processed, so callers can persist it atomically and never expose a
partially extracted document.
"""

from __future__ import annotations

import io
import math
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median
from typing import Any, BinaryIO

import pdfplumber
from pypdf import PdfReader

from covenant_radar.core.errors import ValidationError
from covenant_radar.documents.spans import SpanIndex, TextSpan

_PDF_ROTATIONS = frozenset({0, 90, 180, 270})
_FULL_WIDTH_RATIO = 0.70
_COLUMN_GAP_RATIO = 0.15
_MIN_COLUMN_LINES = 2
_DEFAULT_X_TOLERANCE = 1.0
_DEFAULT_Y_TOLERANCE = 3.0


class NativePdfExtractionError(ValidationError):
    """A PDF could not be safely extracted, with the failing page named."""

    def __init__(self, page_number: int, reason: str) -> None:
        if not isinstance(page_number, int) or page_number < 1:
            raise ValueError("Native PDF extraction errors require a one-based page number.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Native PDF extraction errors require a reason.")
        self.page_number = page_number
        self.reason = reason.strip()[:500]
        super().__init__(
            f"Native PDF extraction failed on page {page_number}: {self.reason}",
            field=f"document.page[{page_number}]",
        )


@dataclass(frozen=True, slots=True)
class NativePage:
    """Normalized text and provenance for one PDF page."""

    page_number: int
    text: str | None
    spans: tuple[TextSpan, ...]
    width: float
    height: float
    rotation: int
    needs_ocr: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or self.page_number < 1
        ):
            raise ValueError("Native page numbers must be one-based.")
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("Native page text must be a string or None.")
        if self.text is None and self.spans:
            raise ValueError("A page without text cannot contain spans.")
        if self.text is not None and not self.text.strip():
            raise ValueError("A text-bearing native page must contain non-whitespace text.")
        if isinstance(self.width, bool) or not isinstance(self.width, int | float):
            raise TypeError("Native page width must be numeric.")
        if not math.isfinite(self.width) or self.width <= 0:
            raise ValueError("Native page width must be a positive finite number.")
        if isinstance(self.height, bool) or not isinstance(self.height, int | float):
            raise TypeError("Native page height must be numeric.")
        if not math.isfinite(self.height) or self.height <= 0:
            raise ValueError("Native page height must be a positive finite number.")
        if self.rotation not in _PDF_ROTATIONS:
            raise ValueError("Native page rotation must be one of 0, 90, 180, or 270 degrees.")
        if not isinstance(self.needs_ocr, bool):
            raise TypeError("Native page needs_ocr must be boolean.")
        if self.needs_ocr != (self.text is None):
            raise ValueError("Native page OCR state must match whether text was extracted.")
        expected_start = 0
        for span in self.spans:
            if span.page_number != self.page_number:
                raise ValueError("A native page span belongs to a different page.")
            if span.start_offset < expected_start:
                raise ValueError("Native page spans must be in normalized reading order.")
            if self.text is None or self.text[span.start_offset : span.end_offset] != span.text:
                raise ValueError("Native page span text does not match page text offsets.")
            expected_start = span.end_offset

    @property
    def page_text(self) -> str | None:
        """Compatibility name for callers that distinguish page from span data."""
        return self.text


@dataclass(frozen=True, slots=True)
class NativeExtractionResult:
    """All pages and spans from one successfully parsed PDF."""

    pages: tuple[NativePage, ...]

    def __post_init__(self) -> None:
        if not self.pages:
            raise ValueError("Native extraction must contain at least one page.")
        for expected_page, page in enumerate(self.pages, start=1):
            if not isinstance(page, NativePage):
                raise TypeError("Native extraction pages must be NativePage values.")
            if page.page_number != expected_page:
                raise ValueError("Native extraction pages must be contiguous and one-based.")

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def pages_needing_ocr(self) -> tuple[int, ...]:
        return tuple(page.page_number for page in self.pages if page.needs_ocr)

    @property
    def extraction_state(self) -> str:
        """The document-level state after native extraction has completed."""
        return "complete"

    @property
    def span_index(self) -> SpanIndex:
        """Build an independent bounds-checked index over this result."""
        index = SpanIndex({page.page_number: page.text or "" for page in self.pages})
        for page in self.pages:
            for span in page.spans:
                index.add(span)
        return index

    @property
    def span_count(self) -> int:
        return sum(len(page.spans) for page in self.pages)


@dataclass(slots=True)
class _Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    bbox: tuple[float, float, float, float]

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(slots=True)
class _Line:
    words: list[_Word]

    @property
    def top(self) -> float:
        return min(word.top for word in self.words)

    @property
    def bottom(self) -> float:
        return max(word.bottom for word in self.words)

    @property
    def left(self) -> float:
        return min(word.x0 for word in self.words)

    @property
    def right(self) -> float:
        return max(word.x1 for word in self.words)

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def width(self) -> float:
        return self.right - self.left


class NativePdfExtractor:
    """Extract native text and line-level coordinate spans from a PDF."""

    def __init__(
        self,
        *,
        x_tolerance: float = _DEFAULT_X_TOLERANCE,
        y_tolerance: float = _DEFAULT_Y_TOLERANCE,
    ) -> None:
        for name, value in (("x_tolerance", x_tolerance), ("y_tolerance", y_tolerance)):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"Native PDF {name} must be numeric.")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Native PDF {name} must be finite and non-negative.")
        self.x_tolerance = float(x_tolerance)
        self.y_tolerance = float(y_tolerance)

    def extract(self, data: bytes | bytearray | memoryview | BinaryIO) -> NativeExtractionResult:
        """Parse all pages, refusing encrypted, corrupt, or partially read PDFs."""
        pdf_bytes = _read_pdf_bytes(data)
        if not pdf_bytes:
            raise NativePdfExtractionError(1, "the PDF content is empty")

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
            if reader.is_encrypted:
                raise NativePdfExtractionError(1, "the PDF is encrypted or password-protected")
            page_count = len(reader.pages)
            if page_count < 1:
                raise NativePdfExtractionError(1, "the PDF contains no pages")
        except NativePdfExtractionError:
            raise
        except Exception as error:
            raise NativePdfExtractionError(
                1,
                _safe_reason(error, "the PDF structure is damaged"),
            ) from error

        pages: list[NativePage] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes), strict_metadata=True) as pdf:
                if len(pdf.pages) != page_count:
                    raise NativePdfExtractionError(
                        1,
                        "the PDF page catalogue changed during extraction",
                    )
                for page_index, (pdf_page, pypdf_page) in enumerate(
                    zip(pdf.pages, reader.pages, strict=True), start=1
                ):
                    pages.append(self._extract_page(pdf_page, pypdf_page, page_index))
        except NativePdfExtractionError:
            raise
        except Exception as error:
            page_number = len(pages) + 1
            raise NativePdfExtractionError(
                page_number,
                _safe_reason(error, "the PDF page is damaged or cannot be decoded"),
            ) from error
        return NativeExtractionResult(tuple(pages))

    def _extract_page(self, pdf_page: Any, pypdf_page: Any, page_number: int) -> NativePage:
        try:
            rotation = _page_rotation(pypdf_page, pdf_page)
            width = float(pdf_page.width)
            height = float(pdf_page.height)
            raw_words = pdf_page.extract_words(
                x_tolerance=self.x_tolerance,
                y_tolerance=self.y_tolerance,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            words = _normalize_words(raw_words, width, height)
        except NativePdfExtractionError:
            raise
        except Exception as error:
            raise NativePdfExtractionError(
                page_number,
                _safe_reason(error, "the page text or layout cannot be decoded"),
            ) from error

        if not words:
            return NativePage(
                page_number=page_number,
                text=None,
                spans=(),
                width=width,
                height=height,
                rotation=rotation,
                needs_ocr=True,
            )

        lines = _group_words_into_lines(words, self.y_tolerance, width)
        ordered_lines = _reading_order(lines, width)
        text, spans = _build_text_and_spans(page_number, ordered_lines)
        if not text:
            return NativePage(
                page_number=page_number,
                text=None,
                spans=(),
                width=width,
                height=height,
                rotation=rotation,
                needs_ocr=True,
            )
        return NativePage(
            page_number=page_number,
            text=text,
            spans=tuple(spans),
            width=width,
            height=height,
            rotation=rotation,
            needs_ocr=False,
        )


def _read_pdf_bytes(data: bytes | bytearray | memoryview | BinaryIO) -> bytes:
    if isinstance(data, bytes | bytearray | memoryview):
        return bytes(data)
    reader = getattr(data, "read", None)
    if not callable(reader):
        raise TypeError("Native PDF extraction requires bytes or a binary stream.")
    try:
        original_position = data.tell() if callable(getattr(data, "tell", None)) else None
        if callable(getattr(data, "seek", None)):
            data.seek(0)
        content = data.read()
        if original_position is not None and callable(getattr(data, "seek", None)):
            data.seek(original_position)
    except (OSError, ValueError) as error:
        raise NativePdfExtractionError(1, "the PDF stream could not be read") from error
    if not isinstance(content, bytes):
        raise TypeError("Native PDF extraction requires a binary stream.")
    return content


def _page_rotation(pypdf_page: Any, pdfplumber_page: Any) -> int:
    raw_rotation = getattr(pypdf_page, "rotation", None)
    if raw_rotation is None:
        raw_rotation = getattr(pdfplumber_page, "rotation", 0)
    try:
        rotation = int(raw_rotation or 0) % 360
    except (TypeError, ValueError) as error:
        raise ValueError("the page rotation is invalid") from error
    if rotation not in _PDF_ROTATIONS:
        raise ValueError(f"the page rotation {rotation} is not supported")
    return rotation


def _normalize_words(
    raw_words: Iterable[dict[str, Any]], width: float, height: float
) -> list[_Word]:
    words: list[_Word] = []
    for raw_word in raw_words:
        text = raw_word.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        coordinates = tuple(
            _word_coordinate(raw_word, key) for key in ("x0", "x1", "top", "bottom")
        )
        x0, x1, top, bottom = coordinates
        if x1 < x0 or bottom < top:
            raise ValueError("the page contains a word with inverted coordinates")
        if x0 < 0 or x1 > width or top < 0 or bottom > height:
            raise ValueError("the page contains coordinates outside its bounds")
        bbox = (x0, height - bottom, x1, height - top)
        words.append(_Word(text=text.strip(), x0=x0, x1=x1, top=top, bottom=bottom, bbox=bbox))
    return sorted(words, key=lambda word: (word.top, word.x0, word.bottom, word.text))


def _word_coordinate(raw_word: dict[str, Any], key: str) -> float:
    value = raw_word.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("the page contains a word with invalid coordinates")
    if not math.isfinite(value):
        raise ValueError("the page contains a word with invalid coordinates")
    return float(value)


def _group_words_into_lines(
    words: list[_Word], y_tolerance: float, page_width: float
) -> list[_Line]:
    if not words:
        return []
    heights = [word.bottom - word.top for word in words if word.bottom > word.top]
    line_tolerance = max(y_tolerance, (median(heights) * 0.5 if heights else y_tolerance))
    horizontal_tolerance = max(30.0, page_width * 0.08)
    lines: list[_Line] = []
    for word in words:
        matching_line: _Line | None = None
        best_distance = float("inf")
        for line in lines:
            distance = abs(word.center_y - (line.top + line.bottom) / 2)
            horizontal_gap = max(line.left - word.x1, word.x0 - line.right, 0.0)
            if (
                distance <= line_tolerance
                and horizontal_gap <= horizontal_tolerance
                and distance < best_distance
            ):
                matching_line = line
                best_distance = distance
        if matching_line is None:
            lines.append(_Line([word]))
        else:
            matching_line.words.append(word)
    for line in lines:
        line.words.sort(key=lambda word: (word.x0, word.top, word.text))
    return sorted(lines, key=lambda line: (line.top, line.left))


def _reading_order(lines: list[_Line], page_width: float) -> list[_Line]:
    if len(lines) < _MIN_COLUMN_LINES * 2:
        return sorted(lines, key=lambda line: (line.top, line.left))
    full_width_lines = [line for line in lines if line.width >= page_width * _FULL_WIDTH_RATIO]
    column_lines = [line for line in lines if line not in full_width_lines]
    if len(column_lines) < _MIN_COLUMN_LINES * 2:
        return sorted(lines, key=lambda line: (line.top, line.left))

    centers = sorted(line.center_x for line in column_lines)
    gaps = [(centers[index + 1] - centers[index], index) for index in range(len(centers) - 1)]
    candidates = [
        (gap, index)
        for gap, index in gaps
        if gap >= page_width * _COLUMN_GAP_RATIO
        and index + 1 >= _MIN_COLUMN_LINES
        and len(centers) - index - 1 >= _MIN_COLUMN_LINES
    ]
    if not candidates:
        return sorted(lines, key=lambda line: (line.top, line.left))
    cut_indexes = [index for _, index in candidates]
    groups: list[list[_Line]] = []
    lower_center_index = 0
    for cut_index in cut_indexes:
        upper_center_index = cut_index + 1
        group_centers = centers[lower_center_index:upper_center_index]
        group = sorted(
            [
                line
                for line in column_lines
                if group_centers[0] <= line.center_x <= group_centers[-1]
            ],
            key=lambda line: (line.top, line.left),
        )
        groups.append(group)
        lower_center_index = upper_center_index
    final_group_centers = centers[lower_center_index:]
    groups.append(
        sorted(
            [
                line
                for line in column_lines
                if final_group_centers[0] <= line.center_x <= final_group_centers[-1]
            ],
            key=lambda line: (line.top, line.left),
        )
    )
    if len(groups) < 2 or any(len(group) < _MIN_COLUMN_LINES for group in groups):
        return sorted(lines, key=lambda line: (line.top, line.left))

    first_column_top = min(line.top for line in column_lines)
    last_column_bottom = max(line.bottom for line in column_lines)
    before = sorted(
        [line for line in full_width_lines if line.bottom <= first_column_top],
        key=lambda line: (line.top, line.left),
    )
    after = sorted(
        [line for line in full_width_lines if line.top >= last_column_bottom],
        key=lambda line: (line.top, line.left),
    )
    middle = sorted(
        [line for line in full_width_lines if line not in before and line not in after],
        key=lambda line: (line.top, line.left),
    )
    return before + [line for group in groups for line in group] + middle + after


def _build_text_and_spans(page_number: int, lines: Iterable[_Line]) -> tuple[str, list[TextSpan]]:
    parts: list[str] = []
    spans: list[TextSpan] = []
    current_offset = 0
    for line in lines:
        line_text = _join_words(line.words)
        if not line_text:
            continue
        if parts:
            parts.append("\n")
            current_offset += 1
        line_start = current_offset
        parts.append(line_text)
        current_offset += len(line_text)
        spans.append(
            TextSpan(
                page_number=page_number,
                start_offset=line_start,
                end_offset=current_offset,
                text=line_text,
                bbox=_line_bbox(line),
                span_type="line",
            )
        )
    return "".join(parts), spans


def _join_words(words: Iterable[_Word]) -> str:
    result = ""
    for word in words:
        if result and not _no_space_before(word.text) and not _no_space_after(result[-1]):
            result += " "
        result += word.text
    return result.strip()


def _no_space_before(text: str) -> bool:
    return text[0] in ",.;:!?%)]}" if text else False


def _no_space_after(character: str) -> bool:
    return character in "([{/$"


def _line_bbox(line: _Line) -> tuple[float, float, float, float]:
    return (
        min(word.bbox[0] for word in line.words),
        min(word.bbox[1] for word in line.words),
        max(word.bbox[2] for word in line.words),
        max(word.bbox[3] for word in line.words),
    )


def _safe_reason(error: Exception, fallback: str) -> str:
    reason = str(error).strip().replace("\n", " ")
    return reason[:500] if reason else fallback


def extract_native(
    data: bytes | bytearray | memoryview | BinaryIO,
    *,
    x_tolerance: float = _DEFAULT_X_TOLERANCE,
    y_tolerance: float = _DEFAULT_Y_TOLERANCE,
) -> NativeExtractionResult:
    """Extract one PDF using a freshly configured native extractor."""
    return NativePdfExtractor(
        x_tolerance=x_tolerance,
        y_tolerance=y_tolerance,
    ).extract(data)


extract_pdf = extract_native
NativePdfPage = NativePage
NativeTextSpan = TextSpan
ExtractionResult = NativeExtractionResult


__all__ = [
    "ExtractionResult",
    "NativeExtractionResult",
    "NativePage",
    "NativePdfExtractionError",
    "NativePdfExtractor",
    "NativePdfPage",
    "NativeTextSpan",
    "extract_native",
    "extract_pdf",
]
