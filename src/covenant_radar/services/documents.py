"""Document upload and retrieval use cases.

The service coordinates the scoped metadata repository, the validation/scan
pipeline, the encrypted byte store, and the audit port.  It never accepts a
document into the database before the scanner has cleared it and never
returns metadata for a document outside the caller's portfolio scope.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import BinaryIO, Protocol, cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.config.settings import get_settings
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import (
    AuthorizationError,
    Conflict,
    NotFound,
    ValidationError,
)
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.document import Document, DocumentPage, DocumentSpan
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import OverrideRecord
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.repositories.document import DocumentRepository
from covenant_radar.db.repositories.facility import FacilityRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.documents.classify import (
    DOCUMENT_TYPES,
    ClassificationResult,
    classify_pages,
)
from covenant_radar.documents.extract_native import (
    NativeExtractionResult,
    NativePdfExtractionError,
    NativePdfExtractor,
)
from covenant_radar.documents.ocr import (
    OcrExtractionResult,
    OcrPipeline,
    is_history_span,
    page_is_eligible_for_detection,
    page_version_span_type,
    spans_from_text,
)
from covenant_radar.documents.scan import DocumentScanPipeline, QuarantineSink
from covenant_radar.documents.spans import SpanIndex, TextSpan
from covenant_radar.ports.document_store import DocumentStore
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize
from covenant_radar.security.uploads import UploadPolicy, UploadScanFailed, VirusScanner

_LOGGER = logging.getLogger(__name__)
_REQUEST_ID_MAX_LENGTH = 40
_DOC_TYPE_MAX_LENGTH = 50
_RETENTION_CLASS_MAX_LENGTH = 50
_DEFAULT_RETENTION_CLASS = "source_document"
_MAX_CORRECTED_PAGE_TEXT_LENGTH = 1_000_000
_OCR_REVIEW_REASON = "OCR confidence is below the configured floor."
_OCR_UNAVAILABLE_REASON = "OCR is unavailable or did not return readable text."
_CLASSIFICATION_SCAN_LIMIT = 5
_CLASSIFICATION_PAGE_LIMIT = 2
_OVERRIDE_REASON_MAX_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    """A scoped page requiring human review before clause detection."""

    document: Document
    page: DocumentPage
    reason: str


@dataclass(frozen=True, slots=True)
class DetectionPage:
    """A scoped page whose text is safe for automated clause detection."""

    document: Document
    page: DocumentPage


class AuditWriter(Protocol):
    """The append-only audit port from contract C-60."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's transaction."""


