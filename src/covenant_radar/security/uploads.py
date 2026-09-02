"""Fail-closed validation for files before they reach a document store."""

from __future__ import annotations

import io
import logging
import posixpath
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePath
from types import MappingProxyType
from typing import BinaryIO, Protocol

_LOGGER = logging.getLogger(__name__)
_DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PNG_MIME = "image/png"
JPEG_MIME = "image/jpeg"


class UploadRejected(ValueError):
    """A file failed a validation gate and must not be stored."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 422,
        filename: str | None = None,
        declared_type: str | None = None,
        detected_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.filename = filename
        self.declared_type = declared_type
        self.detected_type = detected_type

    def __str__(self) -> str:
        return self.message


class UploadTooLarge(UploadRejected):
    """The upload exceeds the configured byte limit."""

    def __init__(
        self,
        message: str,
        *,
        filename: str | None = None,
        declared_type: str | None = None,
        detected_type: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=413,
            filename=filename,
            declared_type=declared_type,
            detected_type=detected_type,
        )


class UploadTypeMismatch(UploadRejected):
    """The filename, declared MIME type and file signature disagree."""

    def __init__(
        self,
        message: str,
        *,
        filename: str | None = None,
        declared_type: str | None = None,
        detected_type: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=415,
            filename=filename,
            declared_type=declared_type,
            detected_type=detected_type,
        )


class UploadScanFailed(UploadRejected):
    """The virus scanner is unavailable or did not clear the file."""

    def __init__(
        self,
        message: str,
        *,
        filename: str | None = None,
        declared_type: str | None = None,
        detected_type: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=422,
            filename=filename,
            declared_type=declared_type,
            detected_type=detected_type,
        )


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Normalized result returned by a virus-scanning hook."""

    clean: bool
    engine: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.engine or len(self.engine) > 128:
            raise ValueError("A scan result requires a bounded engine name.")


class VirusScanner(Protocol):
    """Hook called after validation and before the caller stores the bytes."""

    def __call__(self, content: bytes) -> ScanResult | bool:
        """Return clean status or a detailed scan result."""


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    """Allowed document formats and defensive archive limits."""

    max_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES
    max_filename_bytes: int = 255
    allowed_types: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: {
            ".pdf": frozenset({PDF_MIME}),
            ".docx": frozenset({DOCX_MIME}),
            ".xlsx": frozenset({XLSX_MIME}),
        }
    )
    max_archive_entries: int = 1_000
    max_archive_uncompressed_bytes: int = 100 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_filename_bytes <= 0:
            raise ValueError("Upload size limits must be positive.")
        if self.max_archive_entries <= 0 or self.max_archive_uncompressed_bytes <= 0:
            raise ValueError("Archive safety limits must be positive.")
        normalized: dict[str, frozenset[str]] = {}
        for extension, mime_types in self.allowed_types.items():
            extension = str(extension).lower()
            if (
                not extension.startswith(".")
                or extension.count(".") != 1
                or any(not (character.isalnum() or character in "._-") for character in extension)
            ):
                raise ValueError(f"Invalid upload extension: {extension!r}.")
            values = frozenset(_normalize_mime(mime_type) for mime_type in mime_types)
            if not values:
                raise ValueError(f"No MIME type is configured for {extension!r}.")
            normalized[extension] = values
        object.__setattr__(self, "allowed_types", MappingProxyType(normalized))

    @classmethod
    def from_settings(cls, settings: object) -> UploadPolicy:
        """Build an upload policy from document settings when those fields exist."""
        documents = getattr(settings, "documents", settings)
        configured_limit = getattr(documents, "max_upload_bytes", _DEFAULT_MAX_UPLOAD_BYTES)
        return cls(max_bytes=int(configured_limit))


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """Bytes cleared by filename, type, signature and virus-scan checks."""

    filename: str
    declared_type: str
    detected_type: str
    size_bytes: int
    content: bytes
    scan: ScanResult


