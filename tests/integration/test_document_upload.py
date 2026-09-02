"""Integration tests for the T-084 document upload vertical slice."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import ExternalServiceError
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope
from covenant_radar.documents.scan import InMemoryQuarantine
from covenant_radar.documents.store import FileSystemDocumentStore, StorageUnavailable
from covenant_radar.security.crypto import FieldEncryptor
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.security.uploads import (
    DOCX_MIME,
    PDF_MIME,
    ScanResult,
    UploadPolicy,
    UploadScanFailed,
    UploadTooLarge,
    UploadTypeMismatch,
)
from covenant_radar.services.documents import DocumentService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del actor, request_id
        self.events.append((event_type, subject, payload))
        return object()


class _Fixture:
    def __init__(
        self,
        tmp_path: Path,
        *,
        scanner: object | None = None,
        upload_policy: UploadPolicy | None = None,
        quarantine: InMemoryQuarantine | None = None,
        store: object | None = None,
    ) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.user = AppUser(
            id=uuid4(),
            username="document-uploader",
            email="document-uploader@example.com",
            full_name="Document Uploader",
            auth_source="local",
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-document-test-0001",
        )
        self.portfolio = Portfolio.create(
            code="DOCS",
            name="Documents",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-document-test-0002",
        )
        self.borrower = Borrower(
            id=uuid4(),
            reference="B-DOC-001",
            legal_name="Document Test Borrower",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-document-test-0003",
        )
        self.session.add_all([self.user, self.portfolio, self.borrower])
        self.session.flush()
        self.principal = Principal.user(
            self.user.id,
            (Permission.UPLOAD_DOCUMENT, Permission.VIEW_DOCUMENT),
        )
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.store = store or FileSystemDocumentStore(
            tmp_path / "documents",
            encryptor=FieldEncryptor({"documents-test": b"D" * 32}, "documents-test"),
        )
        self.quarantine = quarantine
        self.service = DocumentService(
            self.session,
            store=self.store,  # type: ignore[arg-type]
            audit=self.audit,
            clock=FixedClock(_NOW),
            scanner=scanner or (lambda _content: ScanResult(clean=True, engine="test-scanner")),
            upload_policy=upload_policy,
            quarantine=quarantine,
            request_id="rq-document-test-0004",
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def upload(
        self,
        *,
        filename: str = "sanction.pdf",
        content: bytes = b"%PDF-1.7\nvalid",
    ) -> Document:
        return self.service.upload_document(
            self.principal,
            borrower_ref=self.borrower.reference,
            filename=filename,
            content_type=PDF_MIME,
            data=content,
            doc_type="sanction_letter",
            scope=self.scope,
        )


def test_scan_failure_quarantines_before_write(tmp_path: Path) -> None:
    quarantine = InMemoryQuarantine()
    fixture = _Fixture(
        tmp_path,
        scanner=lambda _content: ScanResult(False, "test-scanner", "malware signature"),
        quarantine=quarantine,
    )
    try:
        with pytest.raises(UploadScanFailed, match="malware signature"):
            fixture.upload()

        assert len(quarantine.uploads) == 1
        assert fixture.audit.events[0][0] == "document_upload_quarantined"
        assert fixture.session.scalar(select(func.count(Document.id))) == 0
        assert not (tmp_path / "documents" / "sha256").exists()
    finally:
        fixture.close()


def test_duplicate_returns_existing_no_second_copy(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        first = fixture.upload()
        second = fixture.upload(filename="renamed.pdf")

        assert second.id == first.id
        assert fixture.session.scalar(select(func.count(Document.id))) == 1
        stored_files = [
            path for path in (tmp_path / "documents" / "sha256").rglob("*") if path.is_file()
        ]
        assert len(stored_files) == 1
    finally:
        fixture.close()


def test_oversize_refused_nothing_written(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path, upload_policy=UploadPolicy(max_bytes=12))
    try:
        with pytest.raises(UploadTooLarge, match="12 bytes"):
            fixture.upload(content=b"%PDF-1.7\nthis is too large")

        assert fixture.session.scalar(select(func.count(Document.id))) == 0
        assert not (tmp_path / "documents" / "sha256").exists()
    finally:
        fixture.close()


def test_type_mismatch_refused_naming_both(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        with pytest.raises(UploadTypeMismatch) as raised:
            fixture.upload(content=b"\x89PNG\r\n\x1a\nnot a PDF")

        assert PDF_MIME in str(raised.value)
        assert "image/png" in str(raised.value)
        assert DOCX_MIME not in str(raised.value)
        assert fixture.session.scalar(select(func.count(Document.id))) == 0
    finally:
        fixture.close()


def test_store_unavailable_refuses_upload(tmp_path: Path) -> None:
    class _UnavailableStore:
        def put(self, content: bytes, *, content_hash: str | None = None) -> str:
            del content, content_hash
            raise StorageUnavailable("Document storage is unavailable at /var/lib/documents.")

        def get(self, storage_key: str) -> bytes:
            del storage_key
            raise ExternalServiceError("not used")

        def delete(self, storage_key: str) -> None:
            del storage_key

        def stream(self, storage_key: str, *, chunk_size: int = 1):
            del storage_key, chunk_size
            raise ExternalServiceError("not used")

    fixture = _Fixture(tmp_path, store=_UnavailableStore())
    try:
        with pytest.raises(StorageUnavailable, match="/var/lib/documents"):
            fixture.upload()

        assert fixture.session.scalar(select(func.count(Document.id))) == 0
    finally:
        fixture.close()
