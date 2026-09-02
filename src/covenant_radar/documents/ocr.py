"""OCR for pages that have no usable native PDF text.

The OCR boundary is deliberately small and dependency-light.  PDFium renders
one page to an in-memory PNG and Tesseract is invoked without a shell.  The
adapter captures word coordinates and confidence from Tesseract's TSV output,
normalises them to the same page-text/span convention as native extraction,
and never treats an unavailable or malformed OCR result as readable text.

Page corrections use the existing document-span provenance table.  A full
page version span retains the original and corrected text while ordinary
active spans remain the only spans exposed to downstream clause detection.
This keeps the correction history append-only within the files owned by this
task and avoids making a reviewer overwrite source evidence.
"""

from __future__ import annotations

import csv
import io
import math
import os
import shlex
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from covenant_radar.core.errors import ExternalServiceError, ValidationError
from covenant_radar.documents.extract_native import NativeExtractionResult, NativePage
from covenant_radar.documents.spans import TextSpan

_DEFAULT_RENDER_SCALE = 2.0
_DEFAULT_TIMEOUT_SECONDS = 60.0
_MIN_RENDER_SCALE = Decimal("0.5")
_MAX_RENDER_SCALE = Decimal("6")
_MAX_TSV_BYTES = 16 * 1024 * 1024
_MAX_PAGE_TEXT_LENGTH = 1_000_000
_MAX_REASON_LENGTH = 500
_PAGE_VERSION_SPAN_PREFIX = "page_text_version:"
_SUPERSEDED_SPAN_TYPE = "superseded"


class OcrError(ExternalServiceError):
    """Base class for a controlled OCR failure."""


class OcrUnavailable(OcrError):
    """OCR is not configured or its executable cannot be started."""


class OcrProcessingError(OcrError):
    """OCR returned an unusable result for one page."""