class UploadGuard:
    """Perform all upload checks without writing to a storage adapter."""

    def __init__(
        self,
        *,
        policy: UploadPolicy | None = None,
        scanner: VirusScanner | None = None,
    ) -> None:
        # Imported here because the default scanner imports `ScanResult` from
        # this module; the guard stays fail-closed either way, but a caller
        # that injects nothing now gets the built-in engine rather than a
        # refusal it has no way to configure away.
        from covenant_radar.security.scanners import default_upload_scanner

        self.policy = policy or UploadPolicy()
        self.scanner: VirusScanner = scanner if scanner is not None else default_upload_scanner()

    def validate(
        self,
        filename: str,
        content_type: str,
        data: bytes | bytearray | memoryview | BinaryIO,
    ) -> ValidatedUpload:
        """Return validated bytes or raise before any store can be called."""
        safe_filename = _validate_filename(filename, self.policy)
        declared_type = _normalize_mime(content_type)
        extension = PurePath(safe_filename).suffix.lower()
        allowed_types = self.policy.allowed_types.get(extension)
        if allowed_types is None:
            raise UploadTypeMismatch(
                f"Upload extension {extension!r} is not allowed.",
                filename=safe_filename,
                declared_type=declared_type,
            )
        if declared_type not in allowed_types:
            expected = ", ".join(sorted(allowed_types))
            raise UploadTypeMismatch(
                f"Upload type mismatch: extension {extension!r} allows {expected}, "
                f"but declared type is {declared_type!r}.",
                filename=safe_filename,
                declared_type=declared_type,
            )

        content = _read_bounded(data, self.policy.max_bytes)
        detected_type = detect_content_type(
            content,
            max_archive_entries=self.policy.max_archive_entries,
            max_archive_uncompressed_bytes=self.policy.max_archive_uncompressed_bytes,
        )
        if detected_type != declared_type or detected_type not in allowed_types:
            raise UploadTypeMismatch(
                f"Upload type mismatch: declared type {declared_type!r} and detected "
                f"magic-byte type {detected_type!r} disagree.",
                filename=safe_filename,
                declared_type=declared_type,
                detected_type=detected_type,
            )
        scan = self._scan(content, safe_filename, declared_type)
        return ValidatedUpload(
            filename=safe_filename,
            declared_type=declared_type,
            detected_type=detected_type,
            size_bytes=len(content),
            content=content,
            scan=scan,
        )

    validate_file = validate

    def _scan(self, content: bytes, filename: str, content_type: str) -> ScanResult:
        try:
            result = self.scanner(content)
        except Exception as error:
            _LOGGER.exception("Virus scan hook failed for %s", filename)
            raise UploadScanFailed(
                "Upload refused: virus scanning was unavailable.",
                filename=filename,
                declared_type=content_type,
                detected_type=content_type,
            ) from error
        if isinstance(result, bool):
            normalized = ScanResult(clean=result, engine="configured-scanner")
        elif isinstance(result, ScanResult):
            normalized = result
        else:
            raise UploadScanFailed(
                "Upload refused: virus scanner returned an invalid result.",
                filename=filename,
                declared_type=content_type,
                detected_type=content_type,
            )
        if not normalized.clean:
            reason = normalized.reason or "the scanner did not clear the file"
            raise UploadScanFailed(
                f"Upload refused by virus scanner: {reason}.",
                filename=filename,
                declared_type=content_type,
                detected_type=content_type,
            )
        return normalized


def detect_content_type(
    content: bytes,
    *,
    max_archive_entries: int = 1_000,
    max_archive_uncompressed_bytes: int = 100 * 1024 * 1024,
) -> str:
    """Identify supported formats from signatures and safe archive metadata."""
    if content.startswith(b"%PDF-"):
        return PDF_MIME
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG_MIME
    if content.startswith(b"\xff\xd8\xff"):
        return JPEG_MIME
    if content.startswith(b"PK\x03\x04"):
        return _detect_ooxml_type(
            content,
            max_archive_entries=max_archive_entries,
            max_archive_uncompressed_bytes=max_archive_uncompressed_bytes,
        )
    return "application/octet-stream"


