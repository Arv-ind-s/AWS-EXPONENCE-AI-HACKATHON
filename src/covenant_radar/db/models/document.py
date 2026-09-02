"""Document tables: `plan.md §5.4`'s `document`, `document_page` and
`document_span`.

`document_span` is the anchor every extracted field points at — a covenant
version's `source_span_id`, a proposal's evidence, a memo's citation, all
resolve here rather than to a raw page offset each recomputes for itself.

`content_hash` is unique **per borrower** (`Notes` column, `plan.md §5.4`):
a bank re-uploading the same file is recognised without a global collision
between two unrelated borrowers whose extracts happen to hash the same.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models._decimal import FractionValue
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, PortableJSON

_DOC_TYPE_MAX_LENGTH = 50
_FILENAME_MAX_LENGTH = 500
_HASH_MAX_LENGTH = 128
_MIME_TYPE_MAX_LENGTH = 127
_STORAGE_KEY_MAX_LENGTH = 500
_SCAN_RESULT_MAX_LENGTH = 20
_EXTRACTION_STATE_MAX_LENGTH = 20
_RETENTION_CLASS_MAX_LENGTH = 50
_SPAN_TYPE_MAX_LENGTH = 50

_SCAN_RESULTS = ("pending", "clean", "infected", "error")
_EXTRACTION_STATES = ("pending", "in_progress", "complete", "failed")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Document(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """An uploaded file: a sanction letter, a certificate, a statement
    extract — anything `document_page` and `document_span` anchor into."""

    __tablename__ = "document"
    __table_args__ = (
        CheckConstraint(
            f"scan_result IN ({_sql_in_list(_SCAN_RESULTS)})", name="scan_result_valid"
        ),
        CheckConstraint(
            f"extraction_state IN ({_sql_in_list(_EXTRACTION_STATES)})",
            name="extraction_state_valid",
        ),
        UniqueConstraint("borrower_id", "content_hash", name="uq_document_borrower_content_hash"),
        Index("ix_document_borrower_id_doc_type", "borrower_id", "doc_type"),
        Index("ix_document_content_hash", "content_hash"),
    )

    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False
    )
    facility_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("facility.id", ondelete="RESTRICT"), nullable=True
    )
    doc_type: Mapped[str] = mapped_column(String(_DOC_TYPE_MAX_LENGTH), nullable=False)
    filename: Mapped[str] = mapped_column(String(_FILENAME_MAX_LENGTH), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(_HASH_MAX_LENGTH), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(_MIME_TYPE_MAX_LENGTH), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(_STORAGE_KEY_MAX_LENGTH), nullable=False)
    uploaded_by_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    scan_result: Mapped[str] = mapped_column(
        String(_SCAN_RESULT_MAX_LENGTH), nullable=False, default="pending"
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extraction_state: Mapped[str] = mapped_column(
        String(_EXTRACTION_STATE_MAX_LENGTH), nullable=False, default="pending"
    )
    ocr_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retention_class: Mapped[str | None] = mapped_column(
        String(_RETENTION_CLASS_MAX_LENGTH), nullable=True
    )
    purge_after: Mapped[date | None] = mapped_column(Date, nullable=True)


class DocumentPage(Base, UserAttributedColumns, StandardColumns):
    """One page of a `Document`, with its extracted text and OCR
    confidence. Ingested by extraction, not user-edited, so it carries no
    `version` column — the same reasoning as `FacilityConduct`."""

    __tablename__ = "document_page"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_document_page_document_page"),
    )

    document_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DocumentSpan(Base, UserAttributedColumns, StandardColumns):
    """One anchored region of extracted text — the thing every downstream
    field, proposal or citation points back at. Ingested, not user-edited."""

    __tablename__ = "document_span"
    __table_args__ = (
        CheckConstraint("end_offset > start_offset", name="end_offset_after_start_offset"),
        Index("ix_document_span_document_id_page_number", "document_id", "page_number"),
    )

    document_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox: Mapped[list[float] | None] = mapped_column(PortableJSON, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    span_type: Mapped[str | None] = mapped_column(String(_SPAN_TYPE_MAX_LENGTH), nullable=True)
