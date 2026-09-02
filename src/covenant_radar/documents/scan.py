"""Validation, scanning, and quarantine coordination for document uploads."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import BinaryIO, Protocol

from covenant_radar.security.uploads import (
    UploadGuard,
    UploadPolicy,
    UploadScanFailed,
    ValidatedUpload,
    VirusScanner,
)


@dataclass(frozen=True, slots=True)
class QuarantinedUpload:
    """Non-sensitive evidence that an upload was held before persistence.

    The rejected bytes are intentionally not retained by the default sink.
    Their content hash and bounded metadata are enough to correlate the
    security event without creating a second malware-bearing file store.
    """

    filename: str
    declared_type: str
    content_hash: str | None
    size_bytes: int | None
    reason: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.filename or not self.declared_type or not self.reason:
            raise ValueError("A quarantined upload requires bounded identifying metadata.")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("A quarantined upload size cannot be negative.")
        if not isinstance(self.occurred_at, datetime):
            raise ValueError("A quarantined upload timestamp must be a datetime.")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("A quarantined upload timestamp must be timezone-aware.")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))


class QuarantineSink(Protocol):
    """Destination for the metadata of an upload rejected by the scanner."""

    def quarantine(self, upload: QuarantinedUpload) -> None:
        """Record that the upload was held and rejected."""


class InMemoryQuarantine:
    """Small deterministic sink for local operation and dependency injection.

    A production deployment can replace this with a separately controlled
    security-ticket adapter without changing the validation or service layer.
    """

    def __init__(self) -> None:
        self._uploads: list[QuarantinedUpload] = []
        self._lock = Lock()

    @property
    def uploads(self) -> tuple[QuarantinedUpload, ...]:
        """Return an immutable snapshot of recorded quarantine metadata."""
        with self._lock:
            return tuple(self._uploads)

    def quarantine(self, upload: QuarantinedUpload) -> None:
        if not isinstance(upload, QuarantinedUpload):
            raise TypeError("InMemoryQuarantine accepts QuarantinedUpload records only.")
        with self._lock:
            self._uploads.append(upload)


class DocumentScanPipeline:
    """Run every upload gate before a document store can be called."""

    def __init__(
        self,
        *,
        policy: UploadPolicy | None = None,
        scanner: VirusScanner | None = None,
        quarantine: QuarantineSink | None = None,
    ) -> None:
        self.guard = UploadGuard(policy=policy, scanner=scanner)
        self.quarantine = quarantine if quarantine is not None else InMemoryQuarantine()

    def validate(
        self,
        filename: str,
        content_type: str,
        data: bytes | bytearray | memoryview | BinaryIO,
        *,
        occurred_at: datetime,
    ) -> ValidatedUpload:
        """Validate and scan an upload, recording scan failures before raising."""
        if (
            not isinstance(occurred_at, datetime)
            or occurred_at.tzinfo is None
            or occurred_at.utcoffset() is None
        ):
            raise ValueError("Document scan timestamp must be timezone-aware.")
        try:
            return self.guard.validate(filename, content_type, data)
        except UploadScanFailed as error:
            record = QuarantinedUpload(
                filename=error.filename or filename,
                declared_type=error.declared_type or content_type,
                content_hash=_content_hash(data),
                size_bytes=_content_size(data),
                reason=str(error)[:500],
                occurred_at=occurred_at,
            )
            try:
                self.quarantine.quarantine(record)
            except Exception as quarantine_error:
                raise UploadScanFailed(
                    "Upload refused: the scan failure could not be quarantined.",
                    filename=record.filename,
                    declared_type=record.declared_type,
                    detected_type=error.detected_type,
                ) from quarantine_error
            raise


def _content_hash(data: object) -> str | None:
    if isinstance(data, bytes | bytearray | memoryview):
        return hashlib.sha256(bytes(data)).hexdigest()
    return None


def _content_size(data: object) -> int | None:
    if isinstance(data, bytes | bytearray | memoryview):
        return len(data)
    return None


# Names kept explicit at the adapter boundary for callers that use the shorter
# terminology while retaining one implementation and one scan path.
ScanPipeline = DocumentScanPipeline
Quarantine = InMemoryQuarantine


__all__ = [
    "DocumentScanPipeline",
    "InMemoryQuarantine",
    "Quarantine",
    "QuarantineSink",
    "QuarantinedUpload",
    "ScanPipeline",
]