def validate_upload(
    filename: str,
    content_type: str,
    data: bytes | bytearray | memoryview | BinaryIO,
    *,
    policy: UploadPolicy | None = None,
    scanner: VirusScanner | None = None,
) -> ValidatedUpload:
    """Convenience wrapper for the standard guard."""
    return UploadGuard(policy=policy, scanner=scanner).validate(filename, content_type, data)


def _validate_filename(filename: str, policy: UploadPolicy) -> str:
    if not isinstance(filename, str) or not filename:
        raise UploadTypeMismatch("Upload filename is required.")
    if len(filename.encode("utf-8")) > policy.max_filename_bytes:
        raise UploadTypeMismatch("Upload filename exceeds the permitted length.")
    if (
        filename != filename.strip()
        or any(ord(character) < 32 for character in filename)
        or any(character in '<>:"|?*' for character in filename)
        or filename.endswith(".")
    ):
        raise UploadTypeMismatch("Upload filename contains invalid characters.")
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise UploadTypeMismatch("Upload filename must not contain a path.")
    extension = PurePath(filename).suffix.lower()
    if not extension:
        raise UploadTypeMismatch("Upload filename must have an allowed extension.")
    return filename


def _normalize_mime(content_type: str) -> str:
    if not isinstance(content_type, str):
        raise UploadTypeMismatch("Upload content type is required.")
    normalized = content_type.split(";", 1)[0].strip().lower()
    if (
        not normalized
        or "/" not in normalized
        or any(character.isspace() for character in normalized)
    ):
        raise UploadTypeMismatch("Upload content type is invalid.")
    return normalized


def _read_bounded(data: bytes | bytearray | memoryview | BinaryIO, maximum: int) -> bytes:
    if isinstance(data, bytes | bytearray | memoryview):
        content = bytes(data)
        if len(content) > maximum:
            raise UploadTooLarge(
                f"Upload exceeds the configured limit of {maximum} bytes.",
            )
        return content
    reader = getattr(data, "read", None)
    if not callable(reader):
        raise UploadRejected("Upload content is not readable.")
    try:
        content = reader(maximum + 1)
    except (OSError, TypeError, ValueError) as error:
        raise UploadRejected("Upload content could not be read.") from error
    if not isinstance(content, bytes):
        raise UploadRejected("Upload content must be binary.")
    if len(content) > maximum:
        raise UploadTooLarge(f"Upload exceeds the configured limit of {maximum} bytes.")
    return content


def _detect_ooxml_type(
    content: bytes,
    *,
    max_archive_entries: int,
    max_archive_uncompressed_bytes: int,
) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > max_archive_entries:
                return "application/zip"
            total_size = 0
            names: set[str] = set()
            for member in members:
                normalized = posixpath.normpath(member.filename)
                if (
                    normalized.startswith("../")
                    or normalized == ".."
                    or normalized.startswith("/")
                    or normalized != member.filename
                    or "\\" in member.filename
                    or member.filename in names
                ):
                    return "application/zip"
                total_size += max(0, member.file_size)
                names.add(member.filename)
            if total_size > max_archive_uncompressed_bytes or "[Content_Types].xml" not in names:
                return "application/zip"
            if "word/document.xml" in names:
                return DOCX_MIME
            if "xl/workbook.xml" in names:
                return XLSX_MIME
    except (OSError, ValueError, zipfile.BadZipFile):
        return "application/zip"
    return "application/zip"


__all__ = [
    "DOCX_MIME",
    "JPEG_MIME",
    "PDF_MIME",
    "PNG_MIME",
    "ScanResult",
    "UploadGuard",
    "UploadPolicy",
    "UploadRejected",
    "UploadScanFailed",
    "UploadTooLarge",
    "UploadTypeMismatch",
    "UploadValidationError",
    "ValidatedUpload",
    "VirusScanner",
    "VirusScanError",
    "XLSX_MIME",
    "detect_content_type",
    "validate_upload",
]


UploadValidationError = UploadRejected
VirusScanError = UploadScanFailed
