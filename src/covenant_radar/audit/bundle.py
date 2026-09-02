"""Portable evidence-bundle creation and verification (`T-069`, `C-16`).

An evidence bundle is deliberately a boring ZIP file.  It contains the
point-in-time reconstruction, a human-readable rendering, the relevant audit
chain rows, and either each referenced source document or an explicit record
of why that document could not be included.  The manifest is canonical JSON;
its ``manifest_hash`` is the SHA-256 digest of the manifest with the two hash
metadata fields removed.  That definition avoids a self-referential hash and
lets a verifier implemented outside this application reproduce the result.

The verifier has no dependency on SQLAlchemy, the database, encryption keys,
or an LLM provider.  It checks archive safety, completeness, every listed
file's digest, the manifest digest, and the audit-chain result embedded in the
bundle.  A source chain that was already broken is reported separately from
archive tampering: the bundle remains a faithful, verifiable record of the
failure and does not hide it.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import os
import re
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Final, Protocol, cast
from uuid import UUID

from covenant_radar.audit.chain import AuditChainBreak, AuditChainRow, verify_chain
from covenant_radar.audit.reconstruct import WarningReconstruction, json_safe
from covenant_radar.core.clock import SystemClock
from covenant_radar.core.errors import ExternalServiceError, NotFound
from covenant_radar.core.ids import new_id
from covenant_radar.ports.document_store import DocumentStore

_LOGGER = logging.getLogger(__name__)
_SHA256_LENGTH: Final[int] = hashlib.sha256().digest_size * 2
_ZIP_TIMESTAMP: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)
_MAX_ARCHIVE_FILES: Final[int] = 10_000
_MAX_MANIFEST_BYTES: Final[int] = 4 * 1024 * 1024
_MAX_ARCHIVE_UNCOMPRESSED_BYTES: Final[int] = 4 * 1024 * 1024 * 1024
_MAX_PATH_LENGTH: Final[int] = 500
_MAX_FILENAME_LENGTH: Final[int] = 160
_MAX_PDF_TEXT_BYTES: Final[int] = 8 * 1024 * 1024
_BUNDLE_SCHEMA_VERSION: Final[int] = 1
_BUNDLE_FILENAME: Final[str] = "evidence-bundle.zip"
_DOCUMENT_PATH_PREFIX: Final[str] = "documents/"
_MANIFEST_PATH: Final[str] = "manifest.json"
_AUDIT_PATH: Final[str] = "audit_chain.json"
_RECONSTRUCTION_PATH: Final[str] = "reconstruction.json"
_PDF_PATH: Final[str] = "reconstruction.pdf"
_REQUIRED_PAYLOAD_PATHS: Final[frozenset[str]] = frozenset(
    {_RECONSTRUCTION_PATH, _PDF_PATH, _AUDIT_PATH}
)
_MANIFEST_HASH_FIELDS: Final[frozenset[str]] = frozenset({"manifest_hash", "manifest_file"})
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class EvidenceBundleError(ExternalServiceError):
    """The bundle could not be safely assembled or persisted."""

    def __init__(self, message: str) -> None:
        super().__init__(message, field="evidence_bundle")


@dataclass(frozen=True, slots=True)
class BundleDocument:
    """A referenced source document and the storage location to read."""

    document_id: UUID | str
    filename: str
    storage_key: str | None
    content_hash: str | None = None
    status: str = "present"
    reason: str | None = None
    byte_size: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, UUID | str) or not str(self.document_id).strip():
            raise TypeError("BundleDocument.document_id must be a non-empty UUID or string.")
        if not isinstance(self.filename, str) or not self.filename.strip():
            raise ValueError("BundleDocument.filename must be non-empty text.")
        if len(self.filename) > 500:
            raise ValueError("BundleDocument.filename must be at most 500 characters.")
        if self.storage_key is not None and (
            not isinstance(self.storage_key, str) or not self.storage_key.strip()
        ):
            raise ValueError("BundleDocument.storage_key must be non-empty text or None.")
        if self.status not in {"present", "purged", "absent"}:
            raise ValueError("BundleDocument.status must be present, purged, or absent.")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("BundleDocument.reason must be text or None.")
        if self.byte_size is not None and (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
        ):
            raise ValueError("BundleDocument.byte_size must be a non-negative integer or None.")
        if self.content_hash is not None and (
            not isinstance(self.content_hash, str) or not self.content_hash.strip()
        ):
            raise ValueError("BundleDocument.content_hash must be non-empty text or None.")
        if self.content_hash is not None and len(self.content_hash) > 128:
            raise ValueError("BundleDocument.content_hash must be at most 128 characters.")


class BundlePdfRenderer(Protocol):
    """Rendering seam for the human-readable reconstruction PDF."""

    def render(
        self,
        reconstruction: WarningReconstruction,
        chain_verification: Mapping[str, object],
    ) -> bytes:
        """Render the reconstruction and chain status into a PDF."""
        ...


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """The complete, downloadable bundle produced by :func:`build_bundle`."""

    content: bytes
    bundle_id: UUID | str
    manifest_hash: str
    filename: str = _BUNDLE_FILENAME
    content_type: str = "application/zip"

    @property
    def data(self) -> bytes:
        """Compatibility-facing name for callers that use ``data``."""

        return self.content


@dataclass(frozen=True, slots=True)
class BundleVerification:
    """Result of verifying an evidence bundle outside the product."""

    archive_valid: bool
    manifest_hash: str | None
    checked_files: int
    failures: tuple[str, ...] = ()
    chain_verified: bool | None = None
    chain_failure: str | None = None
    manifest: Mapping[str, object] | None = None

    @property
    def ok(self) -> bool:
        """Whether the archive and its manifest contents are intact."""

        return self.archive_valid

    @property
    def valid(self) -> bool:
        """Alias for :attr:`ok` used by CLI and integration callers."""

        return self.archive_valid

    @property
    def verified(self) -> bool:
        """Alias for :attr:`ok`; source-chain failure is reported separately."""

        return self.archive_valid

    @property
    def message(self) -> str:
        """Stable human-readable verification summary."""

        if not self.archive_valid:
            return "Evidence bundle verification failed: " + "; ".join(self.failures)
        if self.chain_verified is False:
            suffix = self.chain_failure or "the audit chain is broken"
            return f"Evidence bundle verified; audit chain verification failed: {suffix}."
        return f"Evidence bundle verified: {self.checked_files} files and manifest hash match."

    @property
    def errors(self) -> tuple[str, ...]:
        """Alias for the immutable failure list."""

        return self.failures


def build_bundle(
    reconstruction: WarningReconstruction,
    *,
    documents: Sequence[BundleDocument] = (),
    document_store: DocumentStore | None = None,
    audit_rows: Sequence[object] = (),
    chain_verification: Mapping[str, object] | None = None,
    bundle_id: UUID | str | None = None,
    generated_at: datetime | None = None,
    pdf_renderer: BundlePdfRenderer | None = None,
) -> EvidenceBundle:
    """Build one self-contained evidence bundle.

    ``documents`` is explicit rather than inferred from a database model, so
    this function is also the application-independent core used by tests and
    offline tooling.  A present document is streamed into the ZIP and its
    plaintext hash is checked against both the document reference and the
    content-addressed path.  ``NotFound`` is the one expected storage gap and
    becomes an explicit, hashed ``.missing.json`` record; all other storage
    failures stop the export because silently packaging a partial document
    would make the evidence misleading.
    """

    if not isinstance(reconstruction, WarningReconstruction):
        raise TypeError("build_bundle requires a WarningReconstruction.")
    if not isinstance(documents, Sequence):
        raise TypeError("build_bundle documents must be a sequence.")
    if not isinstance(audit_rows, Sequence):
        raise TypeError("build_bundle audit_rows must be a sequence.")
    resolved_bundle_id = bundle_id or new_id()
    _validate_identifier(resolved_bundle_id, "bundle_id")
    instant = _utc_instant(generated_at if generated_at is not None else SystemClock().now())

    reconstruction_payload = json_safe(reconstruction.as_dict())
    reconstruction_bytes = _canonical_json_bytes(reconstruction_payload)
    audit_payload, resolved_chain = _audit_payload(audit_rows, chain_verification)
    audit_bytes = _canonical_json_bytes(audit_payload)

    renderer = pdf_renderer or ReconstructionPdfRenderer()
    render = getattr(renderer, "render", None) or getattr(renderer, "render_pdf", None)
    if not callable(render):
        raise TypeError("pdf_renderer must provide a callable render/render_pdf method.")
    pdf_bytes = render(reconstruction, resolved_chain)
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        raise EvidenceBundleError(
            "The reconstruction PDF renderer returned empty or non-binary data."
        )
    if len(pdf_bytes) > _MAX_PDF_TEXT_BYTES:
        raise EvidenceBundleError("The reconstruction PDF exceeds the configured size limit.")

    output = io.BytesIO()
    entries: list[dict[str, object]] = []
    missing_documents: list[dict[str, object]] = []
    with zipfile.ZipFile(
        output, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as archive:
        _write_bytes_entry(archive, _RECONSTRUCTION_PATH, reconstruction_bytes, entries)
        _write_bytes_entry(archive, _PDF_PATH, pdf_bytes, entries)
        _write_bytes_entry(archive, _AUDIT_PATH, audit_bytes, entries)

        seen_document_ids: set[str] = set()
        for document in sorted(documents, key=lambda item: str(item.document_id)):
            if not isinstance(document, BundleDocument):
                raise TypeError("build_bundle documents must contain BundleDocument values.")
            document_id = str(document.document_id)
            if document_id in seen_document_ids:
                raise EvidenceBundleError(
                    f"Referenced document {document_id} occurs more than once."
                )
            seen_document_ids.add(document_id)
            result = _write_document_entry(archive, document, document_store, entries)
            if result is not None:
                missing_documents.append(result)

        entries.sort(key=lambda item: str(item["path"]))
        manifest_base: dict[str, object] = {
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            "bundle_id": str(resolved_bundle_id),
            "forecast_id": str(reconstruction.forecast_id),
            "created_at": instant.isoformat(),
            "hash_algorithm": "sha256",
            "files": entries,
            "missing_documents": missing_documents,
            "chain_verification": resolved_chain,
        }
        manifest_hash = _sha256(_canonical_json_bytes(manifest_base))
        manifest = {
            **manifest_base,
            "manifest_hash": manifest_hash,
            "manifest_file": {
                "path": _MANIFEST_PATH,
                "sha256": manifest_hash,
                "hash_scope": "canonical_manifest_without_hash_metadata",
            },
        }
        manifest_bytes = _canonical_json_bytes(manifest)
        _write_zip_bytes(archive, _MANIFEST_PATH, manifest_bytes)

    return EvidenceBundle(
        content=output.getvalue(),
        bundle_id=resolved_bundle_id,
        manifest_hash=manifest_hash,
    )


def verify_bundle(
    bundle: bytes | bytearray | memoryview | BinaryIO | Path | str,
) -> BundleVerification:
    """Verify an evidence bundle without application services or secrets."""

    try:
        raw = _read_bundle_input(bundle)
    except (OSError, TypeError, ValueError) as error:
        return _failed_verification(f"bundle input is unreadable ({error})")

    try:
        with zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive:
            return _verify_archive(archive)
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zlib.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        return _failed_verification(f"archive is unreadable ({error})")


def verify_evidence_bundle(
    bundle: bytes | bytearray | memoryview | BinaryIO | Path | str,
) -> BundleVerification:
    """Descriptive alias for :func:`verify_bundle`."""

    return verify_bundle(bundle)


class EvidenceBundleVerifier:
    """Stateless object form of the external verifier."""

    @staticmethod
    def verify(
        bundle: bytes | bytearray | memoryview | BinaryIO | Path | str,
    ) -> BundleVerification:
        return verify_bundle(bundle)


class ReconstructionPdfRenderer:
    """Render a readable reconstruction PDF with a dependency-safe fallback.

    WeasyPrint is used when its native libraries are available, matching the
    application's memo export adapter.  A text-only PDF fallback is retained
    for offline support/verification hosts where those optional native
    libraries are not installed; it still contains every reconstruction field
    and makes chain failures prominent instead of refusing an audit export.
    """

    def render(
        self,
        reconstruction: WarningReconstruction,
        chain_verification: Mapping[str, object],
    ) -> bytes:
        if not isinstance(reconstruction, WarningReconstruction):
            raise TypeError("ReconstructionPdfRenderer requires a WarningReconstruction.")
        safe_chain_value = json_safe(dict(chain_verification))
        if not isinstance(safe_chain_value, Mapping):
            raise EvidenceBundleError("PDF chain verification is not a JSON object.")
        safe_chain = safe_chain_value
        safe_reconstruction = json_safe(reconstruction.as_dict())
        title = "EVIDENCE BUNDLE — WARNING RECONSTRUCTION"
        status = "AUDIT CHAIN VERIFIED"
        if safe_chain.get("verified") is False:
            status = "AUDIT CHAIN VERIFICATION FAILED"
        body = json.dumps(
            safe_reconstruction,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        chain_text = json.dumps(safe_chain, ensure_ascii=False, sort_keys=True, indent=2)
        html_document = _pdf_html(title, status, chain_text, body)
        rendered = _try_weasyprint(html_document)
        return (
            rendered if rendered is not None else _plain_text_pdf((title, status, chain_text, body))
        )

    render_pdf = render


def _write_document_entry(
    archive: zipfile.ZipFile,
    document: BundleDocument,
    document_store: DocumentStore | None,
    entries: list[dict[str, object]],
) -> dict[str, object] | None:
    if document.status != "present":
        reason = document.reason or f"Document is recorded as {document.status}."
        return _write_missing_document(archive, document, reason, entries)
    if not document.storage_key:
        return _write_missing_document(
            archive,
            document,
            document.reason or "Document has no storage key.",
            entries,
        )
    if document_store is None:
        return _write_missing_document(
            archive,
            document,
            document.reason or "Document storage is not configured for this export.",
            entries,
        )

    try:
        stream = _document_stream(document_store, document.storage_key)
    except NotFound:
        return _write_missing_document(
            archive,
            document,
            document.reason or "Referenced document is missing from storage.",
            entries,
        )
    path = _document_path(document)
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(_zip_info(path), mode="w") as target:
            for chunk in stream:
                if not isinstance(chunk, bytes) or not chunk:
                    raise EvidenceBundleError(
                        f"Document {document.document_id} storage returned a non-binary "
                        "or empty chunk."
                    )
                size += len(chunk)
                if document.byte_size is not None and size > document.byte_size:
                    raise EvidenceBundleError(
                        f"Document {document.document_id} exceeded its recorded byte size."
                    )
                digest.update(chunk)
                target.write(chunk)
    except NotFound:
        # A backend can discover a race (purge between stream creation and its
        # first read).  This is still an honest absence, but the partially
        # opened ZIP entry cannot be removed, so fail rather than ship a
        # misleading archive.
        raise EvidenceBundleError(
            f"Referenced document {document.document_id} disappeared while being exported."
        ) from None
    actual_hash = digest.hexdigest()
    if document.byte_size is not None and size != document.byte_size:
        raise EvidenceBundleError(
            f"Document {document.document_id} size changed during export "
            f"(expected {document.byte_size}, got {size})."
        )
    if _is_digest(document.content_hash or "") and actual_hash != document.content_hash:
        raise EvidenceBundleError(
            f"Document {document.document_id} content hash mismatch during export."
        )
    entries.append(
        {
            "path": path,
            "status": "present",
            "sha256": actual_hash,
            "size": size,
            "document_id": str(document.document_id),
            "filename": document.filename,
            "content_hash": document.content_hash or actual_hash,
        }
    )
    return None


def _write_missing_document(
    archive: zipfile.ZipFile,
    document: BundleDocument,
    reason: str,
    entries: list[dict[str, object]],
) -> dict[str, object]:
    safe_reason = _bounded_text(reason, "document absence reason", 2_000)
    payload = {
        "status": "absent",
        "document_id": str(document.document_id),
        "filename": document.filename,
        "content_hash": document.content_hash,
        "storage_key": document.storage_key,
        "reason": safe_reason,
    }
    path = f"{_DOCUMENT_PATH_PREFIX}{_safe_document_id(document.document_id)}.missing.json"
    content = _canonical_json_bytes(payload)
    digest = _sha256(content)
    _write_zip_bytes(archive, path, content)
    entries.append(
        {
            "path": path,
            "status": "absent",
            "sha256": digest,
            "size": len(content),
            "document_id": str(document.document_id),
            "filename": document.filename,
            "reason": safe_reason,
        }
    )
    return {
        "document_id": str(document.document_id),
        "filename": document.filename,
        "status": "absent",
        "reason": safe_reason,
        "record_path": path,
    }


def _write_bytes_entry(
    archive: zipfile.ZipFile,
    path: str,
    content: bytes,
    entries: list[dict[str, object]],
) -> None:
    _write_zip_bytes(archive, path, content)
    entries.append(
        {"path": path, "status": "present", "sha256": _sha256(content), "size": len(content)}
    )


def _write_zip_bytes(archive: zipfile.ZipFile, path: str, content: bytes) -> None:
    _validate_archive_path(path)
    archive.writestr(_zip_info(path), content)


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    info.external_attr = 0o600 << 16
    return info


def _audit_payload(
    rows: Sequence[object],
    supplied_verification: Mapping[str, object] | None,
) -> tuple[dict[str, object], dict[str, object]]:
    row_payload = [_audit_row(item) for item in rows]
    if supplied_verification is None:
        report = verify_chain(tuple(_audit_row_object(item) for item in rows)) if rows else None
        resolved = _chain_verification(report)
    else:
        resolved = _normalise_chain_verification(supplied_verification)
    return (
        {
            "schema_version": 1,
            "rows": row_payload,
            "range": resolved.get("range"),
            "verification": resolved,
            "verification_result": resolved,
        },
        resolved,
    )


def _audit_row(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = {
            name: getattr(value, name, None)
            for name in (
                "id",
                "sequence",
                "occurred_at",
                "actor_id",
                "actor_label",
                "event_type",
                "subject_type",
                "subject_id",
                "payload",
                "threshold_snapshot_id",
                "prev_hash",
                "hash",
            )
        }
    required = (
        "sequence",
        "occurred_at",
        "event_type",
        "subject_type",
        "subject_id",
        "payload",
        "hash",
    )
    missing = [field for field in required if raw.get(field) is None]
    if missing:
        raise EvidenceBundleError(f"Audit row is missing required field {missing[0]!r}.")
    return {
        key: json_safe(value)
        for key, value in raw.items()
        if value is not None
        or key in {"actor_id", "actor_label", "threshold_snapshot_id", "prev_hash"}
    }


@dataclass(frozen=True, slots=True)
class _MappingAuditRow:
    sequence: int
    occurred_at: datetime
    actor_id: UUID | None
    actor_label: str | None
    event_type: str
    subject_type: str
    subject_id: UUID
    payload: Mapping[str, object]
    prev_hash: str | None
    hash: str


def _audit_row_object(value: object) -> AuditChainRow:
    if not isinstance(value, Mapping):
        return cast(AuditChainRow, value)
    try:
        subject_id = UUID(str(value["subject_id"]))
        actor_value = value.get("actor_id")
        actor_id = UUID(str(actor_value)) if actor_value else None
        occurred_at = datetime.fromisoformat(str(value["occurred_at"]))
        return cast(
            AuditChainRow,
            _MappingAuditRow(
                sequence=int(value["sequence"]),
                occurred_at=occurred_at,
                actor_id=actor_id,
                actor_label=value.get("actor_label")
                if isinstance(value.get("actor_label"), str)
                else None,
                event_type=str(value["event_type"]),
                subject_type=str(value["subject_type"]),
                subject_id=subject_id,
                payload=value["payload"] if isinstance(value["payload"], Mapping) else {},
                prev_hash=value.get("prev_hash")
                if isinstance(value.get("prev_hash"), str)
                else None,
                hash=str(value["hash"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EvidenceBundleError(
            f"Audit row cannot be decoded for verification ({error})."
        ) from error


def _chain_verification(report: AuditChainBreak | None) -> dict[str, object]:
    if report is None:
        return {
            "verified": True,
            "status": "verified",
            "failure": None,
            "message": "Audit chain verified.",
        }
    failure = {
        "sequence": report.sequence,
        "previous_sequence": report.previous_sequence,
        "reason": report.reason,
        "expected_prev_hash": report.expected_prev_hash,
        "actual_prev_hash": report.actual_prev_hash,
        "expected_hash": report.expected_hash,
        "actual_hash": report.actual_hash,
        "message": report.message,
    }
    return {
        "verified": False,
        "status": "failed",
        "failure": failure,
        "message": report.message,
    }


def _normalise_chain_verification(value: Mapping[str, object]) -> dict[str, object]:
    verified = value.get("verified")
    if not isinstance(verified, bool):
        raise ValueError("chain_verification.verified must be a boolean.")
    status = value.get("status", "verified" if verified else "failed")
    if not isinstance(status, str) or status not in {"verified", "failed"}:
        raise ValueError("chain_verification.status must be verified or failed.")
    result = {
        "verified": verified,
        "status": status,
        "failure": json_safe(value.get("failure")),
        "message": str(
            value.get("message")
            or ("Audit chain verified." if verified else "Audit chain verification failed.")
        ),
    }
    if "range" in value:
        result["range"] = json_safe(value.get("range"))
    return result


def _verify_archive(archive: zipfile.ZipFile) -> BundleVerification:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    failures: list[str] = []
    if len(names) > _MAX_ARCHIVE_FILES:
        failures.append(f"archive contains more than {_MAX_ARCHIVE_FILES} files")
    if len(names) != len(set(names)):
        failures.append("archive contains duplicate file names")
    for name in names:
        try:
            _validate_archive_path(name)
        except ValueError as error:
            failures.append(str(error))
    total_uncompressed = sum(max(info.file_size, 0) for info in infos)
    if total_uncompressed > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        failures.append("archive exceeds the maximum uncompressed size")
    if _MANIFEST_PATH not in names:
        failures.append("manifest.json is missing")
        return _failed_verification(*failures)
    try:
        manifest_bytes = archive.read(_MANIFEST_PATH)
        if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
            failures.append("manifest.json exceeds the maximum size")
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError("manifest root is not an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        failures.append(f"manifest.json is invalid ({error})")
        return _failed_verification(*failures)

    manifest_hash = manifest.get("manifest_hash")
    if not isinstance(manifest_hash, str) or not _is_digest(manifest_hash):
        failures.append("manifest.json has no valid manifest_hash")
        manifest_hash_value: str | None = None
    else:
        manifest_hash_value = manifest_hash
        unsigned = {
            key: value for key, value in manifest.items() if key not in _MANIFEST_HASH_FIELDS
        }
        if _sha256(_canonical_json_bytes(unsigned)) != manifest_hash:
            failures.append("manifest.json manifest_hash does not match its contents")
        manifest_file = manifest.get("manifest_file")
        if not isinstance(manifest_file, Mapping) or manifest_file.get("path") != _MANIFEST_PATH:
            failures.append("manifest.json manifest_file metadata is invalid")
        elif manifest_file.get("sha256") != manifest_hash:
            failures.append("manifest.json manifest_file hash does not match manifest_hash")

    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        failures.append("manifest.json files must be a list")
        entries: list[Mapping[str, object]] = []
    else:
        entries = [entry for entry in raw_entries if isinstance(entry, Mapping)]
        if len(entries) != len(raw_entries):
            failures.append("manifest.json files contains a non-object entry")

    listed_names: set[str] = set()
    checked_files = 0
    for entry in entries:
        path = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(path, str):
            failures.append("manifest.json contains a file entry without a path")
            continue
        try:
            _validate_archive_path(path)
        except ValueError as error:
            failures.append(str(error))
            continue
        if path in listed_names:
            failures.append(f"manifest.json lists {path!r} more than once")
            continue
        listed_names.add(path)
        if path == _MANIFEST_PATH:
            failures.append("manifest.json must be represented by manifest_file, not files")
            continue
        if path not in names:
            failures.append(f"manifest-listed file {path!r} is missing from the archive")
            continue
        if not isinstance(digest, str) or not _is_digest(digest):
            failures.append(f"manifest-listed file {path!r} has an invalid SHA-256 digest")
            continue
        actual_digest, actual_size = _hash_zip_entry(archive, path)
        checked_files += 1
        if actual_digest != digest:
            failures.append(f"file {path!r} hash mismatch")
        expected_size = entry.get("size")
        if isinstance(expected_size, int) and actual_size != expected_size:
            failures.append(f"file {path!r} size mismatch")

    actual_payload_names = set(names).difference({_MANIFEST_PATH})
    for required_path in sorted(_REQUIRED_PAYLOAD_PATHS.difference(actual_payload_names)):
        failures.append(f"required archive file {required_path!r} is missing")
    unlisted = sorted(actual_payload_names.difference(listed_names))
    failures.extend(f"archive file {path!r} is not listed in manifest.json" for path in unlisted)

    chain_verified, chain_failure = _verify_embedded_chain(archive, manifest, failures)
    if failures:
        return BundleVerification(
            archive_valid=False,
            manifest_hash=manifest_hash_value,
            checked_files=checked_files,
            failures=tuple(dict.fromkeys(failures)),
            chain_verified=chain_verified,
            chain_failure=chain_failure,
            manifest=manifest,
        )
    return BundleVerification(
        archive_valid=True,
        manifest_hash=manifest_hash_value,
        checked_files=checked_files,
        chain_verified=chain_verified,
        chain_failure=chain_failure,
        manifest=manifest,
    )


def _verify_embedded_chain(
    archive: zipfile.ZipFile,
    manifest: Mapping[str, object],
    failures: list[str],
) -> tuple[bool | None, str | None]:
    if _AUDIT_PATH not in archive.namelist():
        failures.append("audit_chain.json is missing")
        return None, None
    try:
        payload = json.loads(archive.read(_AUDIT_PATH))
        if not isinstance(payload, Mapping):
            raise ValueError("audit_chain root is not an object")
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ValueError("audit_chain rows is not a list")
        decoded_rows = tuple(_audit_row_object(row) for row in rows)
        range_value = payload.get("range")
        from_sequence: int | None = None
        to_sequence: int | None = None
        if isinstance(range_value, Mapping):
            raw_from = range_value.get("from")
            raw_to = range_value.get("to")
            if raw_from is not None:
                from_sequence = int(raw_from)
            if raw_to is not None:
                to_sequence = int(raw_to)
        report = (
            verify_chain(decoded_rows, from_sequence=from_sequence, to_sequence=to_sequence)
            if decoded_rows
            else None
        )
        actual = _chain_verification(report)
        stored = payload.get("verification")
        stored_result = payload.get("verification_result")
        if (
            not isinstance(stored, Mapping)
            or not isinstance(stored_result, Mapping)
            or stored.get("verified") != actual["verified"]
            or stored_result.get("verified") != actual["verified"]
        ):
            failures.append("audit_chain.json verification result does not match its rows")
        manifest_chain = manifest.get("chain_verification")
        if (
            not isinstance(manifest_chain, Mapping)
            or manifest_chain.get("verified") != actual["verified"]
        ):
            failures.append("manifest.json chain verification does not match audit_chain.json")
        if report is None:
            return True, None
        return False, report.message
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        EvidenceBundleError,
        ValueError,
    ) as error:
        failures.append(f"audit_chain.json is invalid ({error})")
        return None, None


def _hash_zip_entry(archive: zipfile.ZipFile, path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(path, mode="r") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if chunk == b"":
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _document_stream(document_store: DocumentStore, storage_key: str) -> Iterable[bytes]:
    stream = getattr(document_store, "stream", None)
    if callable(stream):
        return cast(Iterable[bytes], stream(storage_key))
    get = getattr(document_store, "get", None)
    if callable(get):
        content = get(storage_key)
        if not isinstance(content, bytes):
            raise EvidenceBundleError("Document storage get returned non-binary data.")
        return (content,)
    raise EvidenceBundleError("Document storage provides neither stream nor get.")


def _document_path(document: BundleDocument) -> str:
    identifier = _safe_document_id(document.document_id)
    filename = Path(document.filename).name
    safe_name = _SAFE_FILENAME.sub("_", filename).strip("._") or "document.bin"
    safe_name = safe_name[:_MAX_FILENAME_LENGTH]
    return f"{_DOCUMENT_PATH_PREFIX}{identifier}-{safe_name}"


def _safe_document_id(value: UUID | str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "_", str(value))[:100] or "unknown"


def _validate_archive_path(path: str) -> None:
    if not isinstance(path, str) or not path or len(path) > _MAX_PATH_LENGTH:
        raise ValueError(f"archive path {path!r} is empty or too long")
    if "\\" in path or ":" in path or path.startswith("/") or "\x00" in path:
        raise ValueError(f"archive path {path!r} is unsafe")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError(f"archive path {path!r} is unsafe")


def _validate_identifier(value: UUID | str, field: str) -> None:
    if not isinstance(value, UUID | str) or not str(value).strip():
        raise TypeError(f"{field} must be a non-empty UUID or string.")


def _validate_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _is_digest(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 hexadecimal digest.")
    return value


def _is_digest(value: str) -> bool:
    return (
        len(value) == _SHA256_LENGTH
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text.")
    cleaned = unicodedata.normalize("NFC", value.strip())
    if len(cleaned) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters.")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in cleaned):
        raise ValueError(f"{field} contains a control character.")
    return cleaned


def _utc_instant(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Bundle timestamp must be timezone-aware.")
    return value.astimezone(UTC)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_bundle_input(value: bytes | bytearray | memoryview | BinaryIO | Path | str) -> bytes:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, Path | str):
        return Path(value).read_bytes()
    read = getattr(value, "read", None)
    if not callable(read):
        raise TypeError("Bundle input must be bytes, a path, or a binary stream.")
    content = read()
    if not isinstance(content, bytes):
        raise TypeError("Bundle input stream must return bytes.")
    return content


def _failed_verification(*failures: str) -> BundleVerification:
    return BundleVerification(
        archive_valid=False,
        manifest_hash=None,
        checked_files=0,
        failures=tuple(failures) or ("unknown verification failure",),
    )


def _pdf_html(title: str, status: str, chain: str, reconstruction: str) -> str:
    status_class = "failure" if "FAILED" in status else "success"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 18mm; }}
body {{ font-family: sans-serif; color: #17202a; font-size: 10pt; }}
h1 {{ font-size: 18pt; margin: 0 0 12pt; }}
h2 {{ font-size: 13pt; margin: 18pt 0 6pt; }}
.status {{ padding: 10pt; border: 2pt solid #17202a; font-weight: bold; }}
.success {{ background: #e7f4e8; }} .failure {{ background: #ffdede; color: #8b0000; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; font-family: monospace; font-size: 7.5pt; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="status {status_class}">{html.escape(status)}</div>
<h2>Audit chain verification</h2><pre>{html.escape(chain)}</pre>
<h2>Point-in-time reconstruction</h2><pre>{html.escape(reconstruction)}</pre>
</body></html>"""