class DocumentService:
    """Scoped document upload and retrieval operations."""

    def __init__(
        self,
        session: Session,
        *,
        store: DocumentStore,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        scanner: VirusScanner | None = None,
        upload_policy: UploadPolicy | None = None,
        quarantine: QuarantineSink | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        native_extractor: NativePdfExtractor | None = None,
        ocr_pipeline: OcrPipeline | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("DocumentService requires a SQLAlchemy Session.")
        if any(
            not callable(getattr(store, method, None))
            for method in ("put", "get", "delete", "stream")
        ):
            raise TypeError("DocumentService requires a DocumentStore.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("DocumentService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("DocumentService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("DocumentService scope_resolver must be callable.")
        if native_extractor is not None and not callable(
            getattr(native_extractor, "extract", None)
        ):
            raise TypeError("DocumentService native_extractor must expose extract().")
        if ocr_pipeline is not None and not callable(getattr(ocr_pipeline, "process", None)):
            raise TypeError("DocumentService ocr_pipeline must expose process().")

        self.session = session
        self.store = store
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if (
            not isinstance(self.request_id, str)
            or not 1 <= len(self.request_id) <= _REQUEST_ID_MAX_LENGTH
        ):
            raise ValueError(
                f"Document request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
            )
        self.scope_resolver = scope_resolver
        self.scans = DocumentScanPipeline(
            policy=upload_policy,
            scanner=scanner,
            quarantine=quarantine,
        )
        self.native_extractor = native_extractor or NativePdfExtractor()
        self.ocr_pipeline = ocr_pipeline or OcrPipeline.from_settings(get_settings().documents)
        self.borrowers = BorrowerRepository(session, audit=audit)
        self.facilities = FacilityRepository(session, audit=audit)
        self.documents = DocumentRepository(session, audit=audit)

    def upload_document(
        self,
        principal: Principal,
        *,
        borrower_ref: str | None = None,
        borrower_reference: str | None = None,
        filename: str,
        content_type: str,
        data: bytes | bytearray | memoryview | BinaryIO,
        doc_type: str,
        facility_id: UUID | None = None,
        retention_class: str = _DEFAULT_RETENTION_CLASS,
        purge_after: date | None = None,
        scope: Scope | None = None,
    ) -> Document:
        """Validate, scan, encrypt, store, and register one document.

        ``borrower_ref`` is the C-04 name.  ``borrower_reference`` is an
        explicit compatibility spelling for service callers; supplying both
        is allowed only when they identify the same borrower.
        """
        resolved_scope = self._write_context(principal, scope)
        reference = _coalesce_reference(borrower_ref, borrower_reference)
        borrower = self.borrowers.by_reference(
            _required_text(reference, "borrower_ref", maximum=20),
            scope=resolved_scope,
        )
        if borrower is None:
            raise NotFound(f"Borrower {reference!r} was not found within the current scope.")
        facility = self._facility_in_scope(facility_id, borrower, resolved_scope)
        normalized_doc_type = _required_text(doc_type, "doc_type", maximum=_DOC_TYPE_MAX_LENGTH)
        normalized_retention = _required_text(
            retention_class,
            "retention_class",
            maximum=_RETENTION_CLASS_MAX_LENGTH,
        )
        normalized_purge_after = _purge_date(purge_after)
        now = self._now()

        try:
            validated = self.scans.validate(
                filename,
                content_type,
                data,
                occurred_at=now,
            )
        except UploadScanFailed as error:
            self.audit.record(
                AuditEventType.DOCUMENT_UPLOAD_QUARANTINED.value,
                ("borrower", borrower.id),
                {
                    "action": "quarantined",
                    "borrower_id": str(borrower.id),
                    "filename": error.filename or filename,
                    "declared_type": error.declared_type or content_type,
                    "detected_type": error.detected_type,
                    "content_hash": _content_hash(data),
                    "reason": str(error)[:500],
                },
                actor=principal.id,
                request_id=self.request_id,
            )
            raise
        content_hash = hashlib.sha256(validated.content).hexdigest()
        existing = self.documents.by_content_hash(
            borrower.id,
            content_hash,
            scope=resolved_scope,
        )
        if existing is not None:
            return existing

        storage_key = self._store(validated.content, content_hash)
        document = Document(
            id=_new_document_id(),
            borrower_id=borrower.id,
            facility_id=facility.id if facility is not None else None,
            doc_type=normalized_doc_type,
            filename=validated.filename,
            content_hash=content_hash,
            byte_size=validated.size_bytes,
            mime_type=validated.detected_type,
            storage_key=storage_key,
            uploaded_by_id=self._user_id(principal),
            scan_result="clean",
            extraction_state="pending",
            ocr_applied=False,
            retention_class=normalized_retention,
            purge_after=normalized_purge_after,
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=self.request_id,
            version=1,
        )
        try:
            self.documents.add(document)
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as error:
            existing = self.documents.by_content_hash(
                borrower.id,
                content_hash,
                scope=resolved_scope,
            )
            if existing is not None:
                return existing
            self._cleanup_storage(storage_key)
            raise Conflict(
                "The document could not be registered because its metadata conflicted."
            ) from error
        except Exception:
            self._cleanup_storage(storage_key)
            raise

        try:
            self.audit.record(
                AuditEventType.DOCUMENT_UPLOADED.value,
                ("document", document.id),
                {
                    "action": "uploaded",
                    "borrower_id": str(document.borrower_id),
                    "document_id": str(document.id),
                    "content_hash": document.content_hash,
                    "byte_size": document.byte_size,
                    "mime_type": document.mime_type,
                    "doc_type": document.doc_type,
                    "retention_class": document.retention_class,
                    "scan_engine": validated.scan.engine,
                },
                actor=principal.id,
                request_id=self.request_id,
            )
        except Exception:
            self._cleanup_storage(storage_key)
            raise
        return document

    def upload_file(
        self,
        principal: Principal,
        *,
        borrower_ref: str,
        doc_type: str,
        upload: object,
        facility_id: UUID | None = None,
        retention_class: str = _DEFAULT_RETENTION_CLASS,
        purge_after: date | None = None,
        scope: Scope | None = None,
    ) -> Document:
        """Adapt a FastAPI/Starlette-style upload object to the service port."""
        filename = getattr(upload, "filename", None)
        content_type = getattr(upload, "content_type", None)
        data = getattr(upload, "file", upload)
        if not isinstance(filename, str) or not isinstance(content_type, str):
            raise ValidationError(
                "The uploaded file must provide filename and content_type.", field="file"
            )
        if not isinstance(data, bytes | bytearray | memoryview) and not callable(
            getattr(data, "read", None)
        ):
            raise ValidationError("The uploaded file content must be binary.", field="file")
        return self.upload_document(
            principal,
            borrower_ref=borrower_ref,
            filename=filename,
            content_type=content_type,
            data=cast(bytes | bytearray | memoryview | BinaryIO, data),
            doc_type=doc_type,
            facility_id=facility_id,
            retention_class=retention_class,
            purge_after=purge_after,
            scope=scope,
        )

    def get_document(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> Document:
        """Return metadata for one document in the caller's scope."""
        resolved_scope = self._read_context(principal, scope)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        return document

    def stream_document(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        scope: Scope | None = None,
        chunk_size: int = 256 * 1024,
    ) -> Iterator[bytes]:
        """Return a scope-checked, incrementally decrypted document stream."""
        document = self.get_document(principal, document_id, scope=scope)
        return self.store.stream(document.storage_key, chunk_size=chunk_size)

    def list_documents(
        self,
        principal: Principal,
        borrower_id: UUID,
        *,
        scope: Scope | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[Document, ...]:
        """List scoped documents for one borrower."""
        resolved_scope = self._read_context(principal, scope)
        borrower = self.borrowers.get(borrower_id, scope=resolved_scope)
        if borrower is None:
            raise NotFound(f"Borrower {borrower_id} was not found within the current scope.")
        return tuple(
            self.documents.for_borrower(
                borrower.id,
                scope=resolved_scope,
                limit=limit,
                offset=offset,
            )
        )

    def list_review_pages(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        limit: int = 200,
    ) -> tuple[ReviewQueueItem, ...]:
        """Return only scoped pages that a person must review before detection."""
        resolved_scope = self._read_context(principal, scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Document review queue limit must be between 1 and 200.")
        rows = self._page_rows(resolved_scope, needs_review=True, limit=limit)
        return tuple(
            ReviewQueueItem(document=document, page=page, reason=self._review_reason(page))
            for page, document in rows
        )

    def review_queue(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        limit: int = 200,
    ) -> tuple[ReviewQueueItem, ...]:
        """Compatibility-facing name for the human-review queue."""
        return self.list_review_pages(principal, scope=scope, limit=limit)

    def list_detection_pages(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        limit: int = 200,
    ) -> tuple[DetectionPage, ...]:
        """Return pages eligible for downstream automated clause detection.

        The query applies the review flag before rows leave the database and
        repeats the value-object check in Python as a defensive invariant.
        A low-confidence page therefore cannot reach a future detector by
        accidentally reusing the review queue's page collection.
        """
        resolved_scope = self._read_context(principal, scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Document detection page limit must be between 1 and 200.")
        rows = self._page_rows(resolved_scope, needs_review=False, limit=limit)
        return tuple(
            DetectionPage(document=document, page=page)
            for page, document in rows
            if page_is_eligible_for_detection(text=page.text, needs_review=page.needs_review)
        )

    def correct_page(
        self,
        principal: Principal,
        document_id: UUID,
        page_number: int,
        corrected_text: str,
        *,
        expected_version: int | None = None,
        scope: Scope | None = None,
    ) -> DocumentPage:
        """Store a reviewed page correction while retaining its source text.

        The current page row becomes the active reviewed text.  The prior and
        new full-page values, plus superseded line spans, remain in the span
        ledger with actor, timestamp and request provenance.  Requiring the
        document version prevents two reviewers from silently overwriting one
        another.
        """
        resolved_scope = self._write_context(
            principal,
            scope,
            permission=Permission.CORRECT_SOURCE_DATA,
        )
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise ValidationError("page_number must be a positive integer.", field="page_number")
        normalized_text = _corrected_page_text(corrected_text)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        current_version = document.version or 1
        if expected_version is not None:
            if (
                isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version < 1
            ):
                raise ValidationError(
                    "expected_version must be a positive integer.", field="expected_version"
                )
            if expected_version != current_version:
                raise Conflict("The document changed while this page was being reviewed.")
        page = self.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == page_number,
            )
        )
        if page is None:
            raise NotFound(f"Document page {page_number} was not found for document {document_id}.")
        if not page.needs_review:
            raise Conflict("The document page no longer requires human review.")

        now = self._now()
        new_version = current_version + 1
        original_text = page.text
        with self.session.begin_nested():
            active_spans = tuple(
                self.session.scalars(
                    select(DocumentSpan)
                    .where(
                        DocumentSpan.document_id == document.id,
                        DocumentSpan.page_number == page_number,
                    )
                    .order_by(DocumentSpan.start_offset, DocumentSpan.end_offset, DocumentSpan.id)
                ).all()
            )
            for span in active_spans:
                if not is_history_span(span.span_type):
                    span.span_type = "superseded"
            if original_text:
                self._add_page_version_span(
                    document,
                    page_number,
                    original_text,
                    page_version_span_type(current_version, "original"),
                    principal,
                    now,
                )
            self._add_page_version_span(
                document,
                page_number,
                normalized_text,
                page_version_span_type(new_version, "corrected"),
                principal,
                now,
            )
            for span in spans_from_text(page_number, normalized_text):
                self.session.add(
                    DocumentSpan(
                        id=new_id(),
                        document_id=document.id,
                        page_number=page_number,
                        start_offset=span.start_offset,
                        end_offset=span.end_offset,
                        bbox=None,
                        text=span.text,
                        span_type=span.span_type,
                        created_at=now,
                        updated_at=now,
                        created_by_id=principal.id,
                        updated_by_id=principal.id,
                        request_id=self.request_id,
                    )
                )
            page.text = normalized_text
            page.ocr_confidence = Decimal("1.0000")
            page.needs_review = False
            page.updated_at = now
            page.updated_by_id = principal.id
            page.request_id = self.request_id
            document.updated_at = now
            document.updated_by_id = principal.id
            document.version = new_version
            self.session.flush()
            self.audit.record(
                AuditEventType.DOCUMENT_PAGE_CORRECTED.value,
                ("document", document.id),
                {
                    "action": "page_corrected",
                    "document_id": str(document.id),
                    "page_number": page_number,
                    "previous_version": current_version,
                    "new_version": new_version,
                    "original_text_hash": _text_hash(original_text),
                    "corrected_text_hash": _text_hash(normalized_text),
                    "original_retained": bool(original_text),
                    "provenance": "manual_correction",
                },
                actor=principal.id,
                request_id=self.request_id,
            )
        return page

    def classify_document(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> ClassificationResult:
        """Classify a document from its own already-extracted page text.

        Classification is recomputed on every call rather than cached: the
        schema carries no classification column, and a page correction or a
        re-extraction must be reflected immediately rather than through a
        second, easily stale write path.
        """
        resolved_scope = self._read_context(principal, scope)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        return classify_pages(self._classification_page_texts(document.id))

    def get_classification_override(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> OverrideRecord | None:
        """Return the latest manual classification override, if any."""
        resolved_scope = self._read_context(principal, scope)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        return self.session.scalar(
            select(OverrideRecord)
            .where(
                OverrideRecord.subject_type == "document",
                OverrideRecord.subject_id == document.id,
                OverrideRecord.stage == "classification",
            )
            .order_by(OverrideRecord.created_at.desc(), OverrideRecord.id.desc())
            .limit(1)
        )

    def override_classification(
        self,
        principal: Principal,
        document_id: UUID,
        doc_type: str,
        reason: str,
        *,
        scope: Scope | None = None,
    ) -> OverrideRecord:
        """Record a person's classification choice alongside the automatic one.

        The automatic result is recomputed here and retained as ``shown``
        rather than trusted from an earlier request, so the override is
        always evidence about what the classifier actually proposed at
        decision time. The automatic result itself is never overwritten:
        both remain readable side by side through
        :meth:`classify_document` and :meth:`get_classification_override`.
        """
        resolved_scope = self._write_context(
            principal,
            scope,
            permission=Permission.CORRECT_SOURCE_DATA,
        )
        normalized_doc_type = _validated_override_doc_type(doc_type)
        normalized_reason = _required_text(reason, "reason", maximum=_OVERRIDE_REASON_MAX_LENGTH)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        automatic = classify_pages(self._classification_page_texts(document.id))
        now = self._now()
        record = OverrideRecord(
            id=new_id(),
            subject_type="document",
            subject_id=document.id,
            stage="classification",
            shown={"doc_type": automatic.doc_type, "confidence": str(automatic.confidence)},
            user_action="reclassify",
            user_value={"doc_type": normalized_doc_type},
            reason=normalized_reason,
            actor_id=self._user_id(principal),
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=self.request_id,
        )
        with self.session.begin_nested():
            self.session.add(record)
            self.session.flush()
            self.audit.record(
                AuditEventType.DOCUMENT_CLASSIFICATION_OVERRIDDEN.value,
                ("document", document.id),
                {
                    "action": "classification_overridden",
                    "document_id": str(document.id),
                    "automatic_doc_type": automatic.doc_type,
                    "automatic_confidence": str(automatic.confidence),
                    "override_doc_type": normalized_doc_type,
                    "reason": normalized_reason,
                },
                actor=principal.id,
                request_id=self.request_id,
            )
        return record

    def get_page(
        self,
        principal: Principal,
        document_id: UUID,
        page_number: int,
        *,
        scope: Scope | None = None,
    ) -> DocumentPage:
        """Return one scoped page row, e.g. for the span-highlighting viewer."""
        resolved_scope = self._read_context(principal, scope)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        page = self.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == page_number,
            )
        )
        if page is None:
            raise NotFound(f"Document page {page_number} was not found for document {document_id}.")
        return page

    def page_was_corrected(
        self,
        principal: Principal,
        document_id: UUID,
        page_number: int,
        *,
        scope: Scope | None = None,
    ) -> bool:
        """Whether a page's active text descends from a reviewer correction.

        Reuses the page-version provenance :meth:`correct_page` already
        writes: a page carries a history span if and only if it has been
        corrected at least once, so this needs no separate flag or a
        schema migration.
        """
        resolved_scope = self._read_context(principal, scope)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        span_types = self.session.scalars(
            select(DocumentSpan.span_type).where(
                DocumentSpan.document_id == document.id,
                DocumentSpan.page_number == page_number,
            )
        ).all()
        return any(is_history_span(span_type) for span_type in span_types)

    def _classification_page_texts(self, document_id: UUID) -> tuple[tuple[int, str], ...]:
        rows = tuple(
            self.session.scalars(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_number)
                .limit(_CLASSIFICATION_SCAN_LIMIT)
            ).all()
        )
        eligible = tuple(
            (row.page_number, cast(str, row.text))
            for row in rows
            if page_is_eligible_for_detection(text=row.text, needs_review=row.needs_review)
        )
        return eligible[:_CLASSIFICATION_PAGE_LIMIT]

    def _page_rows(
        self,
        scope: Scope,
        *,
        needs_review: bool,
        limit: int,
    ) -> tuple[tuple[DocumentPage, Document], ...]:
        statement = (
            select(DocumentPage, Document)
            .join(Document, Document.id == DocumentPage.document_id)
            .join(Borrower, Borrower.id == Document.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(
                DocumentPage.needs_review.is_(needs_review),
                scope.predicate(Portfolio.path),
            )
            .order_by(DocumentPage.updated_at, Document.id, DocumentPage.page_number)
            .limit(limit)
        )
        return tuple(self.session.execute(statement).all())

    def _review_reason(self, page: DocumentPage) -> str:
        confidence = page.ocr_confidence
        if confidence is None:
            return _OCR_UNAVAILABLE_REASON
        floor = self.ocr_pipeline.confidence_floor
        if confidence < floor:
            return f"{_OCR_REVIEW_REASON} Observed {confidence:.2f}; floor {floor:.2f}."
        return "The page requires human review before automated detection."

    def _add_page_version_span(
        self,
        document: Document,
        page_number: int,
        text: str,
        span_type: str,
        principal: Principal,
        now: datetime,
    ) -> None:
        self.session.add(
            DocumentSpan(
                id=new_id(),
                document_id=document.id,
                page_number=page_number,
                start_offset=0,
                end_offset=len(text),
                bbox=None,
                text=text,
                span_type=span_type,
                created_at=now,
                updated_at=now,
                created_by_id=principal.id,
                updated_by_id=principal.id,
                request_id=self.request_id,
            )
        )

    def extract_document(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> NativeExtractionResult:
        """Extract and persist native PDF text and coordinate spans.

        The PDF is parsed before any child rows are changed.  Persistence is
        then performed inside one savepoint, replacing any prior extraction
        atomically.  A malformed or encrypted PDF removes child rows, marks
        the document as failed, and raises a page-specific error; it can
        therefore never leave a partly indexed document behind.
        """
        resolved_scope = self._read_context(principal, scope)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        if document.mime_type.lower() != "application/pdf":
            raise ValidationError(
                "Native extraction requires an application/pdf document.",
                field="document.mime_type",
            )

        try:
            document_bytes = self.store.get(document.storage_key)
            result = self.native_extractor.extract(document_bytes)
        except NativePdfExtractionError as error:
            self._record_extraction_failure(document, principal, error)
            raise
        if not isinstance(result, NativeExtractionResult):
            raise TypeError("Native PDF extractor must return NativeExtractionResult.")
        ocr_result = self.ocr_pipeline.process(document_bytes, result)
        if not isinstance(ocr_result, OcrExtractionResult):
            raise TypeError("Document OCR pipeline must return OcrExtractionResult.")

        now = self._now()
        with self.session.begin_nested():
            self._delete_extraction_rows(document.id)
            self._add_extraction_rows(document, result, principal, now, ocr_result=ocr_result)
            document.page_count = result.page_count
            document.extraction_state = result.extraction_state
            document.ocr_applied = bool(ocr_result.attempted_pages)
            document.updated_at = now
            document.updated_by_id = principal.id
            document.version = (document.version or 0) + 1
            self.session.flush()
            self.audit.record(
                AuditEventType.DOCUMENT_NATIVE_EXTRACTED.value,
                ("document", document.id),
                {
                    "action": "native_extraction",
                    "document_id": str(document.id),
                    "page_count": result.page_count,
                    "span_count": result.span_count,
                    "pages_needing_ocr": list(result.pages_needing_ocr),
                    "rotations": [
                        {"page_number": page.page_number, "degrees": page.rotation}
                        for page in result.pages
                        if page.rotation
                    ],
                },
                actor=principal.id,
                request_id=self.request_id,
            )
            if ocr_result.pages:
                self.audit.record(
                    AuditEventType.DOCUMENT_OCR_PROCESSED.value,
                    ("document", document.id),
                    {
                        "action": "ocr_processed",
                        "document_id": str(document.id),
                        "capability_available": ocr_result.capability.available,
                        "capability_detail": ocr_result.capability.detail,
                        "attempted_pages": list(ocr_result.attempted_pages),
                        "pages_needing_review": list(ocr_result.pages_needing_review),
                        "pages": [
                            {
                                "page_number": page.page_number,
                                "confidence": (
                                    str(page.confidence) if page.confidence is not None else None
                                ),
                                "needs_review": page.page_number
                                in ocr_result.pages_needing_review,
                                "reason": page.reason,
                            }
                            for page in ocr_result.pages
                        ],
                    },
                    actor=principal.id,
                    request_id=self.request_id,
                )
        return result

    def extract_native(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> NativeExtractionResult:
        """Explicit alias for :meth:`extract_document` at the service boundary."""
        return self.extract_document(principal, document_id, scope=scope)

    def lookup_spans(
        self,
        principal: Principal,
        document_id: UUID,
        page_number: int,
        start_offset: int,
        end_offset: int,
        *,
        scope: Scope | None = None,
    ) -> tuple[DocumentSpan, ...]:
        """Resolve stored spans after scope and page bounds validation."""
        resolved_scope = self._read_context(principal, scope)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        page = self.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document.id,
                DocumentPage.page_number == page_number,
            )
        )
        if page is None:
            raise NotFound(f"Document page {page_number} was not found for document {document_id}.")
        rows = tuple(
            self.session.scalars(
                select(DocumentSpan)
                .where(
                    DocumentSpan.document_id == document.id,
                    DocumentSpan.page_number == page_number,
                )
                .order_by(DocumentSpan.start_offset, DocumentSpan.end_offset, DocumentSpan.id)
            ).all()
        )
        index = SpanIndex({page_number: page.text or ""})
        active_rows = tuple(row for row in rows if not is_history_span(row.span_type))
        for row in active_rows:
            index.add(_text_span_from_row(row))
        index.lookup(page_number, start_offset, end_offset)
        return tuple(
            row
            for row in active_rows
            if row.start_offset < end_offset and row.end_offset > start_offset
        )

    def lookup_span(
        self,
        principal: Principal,
        document_id: UUID,
        page_number: int,
        start_offset: int,
        end_offset: int,
        *,
        scope: Scope | None = None,
    ) -> tuple[DocumentSpan, ...]:
        """Singular-name compatibility wrapper for range span lookup."""
        return self.lookup_spans(
            principal,
            document_id,
            page_number,
            start_offset,
            end_offset,
            scope=scope,
        )

    def _add_extraction_rows(
        self,
        document: Document,
        result: NativeExtractionResult,
        principal: Principal,
        now: datetime,
        *,
        ocr_result: OcrExtractionResult | None = None,
    ) -> None:
        ocr_pages = {
            page.page_number: page for page in (ocr_result.pages if ocr_result is not None else ())
        }
        for page in result.pages:
            ocr_page = ocr_pages.get(page.page_number)
            page_text = page.text
            page_spans = page.spans
            needs_review = page.needs_ocr
            ocr_confidence = None
            if ocr_page is not None:
                page_text = ocr_page.text
                ocr_confidence = ocr_page.confidence
                needs_review = OcrExtractionResult.needs_review(ocr_page)
                page_spans = ocr_page.spans if not needs_review else ()
            page_row = DocumentPage(
                id=new_id(),
                document_id=document.id,
                page_number=page.page_number,
                text=page_text,
                ocr_confidence=ocr_confidence,
                needs_review=needs_review,
                width=_database_dimension(page.width),
                height=_database_dimension(page.height),
                created_at=now,
                updated_at=now,
                created_by_id=principal.id,
                updated_by_id=principal.id,
                request_id=self.request_id,
            )
            self.session.add(page_row)
            for span in page_spans:
                self.session.add(
                    DocumentSpan(
                        id=new_id(),
                        document_id=document.id,
                        page_number=span.page_number,
                        start_offset=span.start_offset,
                        end_offset=span.end_offset,
                        bbox=list(span.bbox) if span.bbox is not None else None,
                        text=span.text,
                        span_type=span.span_type,
                        created_at=now,
                        updated_at=now,
                        created_by_id=principal.id,
                        updated_by_id=principal.id,
                        request_id=self.request_id,
                    )
                )

    def _record_extraction_failure(
        self,
        document: Document,
        principal: Principal,
        error: NativePdfExtractionError,
    ) -> None:
        now = self._now()
        with self.session.begin_nested():
            self._delete_extraction_rows(document.id)
            self._set_document_extraction_state(document, "failed", now, principal.id)
            document.page_count = None
            document.ocr_applied = False
            self.session.flush()
            self.audit.record(
                AuditEventType.DOCUMENT_NATIVE_EXTRACTION_FAILED.value,
                ("document", document.id),
                {
                    "action": "native_extraction_failed",
                    "document_id": str(document.id),
                    "page_number": error.page_number,
                    "reason": error.reason,
                },
                actor=principal.id,
                request_id=self.request_id,
            )

    def _delete_extraction_rows(self, document_id: UUID) -> None:
        self.session.execute(delete(DocumentSpan).where(DocumentSpan.document_id == document_id))
        self.session.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))

    @staticmethod
    def _set_document_extraction_state(
        document: Document,
        state: str,
        now: datetime,
        actor_id: UUID,
    ) -> None:
        document.extraction_state = state
        document.updated_at = now
        document.updated_by_id = actor_id
        document.version = (document.version or 0) + 1

    def _facility_in_scope(
        self,
        facility_id: UUID | None,
        borrower: Borrower,
        scope: Scope,
    ) -> Facility | None:
        if facility_id is None:
            return None
        facility = self.facilities.get(facility_id, scope=scope)
        if facility is None or facility.borrower_id != borrower.id:
            raise NotFound(f"Facility {facility_id} was not found for the borrower in scope.")
        return facility

    def _store(self, content: bytes, content_hash: str) -> str:
        storage_key = self.store.put(content, content_hash=content_hash)
        if (
            not isinstance(storage_key, str)
            or not storage_key.strip()
            or len(storage_key) > 500
            or any(ord(character) < 32 or ord(character) == 127 for character in storage_key)
        ):
            raise TypeError("DocumentStore.put must return a non-empty storage key.")
        return storage_key

    def _cleanup_storage(self, storage_key: str) -> None:
        try:
            self.store.delete(storage_key)
        except NotFound:
            return
        except Exception:
            _LOGGER.exception("Document storage cleanup failed for %s", storage_key)

    def _read_context(self, principal: Principal, scope: Scope | None) -> Scope:
        self._require_principal(principal, Permission.VIEW_DOCUMENT)
        return self._validated_scope(principal, scope)

    def _write_context(
        self,
        principal: Principal,
        scope: Scope | None,
        *,
        permission: Permission = Permission.UPLOAD_DOCUMENT,
    ) -> Scope:
        self._require_principal(principal, permission)
        if principal.kind is not PrincipalKind.USER:
            raise AuthorizationError("Document changes require an authenticated user principal.")
        return self._validated_scope(principal, scope)

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        if scope is None:
            resolved = (
                self.scope_resolver(principal)
                if self.scope_resolver is not None
                else resolve_scope(principal, self.session)
            )
            if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
                raise AuthorizationError(
                    "The resolved scope does not belong to the authenticated principal."
                )
            return resolved
        if not isinstance(scope, Scope) or scope.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied scope does not belong to the authenticated principal."
            )
        return scope

    @staticmethod
    def _require_principal(principal: Principal, permission: Permission) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, permission)

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Document clock must return an aware datetime.")
        return now.astimezone(UTC)

    @staticmethod
    def _user_id(principal: Principal) -> UUID:
        if principal.kind is not PrincipalKind.USER:
            raise AuthorizationError("Document uploads require an authenticated user principal.")
        return principal.id


def _coalesce_reference(first: str | None, second: str | None) -> str:
    if first is None and second is None:
        raise ValidationError("borrower_ref is required.", field="borrower_ref")
    if (
        first is not None
        and second is not None
        and (
            not isinstance(first, str)
            or not isinstance(second, str)
            or first.strip() != second.strip()
        )
    ):
        raise ValidationError(
            "borrower_ref and borrower_reference identify different borrowers.",
            field="borrower_ref",
        )
    return first if first is not None else second or ""


def _required_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} is required.", field=field)
    clean = value.strip()
    if not clean:
        raise ValidationError(f"{field} is required.", field=field)
    if len(clean) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ValidationError(f"{field} contains an invalid control character.", field=field)
    return clean


