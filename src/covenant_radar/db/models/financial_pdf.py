"""Durable review batches for uploaded quarterly financial-statement PDFs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, PortableJSON


class FinancialPdfBatch(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A borrower-scoped PDF submission and its immutable extraction evidence.

    Candidate values live in JSON deliberately: they are review artifacts, not
    financial truth. Only approved values are written to ``financial_period``.
    """

    __tablename__ = "financial_pdf_batch"
    __table_args__ = (Index("ix_financial_pdf_batch_borrower_state", "borrower_id", "state"),)

    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    documents: Mapped[list[dict[str, object]]] = mapped_column(PortableJSON, nullable=False)
    candidates: Mapped[list[dict[str, object]]] = mapped_column(PortableJSON, nullable=False)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
