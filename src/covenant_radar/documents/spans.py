"""Immutable character-span values and bounds-checked span indexing.

The PDF extractor produces offsets against the normalized page text rather
than against the PDF content stream.  Keeping that convention in one small
value object means the database adapter, clause detector, and viewer all use
the same half-open range semantics: ``start_offset <= offset < end_offset``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import isfinite
from numbers import Real

_BBOX_LENGTH = 4


class SpanBoundsError(ValueError):
    """A requested page or character range is not present in the index."""

    def __init__(self, page_number: int, start_offset: int, end_offset: int, page_length: int):
        self.page_number = page_number
        self.start_offset = start_offset
        self.end_offset = end_offset
        self.page_length = page_length
        super().__init__(
            f"Span range {start_offset}:{end_offset} is outside page {page_number}; "
            f"valid bounds are 0:{page_length}."
        )


@dataclass(frozen=True, slots=True)
class TextSpan:
    """One normalized, addressable region of page text."""

    page_number: int
    start_offset: int
    end_offset: int
    text: str
    bbox: tuple[float, float, float, float] | None = None
    span_type: str = "line"

    def __post_init__(self) -> None:
        if not isinstance(self.page_number, int) or isinstance(self.page_number, bool):
            raise TypeError("Span page_number must be an integer.")
        if self.page_number < 1:
            raise ValueError("Span page_number must be one-based.")
        _validate_offsets(self.start_offset, self.end_offset)
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("A text span requires non-empty text.")
        if len(self.text) != self.end_offset - self.start_offset:
            raise ValueError("A text span's offsets must match its text length.")
        if not isinstance(self.span_type, str) or not self.span_type.strip():
            raise ValueError("A text span requires a non-empty span_type.")
        if self.bbox is not None:
            object.__setattr__(self, "bbox", _normalize_bbox(self.bbox))

    @property
    def length(self) -> int:
        """Return the number of normalized characters in this span."""
        return self.end_offset - self.start_offset


class SpanIndex:
    """Bounds-checked index of text spans grouped by one-based page number."""

    def __init__(
        self,
        pages: Mapping[int, str] | Iterable[tuple[int, str]] | None = None,
        spans: Iterable[TextSpan] = (),
    ) -> None:
        self._page_text: dict[int, str] = {}
        self._page_lengths: dict[int, int] = {}
        self._spans: dict[int, list[TextSpan]] = {}
        if pages is not None:
            page_items = pages.items() if isinstance(pages, Mapping) else pages
            for page_number, text in page_items:
                self.add_page(page_number, text)
        for span in spans:
            self.add(span)

    def add_page(self, page_number: int, text: str | None) -> None:
        """Register a page and its normalized text length."""
        if not isinstance(page_number, int) or isinstance(page_number, bool):
            raise TypeError("Span page_number must be an integer.")
        if page_number < 1:
            raise ValueError("Span page_number must be one-based.")
        if text is not None and not isinstance(text, str):
            raise TypeError("Indexed page text must be a string or None.")
        if page_number in self._page_lengths:
            raise ValueError(f"Page {page_number} is already indexed.")
        normalized_text = text or ""
        self._page_text[page_number] = normalized_text
        self._page_lengths[page_number] = len(normalized_text)
        self._spans[page_number] = []

    def add(self, span: TextSpan) -> None:
        """Add a span after checking it fits its registered page."""
        if not isinstance(span, TextSpan):
            raise TypeError("SpanIndex accepts TextSpan values only.")
        if span.page_number not in self._page_lengths:
            raise KeyError(f"Page {span.page_number} is not indexed.")
        self._check_bounds(
            span.page_number,
            span.start_offset,
            span.end_offset,
        )
        if self._page_text[span.page_number][span.start_offset : span.end_offset] != span.text:
            raise ValueError("A text span does not match the indexed page text.")
        page_spans = self._spans[span.page_number]
        page_spans.append(span)
        page_spans.sort(key=lambda item: (item.start_offset, item.end_offset, item.span_type))

    def lookup(self, page_number: int, start_offset: int, end_offset: int) -> tuple[TextSpan, ...]:
        """Return spans intersecting a validated half-open character range."""
        self._check_bounds(page_number, start_offset, end_offset)
        return tuple(
            span
            for span in self._spans[page_number]
            if span.start_offset < end_offset and span.end_offset > start_offset
        )

    def spans_for_page(self, page_number: int) -> tuple[TextSpan, ...]:
        """Return all spans on a page in normalized reading order."""
        if page_number not in self._page_lengths:
            raise KeyError(f"Page {page_number} is not indexed.")
        return tuple(self._spans[page_number])

    def page_length(self, page_number: int) -> int:
        """Return the normalized character length of a registered page."""
        try:
            return self._page_lengths[page_number]
        except KeyError as error:
            raise KeyError(f"Page {page_number} is not indexed.") from error

    def __len__(self) -> int:
        return sum(len(page_spans) for page_spans in self._spans.values())

    def _check_bounds(self, page_number: int, start_offset: int, end_offset: int) -> None:
        if page_number not in self._page_lengths:
            raise SpanBoundsError(page_number, start_offset, end_offset, 0)
        page_length = self._page_lengths[page_number]
        if (
            not isinstance(start_offset, int)
            or isinstance(start_offset, bool)
            or not isinstance(end_offset, int)
            or isinstance(end_offset, bool)
        ):
            raise TypeError("Span offsets must be integers.")
        if start_offset < 0 or end_offset > page_length:
            raise SpanBoundsError(page_number, start_offset, end_offset, page_length)
        _validate_offsets(start_offset, end_offset)


def _validate_offsets(start_offset: int, end_offset: int) -> None:
    if any(
        not isinstance(offset, int) or isinstance(offset, bool)
        for offset in (start_offset, end_offset)
    ):
        raise TypeError("Span offsets must be integers.")
    if start_offset < 0 or end_offset <= start_offset:
        raise ValueError("Span offsets must be a non-empty half-open range.")


def _normalize_bbox(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, tuple | list) or len(value) != _BBOX_LENGTH:
        raise ValueError("A span bbox must contain exactly four coordinates.")
    coordinates: list[float] = []
    for coordinate in value:
        if not isinstance(coordinate, Real) or isinstance(coordinate, bool):
            raise TypeError("A span bbox coordinate must be numeric.")
        number = float(coordinate)
        if not isfinite(number):
            raise ValueError("A span bbox coordinate must be finite.")
        coordinates.append(number)
    x0, y0, x1, y1 = coordinates
    if x1 < x0 or y1 < y0:
        raise ValueError("A span bbox must be ordered as x0, y0, x1, y1.")
    return (x0, y0, x1, y1)


Span = TextSpan


__all__ = ["Span", "SpanBoundsError", "SpanIndex", "TextSpan"]