@dataclass(frozen=True, slots=True)
class OcrCapability:
    """The current configured OCR capability and an operator-facing reason."""

    available: bool
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise TypeError("OCR capability availability must be boolean.")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("OCR capability detail must be non-empty.")
        if len(self.detail) > _MAX_REASON_LENGTH:
            raise ValueError("OCR capability detail is too long.")


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """One rendered page image and its pixel dimensions."""

    image: bytes
    pixel_width: int
    pixel_height: int

    def __post_init__(self) -> None:
        if not isinstance(self.image, bytes) or not self.image:
            raise ValueError("A rendered OCR page requires non-empty image bytes.")
        for name, value in (("pixel_width", self.pixel_width), ("pixel_height", self.pixel_height)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Rendered OCR page {name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class OcrToken:
    """A recognised word in rendered-image pixel coordinates."""

    text: str
    confidence: Decimal
    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("An OCR token requires non-empty text.")
        confidence = _fraction(self.confidence, "OCR token confidence")
        object.__setattr__(self, "confidence", confidence)
        for name, value in (
            ("left", self.left),
            ("top", self.top),
            ("width", self.width),
            ("height", self.height),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"OCR token {name} must be an integer.")
            if value < 0 or name in {"width", "height"} and value < 1:
                raise ValueError(f"OCR token {name} is outside the valid range.")


@dataclass(frozen=True, slots=True)
class OcrPageResult:
    """OCR output for one page before the confidence-floor decision."""

    page_number: int
    text: str | None
    confidence: Decimal | None
    spans: tuple[TextSpan, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int):
            raise TypeError("OCR page numbers must be integers.")
        if self.page_number < 1:
            raise ValueError("OCR page numbers must be one-based.")
        if self.text is not None:
            if not isinstance(self.text, str) or not self.text.strip():
                raise ValueError("OCR page text must be non-empty or None.")
            if len(self.text) > _MAX_PAGE_TEXT_LENGTH:
                raise ValueError("OCR page text exceeds the supported page limit.")
        if self.confidence is not None:
            object.__setattr__(self, "confidence", _fraction(self.confidence, "OCR confidence"))
        if self.text is None and self.spans:
            raise ValueError("An OCR page without text cannot contain spans.")
        if self.text is not None:
            for span in self.spans:
                if span.page_number != self.page_number:
                    raise ValueError("An OCR span belongs to a different page.")
            _validate_spans(self.text, self.spans)
        if self.reason is not None:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("An OCR page reason must be non-empty or None.")
            object.__setattr__(self, "reason", self.reason.strip()[:_MAX_REASON_LENGTH])


@dataclass(frozen=True, slots=True)
class OcrExtractionResult:
    """All OCR outcomes for the pages selected by native extraction."""

    pages: tuple[OcrPageResult, ...]
    capability: OcrCapability
    attempted_pages: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        page_numbers = [page.page_number for page in self.pages]
        if page_numbers != sorted(set(page_numbers)):
            raise ValueError("OCR result pages must be unique and ordered.")
        if any(not isinstance(page, OcrPageResult) for page in self.pages):
            raise TypeError("OCR result pages must be OcrPageResult values.")
        if any(page_number < 1 for page_number in self.attempted_pages):
            raise ValueError("OCR attempted page numbers must be one-based.")
        if tuple(sorted(set(self.attempted_pages))) != self.attempted_pages:
            raise ValueError("OCR attempted page numbers must be unique and ordered.")

    @property
    def pages_needing_review(self) -> tuple[int, ...]:
        """Return the one-based pages held back from automated detection."""
        return tuple(page.page_number for page in self.pages if self.needs_review(page))

    @staticmethod
    def needs_review(page: OcrPageResult) -> bool:
        """Whether an OCR result is not safe for automated clause detection."""
        return page.reason is not None


class PageRenderer(Protocol):
    """Render a selected PDF page to an image."""

    def render(self, document: bytes, page: NativePage) -> RenderedPage:
        """Render one native-extraction page."""


class OcrEngine(Protocol):
    """Recognise text and coordinates from one rendered page."""

    def recognize(
        self,
        image: RenderedPage,
        *,
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> OcrPageResult:
        """Return an unclassified OCR page result."""


class PdfiumPageRenderer:
    """Render PDF pages with the pinned PDFium dependency, in memory."""

    def __init__(self, *, scale: float = _DEFAULT_RENDER_SCALE) -> None:
        if isinstance(scale, bool) or not isinstance(scale, int | float):
            raise TypeError("OCR render scale must be numeric.")
        scale_decimal = Decimal(str(scale))
        if (
            not math.isfinite(float(scale))
            or not _MIN_RENDER_SCALE <= scale_decimal <= _MAX_RENDER_SCALE
        ):
            raise ValueError("OCR render scale must be between 0.5 and 6.0.")
        self.scale = float(scale)

    def render(self, document: bytes, page: NativePage) -> RenderedPage:
        if not isinstance(document, bytes) or not document:
            raise OcrProcessingError("OCR cannot render an empty document.")
        try:
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(document)
        except Exception as error:
            raise OcrProcessingError("The PDF page could not be rendered for OCR.") from error

        pdf_page: Any = None
        bitmap: Any = None
        try:
            if page.page_number > len(pdf):
                raise OcrProcessingError(f"The PDF does not contain page {page.page_number}.")
            pdf_page = pdf[page.page_number - 1]
            bitmap = pdf_page.render(scale=self.scale)
            pil_image = bitmap.to_pil()
            output = io.BytesIO()
            pil_image.save(output, format="PNG", optimize=False)
            image = output.getvalue()
            width, height = pil_image.size
            return RenderedPage(image=image, pixel_width=width, pixel_height=height)
        except OcrProcessingError:
            raise
        except Exception as error:
            raise OcrProcessingError(
                f"PDF page {page.page_number} could not be rendered for OCR."
            ) from error
        finally:
            if bitmap is not None:
                bitmap.close()
            if pdf_page is not None:
                pdf_page.close()
            pdf.close()


class TesseractOcrEngine:
    """Run a configured Tesseract executable and parse its TSV response."""

    def __init__(
        self,
        command: str | Sequence[str],
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        parts = _command_parts(command)
        executable = _resolve_executable(parts[0])
        if executable is None:
            raise OcrUnavailable(f"OCR executable {parts[0]!r} is not available.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
            raise TypeError("OCR timeout must be numeric.")
        if not 1.0 <= float(timeout_seconds) <= 600.0:
            raise ValueError("OCR timeout must be between 1 and 600 seconds.")
        self.command = (executable, *parts[1:])
        self.timeout_seconds = float(timeout_seconds)

    @property
    def executable(self) -> str:
        """Return the resolved executable path used for diagnostics."""
        return self.command[0]

    def recognize(
        self,
        image: RenderedPage,
        *,
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> OcrPageResult:
        command = (*self.command, "stdin", "stdout", "tsv")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                input=image.image,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
                creationflags=creation_flags,
            )
        except FileNotFoundError as error:
            raise OcrUnavailable("The configured OCR executable could not be started.") from error
        except subprocess.TimeoutExpired as error:
            raise OcrProcessingError(f"OCR timed out on page {page_number}.") from error
        except OSError as error:
            raise OcrUnavailable("The configured OCR executable could not be started.") from error
        if completed.returncode != 0:
            detail = _safe_reason(completed.stderr, "Tesseract returned an error")
            raise OcrProcessingError(f"OCR failed on page {page_number}: {detail}")
        if len(completed.stdout) > _MAX_TSV_BYTES:
            raise OcrProcessingError(f"OCR output on page {page_number} is too large.")
        return _parse_tsv(
            completed.stdout,
            page_number=page_number,
            page_width=page_width,
            page_height=page_height,
            pixel_width=image.pixel_width,
            pixel_height=image.pixel_height,
        )


class OcrPipeline:
    """Select textless pages, OCR them, and apply the inclusive T9 floor."""

    def __init__(
        self,
        *,
        engine: OcrEngine | None = None,
        renderer: PageRenderer | None = None,
        command: str | Sequence[str] | None = None,
        confidence_floor: Decimal | str | float | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        unavailable_reason: str | None = None,
    ) -> None:
        if engine is not None and command is not None:
            raise TypeError("OCR pipeline accepts either engine or command, not both.")
        configured_floor = (
            _configured_confidence_floor() if confidence_floor is None else confidence_floor
        )
        self.confidence_floor = _fraction(configured_floor, "OCR confidence floor")
        self.renderer = renderer or PdfiumPageRenderer()
        if not callable(getattr(self.renderer, "render", None)):
            raise TypeError("OCR renderer must expose render().")
        self._unavailable_reason = _bounded_reason(unavailable_reason)
        if engine is not None:
            if not callable(getattr(engine, "recognize", None)):
                raise TypeError("OCR engine must expose recognize().")
            self.engine = engine
            self._capability = OcrCapability(True, "configured")
        elif command is not None:
            try:
                self.engine = TesseractOcrEngine(command, timeout_seconds=timeout_seconds)
            except OcrUnavailable as error:
                self.engine = None
                self._unavailable_reason = str(error)
                self._capability = OcrCapability(False, self._unavailable_reason)
            else:
                self._capability = OcrCapability(True, "configured")
        else:
            self.engine = None
            self._unavailable_reason = self._unavailable_reason or "OCR is not configured."
            self._capability = OcrCapability(False, self._unavailable_reason)

    @classmethod
    def from_settings(cls, documents_settings: object) -> OcrPipeline:
        """Build an OCR pipeline from validated document settings."""
        enabled = getattr(documents_settings, "ocr_enabled", False)
        command = getattr(documents_settings, "ocr_command", None)
        if not enabled:
            return cls(unavailable_reason="OCR is disabled by configuration.")
        if not isinstance(command, str) or not command.strip():
            return cls(unavailable_reason="OCR is enabled but no OCR command is configured.")
        return cls(command=command)

    @classmethod
    def disabled(cls, reason: str = "OCR is unavailable.") -> OcrPipeline:
        """Return an explicit fail-closed pipeline for offline deployments."""
        return cls(unavailable_reason=reason)

    @property
    def capability(self) -> OcrCapability:
        """Return the configured capability without probing a process."""
        return self._capability

    def process(
        self,
        document: bytes,
        native: NativeExtractionResult,
    ) -> OcrExtractionResult:
        """OCR only native pages without extractable text.

        Expected renderer/engine failures become review outcomes per page.  A
        capability failure is represented for every selected page so no page
        can accidentally proceed to automated clause detection.
        """
        if not isinstance(document, bytes) or not document:
            raise ValidationError("OCR requires non-empty PDF bytes.", field="document")
        if not isinstance(native, NativeExtractionResult):
            raise TypeError("OCR requires a NativeExtractionResult.")
        selected = tuple(page for page in native.pages if page.needs_ocr)
        if not selected:
            return OcrExtractionResult((), self.capability)
        if self.engine is None:
            reason = self._unavailable_reason or self.capability.detail
            return OcrExtractionResult(
                tuple(
                    OcrPageResult(page.page_number, None, None, reason=reason)
                    for page in selected
                ),
                self.capability,
            )

        outcomes: list[OcrPageResult] = []
        attempted: list[int] = []
        capability = self.capability
        for page in selected:
            try:
                rendered = self.renderer.render(document, page)
                attempted.append(page.page_number)
                outcome = self.engine.recognize(
                    rendered,
                    page_number=page.page_number,
                    page_width=page.width,
                    page_height=page.height,
                )
                if not isinstance(outcome, OcrPageResult):
                    raise OcrProcessingError("The OCR engine returned an invalid page result.")
                if outcome.page_number != page.page_number:
                    raise OcrProcessingError(
                        f"The OCR engine returned the wrong page for page {page.page_number}."
                    )
                outcomes.append(self._apply_floor(outcome))
            except OcrUnavailable as error:
                capability = OcrCapability(False, str(error))
                outcomes.extend(
                    OcrPageResult(
                        remaining.page_number,
                        None,
                        None,
                        reason=str(error),
                    )
                    for remaining in selected[len(outcomes) :]
                )
                break
            except Exception as error:
                reason = _safe_reason(error, f"OCR failed on page {page.page_number}")
                outcomes.append(OcrPageResult(page.page_number, None, None, reason=reason))
        return OcrExtractionResult(tuple(outcomes), capability, tuple(sorted(attempted)))

    def extract(
        self,
        document: bytes,
        native: NativeExtractionResult,
    ) -> OcrExtractionResult:
        """Compatibility alias for callers that name the pipeline an extractor."""
        return self.process(document, native)

    def _apply_floor(self, outcome: OcrPageResult) -> OcrPageResult:
        if outcome.text is None:
            return replace(
                outcome,
                reason=outcome.reason or "OCR returned no readable text.",
            )
        if outcome.confidence is None:
            return replace(
                outcome,
                reason=outcome.reason or "OCR did not return a page confidence.",
            )
        if outcome.confidence < self.confidence_floor:
            return replace(
                outcome,
                reason=(
                    outcome.reason
                    or f"OCR confidence {outcome.confidence:.2f} is below the "
                    f"required floor {self.confidence_floor:.2f}."
                ),
            )
        return replace(outcome, reason=None)


def spans_from_text(page_number: int, text: str) -> tuple[TextSpan, ...]:
    """Create deterministic line spans for a human-corrected page."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Corrected page text must contain non-whitespace text.")
    spans: list[TextSpan] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        line_text = line.rstrip("\r\n")
        if line_text:
            spans.append(
                TextSpan(
                    page_number=page_number,
                    start_offset=offset,
                    end_offset=offset + len(line_text),
                    text=line_text,
                    span_type="manual_correction",
                )
            )
        offset += len(line)
    if not spans:
        spans.append(
            TextSpan(
                page_number=page_number,
                start_offset=0,
                end_offset=len(text),
                text=text,
                span_type="manual_correction",
            )
        )
    return tuple(spans)


def is_history_span(span_type: str | None) -> bool:
    """Return whether a stored span is page-version provenance, not active text."""
    return span_type == _SUPERSEDED_SPAN_TYPE or bool(
        isinstance(span_type, str) and span_type.startswith(_PAGE_VERSION_SPAN_PREFIX)
    )


def page_is_eligible_for_detection(*, text: str | None, needs_review: bool) -> bool:
    """Apply the fail-closed downstream clause-detection eligibility rule."""
    return isinstance(text, str) and bool(text.strip()) and needs_review is False


def page_version_span_type(version: int, role: str) -> str:
    """Return a bounded provenance type for a retained full-page version."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("Page text versions must be positive integers.")
    if role not in {"original", "corrected"}:
        raise ValueError("Page text version role must be original or corrected.")
    return f"{_PAGE_VERSION_SPAN_PREFIX}{role}:v{version}"


def _parse_tsv(
    payload: bytes,
    *,
    page_number: int,
    page_width: float,
    page_height: float,
    pixel_width: int,
    pixel_height: int,
) -> OcrPageResult:
    try:
        decoded = payload.decode("utf-8-sig", errors="strict")
        rows = csv.DictReader(io.StringIO(decoded), delimiter="\t")
        required = {
            "level",
            "block_num",
            "par_num",
            "line_num",
            "word_num",
            "left",
            "top",
            "width",
            "height",
            "conf",
            "text",
        }
        if rows.fieldnames is None or not required.issubset(set(rows.fieldnames)):
            raise ValueError("Tesseract TSV headers are incomplete")
        tokens: list[OcrToken] = []
        grouped: dict[tuple[int, int, int], list[tuple[int, OcrToken]]] = {}
        for raw in rows:
            text = _clean_ocr_text(raw.get("text"))
            if not text:
                continue
            confidence = _decimal(raw.get("conf"), "Tesseract confidence")
            if confidence < 0:
                continue
            if confidence > 100:
                raise ValueError("Tesseract confidence is outside 0..100")
            left = _integer(raw.get("left"), "Tesseract left")
            top = _integer(raw.get("top"), "Tesseract top")
            width = _integer(raw.get("width"), "Tesseract width")
            height = _integer(raw.get("height"), "Tesseract height")
            if left < 0 or top < 0 or width < 1 or height < 1:
                raise ValueError("Tesseract returned an invalid word bounding box")
            if left + width > pixel_width or top + height > pixel_height:
                raise ValueError("Tesseract returned a word outside the image bounds")
            token = OcrToken(
                text=text,
                confidence=confidence / Decimal("100"),
                left=left,
                top=top,
                width=width,
                height=height,
            )
            tokens.append(token)
            key = (
                _integer(raw.get("block_num"), "Tesseract block number"),
                _integer(raw.get("par_num"), "Tesseract paragraph number"),
                _integer(raw.get("line_num"), "Tesseract line number"),
            )
            grouped.setdefault(key, []).append(
                (_integer(raw.get("word_num"), "Tesseract word number"), token)
            )
    except (InvalidOperation, TypeError, ValueError, UnicodeDecodeError) as error:
        raise OcrProcessingError(
            f"Tesseract returned malformed TSV for page {page_number}."
        ) from error

    if not tokens:
        return OcrPageResult(
            page_number,
            None,
            Decimal("0"),
            reason="OCR returned no readable text.",
        )
    text_lines: list[str] = []
    spans: list[TextSpan] = []
    current_offset = 0
    for line_tokens in (grouped[key] for key in sorted(grouped)):
        line_tokens.sort(key=lambda item: item[0])
        line = _join_tokens(token for _, token in line_tokens)
        if not line:
            continue
        if text_lines:
            current_offset += 1
        start = current_offset
        text_lines.append(line)
        current_offset += len(line)
        spans.append(
            TextSpan(
                page_number=page_number,
                start_offset=start,
                end_offset=current_offset,
                text=line,
                bbox=_mapped_bbox(
                    line_tokens,
                    page_width=page_width,
                    page_height=page_height,
                    pixel_width=pixel_width,
                    pixel_height=pixel_height,
                ),
                span_type="ocr_line",
            )
        )
    text = "\n".join(text_lines)
    confidence = sum((token.confidence for token in tokens), Decimal("0")) / Decimal(len(tokens))
    return OcrPageResult(page_number, text, confidence, tuple(spans))


def _join_tokens(tokens: Iterable[OcrToken]) -> str:
    result = ""
    for token in tokens:
        value = token.text.strip()
        if result and not _no_space_before(value) and not _no_space_after(result[-1]):
            result += " "
        result += value
    return result


def _mapped_bbox(
    line_tokens: Sequence[tuple[int, OcrToken]],
    *,
    page_width: float,
    page_height: float,
    pixel_width: int,
    pixel_height: int,
) -> tuple[float, float, float, float]:
    left = min(token.left for _, token in line_tokens)
    top = min(token.top for _, token in line_tokens)
    right = max(token.left + token.width for _, token in line_tokens)
    bottom = max(token.top + token.height for _, token in line_tokens)
    x0 = left / pixel_width * page_width
    x1 = right / pixel_width * page_width
    y0 = page_height - bottom / pixel_height * page_height
    y1 = page_height - top / pixel_height * page_height
    return (x0, y0, x1, y1)


def _validate_spans(text: str, spans: Iterable[TextSpan]) -> None:
    expected_start = 0
    for span in spans:
        if text[span.start_offset : span.end_offset] != span.text:
            raise ValueError("OCR span text does not match OCR page offsets.")
        if span.start_offset < expected_start:
            raise ValueError("OCR spans must be in reading order.")
        expected_start = span.end_offset


def _command_parts(command: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, str):
        direct_path = Path(command.strip().strip('"'))
        if direct_path.is_file() and not direct_path.is_symlink():
            parts = (command.strip().strip('"'),)
        else:
            try:
                parts = tuple(shlex.split(command, posix=os.name != "nt"))
            except ValueError as error:
                raise OcrUnavailable("The configured OCR command is malformed.") from error
    else:
        parts = tuple(command)
    if not parts or any(not isinstance(part, str) or not part.strip() for part in parts):
        raise OcrUnavailable("The configured OCR command is empty.")
    return tuple(str(part) for part in parts)


def _resolve_executable(value: str) -> str | None:
    candidate = value.strip().strip('"')
    path = Path(candidate)
    if path.is_file() and not path.is_symlink():
        return str(path.resolve())
    return shutil.which(candidate)


def _clean_ocr_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _decimal(value: object, label: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as error:
        raise ValueError(f"{label} is not numeric") from error


def _integer(value: object, label: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"{label} is not an integer") from error
    return parsed


def _fraction(value: Decimal | str | float, label: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{label} must be a decimal fraction.") from error
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
        raise ValueError(f"{label} must be between 0 and 1.")
    return parsed


def _bounded_reason(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("OCR unavailable_reason must be non-empty when supplied.")
    return value.strip()[:_MAX_REASON_LENGTH]


def _configured_confidence_floor() -> Decimal:
    """Read T9 from the approved threshold configuration."""
    from covenant_radar.config.thresholds import get

    values = get("T9")
    value = values.get("ocr_confidence_floor")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value


def _safe_reason(value: bytes | BaseException, fallback: str) -> str:
    """Return a bounded diagnostic for process output or a caught OCR error.

    Per-page pipeline failures are deliberately converted to review outcomes.
    Those failures arrive as exceptions, while Tesseract diagnostics arrive
    as bytes; accepting both here keeps the failure handler itself from
    raising and accidentally abandoning the complete extraction.
    """

    if isinstance(value, bytes):
        reason = value.decode("utf-8", errors="replace")
    else:
        reason = str(value)
    reason = reason.strip().replace("\n", " ")
    return reason[:_MAX_REASON_LENGTH] if reason else fallback


def _no_space_before(value: str) -> bool:
    return bool(value) and value[0] in ",.;:!?%)]}"


def _no_space_after(value: str) -> bool:
    return value in "([{/$"


__all__ = [
    "OcrCapability",
    "OcrEngine",
    "OcrError",
    "OcrExtractionResult",
    "OcrPageResult",
    "OcrPipeline",
    "OcrProcessingError",
    "OcrToken",
    "OcrUnavailable",
    "PageRenderer",
    "PdfiumPageRenderer",
    "RenderedPage",
    "TesseractOcrEngine",
    "is_history_span",
    "page_is_eligible_for_detection",
    "page_version_span_type",
    "spans_from_text",
]