def _try_weasyprint(document: str) -> bytes | None:
    handles: list[Any] = []
    try:
        if os.name == "nt":
            for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
                directory = Path(raw_directory)
                if directory.is_dir() and hasattr(os, "add_dll_directory"):
                    try:
                        handles.append(os.add_dll_directory(str(directory)))
                    except OSError:
                        continue
        from weasyprint import HTML  # type: ignore[import-untyped]

        rendered = HTML(string=document).write_pdf()
        return rendered if isinstance(rendered, bytes) and rendered else None
    except (ImportError, OSError, RuntimeError):
        return None
    finally:
        for handle in handles:
            handle.close()


def _plain_text_pdf(sections: Sequence[str]) -> bytes:
    """Create a small valid Type-1 PDF for offline/native-library-free hosts."""

    lines: list[str] = []
    for section in sections:
        for raw_line in section.splitlines() or [""]:
            plain = (
                unicodedata.normalize("NFKD", raw_line).encode("ascii", "replace").decode("ascii")
            )
            if not plain:
                lines.append("")
                continue
            while len(plain) > 105:
                lines.append(plain[:105])
                plain = plain[105:]
            lines.append(plain)
        lines.append("")
    pages = [lines[index : index + 52] for index in range(0, len(lines), 52)] or [[]]
    objects: list[bytes] = [
        b"",  # object zero is reserved by the PDF format
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the Pages object is filled after page ids are known
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    font_id = 3
    page_object_ids: list[int] = []
    for _ in pages:
        page_object_ids.append(len(objects))
        objects.extend([b"", b""])  # page object followed by its content stream
    objects[2] = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{page_id} 0 R".encode("ascii") for page_id in page_object_ids)
        + b"] /Count "
        + str(len(pages)).encode("ascii")
        + b" >>"
    )
    for page_index, page_lines in enumerate(pages):
        stream_lines = ["BT", "/F1 9 Tf", "72 760 Td", "11 TL"]
        for line_index, line in enumerate(page_lines):
            if line_index:
                stream_lines.append("T*")
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream_lines.append(f"({escaped}) Tj")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", errors="replace")
        page_id = page_object_ids[page_index]
        content_id = page_id + 1
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )

    output = io.BytesIO(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, content in enumerate(objects[1:], start=1):
        offsets.append(output.tell())
        output.write(f"{object_id} 0 obj\n".encode("ascii"))
        output.write(content)
        output.write(b"\nendobj\n")
    xref_offset = output.tell()
    output.write(f"xref\n0 {len(objects)}\n".encode("ascii"))
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = f"trailer\n<< /Size {len(objects)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    output.write(trailer.encode("ascii"))
    return output.getvalue()


# Compatibility-facing builder object for callers that prefer an object
# operation over the functional entry point.
class EvidenceBundleBuilder:
    """Build and verify bundles without retaining application state."""

    @staticmethod
    def build(
        reconstruction: WarningReconstruction,
        **kwargs: object,
    ) -> EvidenceBundle:
        return build_bundle(reconstruction, **kwargs)  # type: ignore[arg-type]

    @staticmethod
    def verify(
        bundle: bytes | bytearray | memoryview | BinaryIO | Path | str,
    ) -> BundleVerification:
        return verify_bundle(bundle)


build_evidence_bundle = build_bundle
verify_bundle_file = verify_bundle
EvidenceBundleVerification = BundleVerification


__all__ = [
    "BundleDocument",
    "BundlePdfRenderer",
    "BundleVerification",
    "EvidenceBundle",
    "EvidenceBundleBuilder",
    "EvidenceBundleError",
    "EvidenceBundleVerification",
    "EvidenceBundleVerifier",
    "ReconstructionPdfRenderer",
    "build_bundle",
    "build_evidence_bundle",
    "verify_bundle",
    "verify_bundle_file",
    "verify_evidence_bundle",
]