def _purge_date(value: date | None) -> date | None:
    if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
        raise ValidationError("purge_after must be a calendar date or null.", field="purge_after")
    return value


def _content_hash(data: object) -> str | None:
    if isinstance(data, bytes | bytearray | memoryview):
        return hashlib.sha256(bytes(data)).hexdigest()
    return None


def _text_hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _corrected_page_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("corrected_text is required.", field="corrected_text")
    normalized = value.strip()
    if not normalized:
        raise ValidationError("corrected_text is required.", field="corrected_text")
    if len(normalized) > _MAX_CORRECTED_PAGE_TEXT_LENGTH:
        raise ValidationError(
            "corrected_text exceeds the supported page limit.", field="corrected_text"
        )
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise ValidationError(
            "corrected_text contains an invalid control character.", field="corrected_text"
        )
    return normalized


def _validated_override_doc_type(value: object) -> str:
    if not isinstance(value, str) or value.strip() not in DOCUMENT_TYPES:
        raise ValidationError(
            f"doc_type must be one of: {', '.join(DOCUMENT_TYPES)}.", field="doc_type"
        )
    return value.strip()


def _new_document_id() -> UUID:
    return new_id()


def _text_span_from_row(row: DocumentSpan) -> TextSpan:
    bbox = row.bbox
    normalized_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        if not isinstance(bbox, tuple | list) or len(bbox) != 4:
            raise ValueError("A stored document span bbox must contain four coordinates.")
        normalized_bbox = (
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
        )
    return TextSpan(
        page_number=row.page_number,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        text=row.text,
        bbox=normalized_bbox,
        span_type=row.span_type or "line",
    )


def _database_dimension(value: float) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Extracted PDF page dimensions must be numeric.")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Extracted PDF page dimensions must be positive and finite.")
    rounded = int(round(value))
    if not 1 <= rounded <= 2_147_483_647:
        raise ValueError("Extracted PDF page dimensions exceed the database range.")
    return rounded


DocumentUploadService = DocumentService


__all__ = [
    "AuditWriter",
    "DetectionPage",
    "DocumentService",
    "DocumentUploadService",
    "ReviewQueueItem",
]
