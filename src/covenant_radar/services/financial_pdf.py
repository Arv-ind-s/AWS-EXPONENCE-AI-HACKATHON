"""PDF financial-statement intake with conservative deterministic extraction."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.errors import Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.document import DocumentPage
from covenant_radar.db.models.financial_pdf import FinancialPdfBatch
from covenant_radar.db.models.statements import (
    FieldProvenance,
    FinancialPeriod,
    ImportBatch,
    ImportMapping,
    StatementLineValue,
)
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.statements.chart import default_chart
from covenant_radar.domain.covenants.calendar import RetestTrigger, RetestTriggerKind
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize
from covenant_radar.services.documents import DocumentService
from covenant_radar.services.engine import EngineService

_MAX_FILES = 8
_AUTO_ACCEPT = Decimal("0.90")
_ALIASES = {
    "revenue": ("revenue", "turnover", "total income"),
    "ebitda": ("ebitda", "earnings before interest"),
    "finance_cost": ("finance cost", "interest expense", "finance charges"),
    "profit_after_tax": ("profit after tax", "profit for the period", "net profit"),
    "current_assets": ("current assets",),
    "current_liabilities": ("current liabilities",),
    "total_debt": ("total debt", "borrowings"),
    "long_term_debt": ("long term borrowings", "long-term debt"),
    "short_term_debt": ("short term borrowings", "short-term debt"),
    "tangible_net_worth": ("tangible net worth", "net worth"),
    "cash_flow_debt_service": ("cash flow available for debt service", "cfads"),
}
_DATE = re.compile(r"(?:quarter|period)\s+ended\s+(\d{1,2}[\s/-][A-Za-z]{3,9}[\s/-]\d{2,4}|\d{4}-\d{2}-\d{2})", re.I)
_NUMBER = re.compile(r"\(?-?[\d,]+(?:\.\d+)?\)?")


class AuditWriter(Protocol):
    def record(self, event_type: str, subject: object, payload: dict[str, object], *, actor: object, request_id: str) -> object: ...


class FinancialPdfService:
    def __init__(self, session: Session, *, documents: DocumentService, audit: AuditWriter, clock: Clock | None = None) -> None:
        if not is_database_session(session):
            raise TypeError("FinancialPdfService requires a SQLAlchemy Session.")
        self.session, self.documents, self.audit, self.clock = session, documents, audit, clock or SystemClock()

    def submit(self, principal: Principal, *, borrower_ref: str, uploads: list[object], scope: Scope | None = None) -> FinancialPdfBatch:
        authorize(principal, Permission.INGEST_FINANCIAL_STATEMENTS)
        if not 1 <= len(uploads) <= _MAX_FILES:
            raise ValidationError("Upload between 1 and 8 quarterly PDF statements.", field="files")
        resolved_scope = scope or resolve_scope(principal, self.session)
        borrower = self.session.scalar(select(Borrower).where(Borrower.reference == borrower_ref, resolved_scope.predicate(Borrower.portfolio_id)))
        if borrower is None:
            raise NotFound("Borrower was not found within the current scope.")
        documents: list[dict[str, object]] = []
        candidates: list[dict[str, object]] = []
        periods: set[str] = set()
        held_reasons: list[str] = []
        for upload in uploads:
            document = self.documents.upload_file(principal, borrower_ref=borrower.reference, doc_type="financial_statement", upload=upload, scope=resolved_scope)
            if document.mime_type.lower() != "application/pdf":
                raise ValidationError("Financial statements must be genuine PDF files.", field="files")
            self.documents.extract_document(principal, document.id, scope=resolved_scope)
            pages = tuple(self.session.scalars(select(DocumentPage).where(DocumentPage.document_id == document.id).order_by(DocumentPage.page_number)).all())
            extracted = _extract(document.id, pages)
            documents.append({"id": str(document.id), "filename": document.filename, "ocr_applied": document.ocr_applied})
            if extracted["period_end"] in periods:
                raise Conflict("Each PDF must represent a different quarterly period.")
            periods.add(str(extracted["period_end"]))
            candidates.append(extracted)
            if extracted["state"] != "ready":
                held_reasons.append(str(extracted["reason"]))
        now = self.clock.now()
        state = "ready" if not held_reasons else "held"
        batch = FinancialPdfBatch(id=new_id(), borrower_id=borrower.id, state=state, documents=documents, candidates=candidates, message="; ".join(held_reasons) or None, created_at=now, updated_at=now, created_by_id=principal.id, updated_by_id=principal.id, request_id="financial-pdf-" + new_id().hex[:20], version=1)
        self.session.add(batch)
        self.session.flush()
        self.audit.record(AuditEventType.STATEMENT_IMPORT_COMPLETED.value, ("financial_pdf_batch", batch.id), {"state": state, "documents": len(documents), "held": len(held_reasons)}, actor=principal.id, request_id=batch.request_id)
        return batch

    def get(self, principal: Principal, batch_id: UUID, *, scope: Scope | None = None) -> FinancialPdfBatch:
        authorize(principal, Permission.INGEST_FINANCIAL_STATEMENTS)
        scope = scope or resolve_scope(principal, self.session)
        batch = self.session.get(FinancialPdfBatch, batch_id)
        if batch is None or self.session.scalar(select(Borrower.id).where(Borrower.id == batch.borrower_id, scope.predicate(Borrower.portfolio_id))) is None:
            raise NotFound("Financial statement batch was not found within the current scope.")
        return batch

    def approve(self, principal: Principal, batch_id: UUID, *, scope: Scope | None = None) -> FinancialPdfBatch:
        batch = self.get(principal, batch_id, scope=scope)
        if batch.state != "ready":
            raise Conflict("Held financial statements must be corrected before approval.")
        now = self.clock.now()
        source_batch_id = _pdf_import_batch(self.session, principal, batch, now)
        for candidate in batch.candidates:
            period_end = date.fromisoformat(str(candidate["period_end"]))
            existing = self.session.scalar(select(FinancialPeriod).where(FinancialPeriod.borrower_id == batch.borrower_id, FinancialPeriod.fy_label == candidate["fy_label"], FinancialPeriod.superseded_by_id.is_(None)))
            if existing is not None:
                raise Conflict(f"A live financial period already exists for {candidate['fy_label']}.")
            period = FinancialPeriod(id=new_id(), borrower_id=batch.borrower_id, fy_label=str(candidate["fy_label"]), period_type="quarterly", period_start=period_end - timedelta(days=89), period_end=period_end, is_complete=True, is_audited=False, superseded_by_id=None, source_batch_id=source_batch_id, created_at=now, updated_at=now, created_by_id=principal.id, updated_by_id=principal.id, request_id=batch.request_id, version=1)
            self.session.add(period)
            for line in candidate["lines"]:
                provenance = FieldProvenance(id=new_id(), source_type="api", source_reference=f"/documents/{line['document_id']}/view?page={line['page_number']}&start={line['start']}&end={line['end']}", row_reference=str(line["code"]), mapping_version=1, ingested_at=now, batch_id=source_batch_id, transform_note="deterministic financial PDF extraction", created_at=now, updated_at=now, created_by_id=principal.id, updated_by_id=principal.id, request_id=batch.request_id)
                self.session.add(provenance)
                self.session.add(StatementLineValue(id=new_id(), period_id=period.id, line_code=str(line["code"]), value=Decimal(str(line["value"])), unit="crore", currency="INR", provenance_id=provenance.id, created_at=now, updated_at=now, created_by_id=principal.id, updated_by_id=principal.id, request_id=batch.request_id))
        batch.state, batch.updated_at, batch.updated_by_id, batch.version = "approved", now, principal.id, batch.version + 1
        EngineService(self.session, audit=self.audit, scope_resolver=lambda _principal: scope or resolve_scope(principal, self.session)).queue_retest(
            principal,
            RetestTrigger(kind=RetestTriggerKind.STATEMENT, borrower_id=batch.borrower_id, as_of_date=now.date(), period_label="financial-pdf"),
            scope=scope,
        )
        self.audit.record(AuditEventType.STATEMENT_IMPORT_COMPLETED.value, ("financial_pdf_batch", batch.id), {"state": "approved", "periods": len(batch.candidates)}, actor=principal.id, request_id=batch.request_id)
        return batch


def _pdf_import_batch(session: Session, principal: Principal, review: FinancialPdfBatch, now: object) -> UUID:
    """Create the minimal immutable import envelope required by provenance."""
    mapping = session.scalar(select(ImportMapping).where(ImportMapping.name == "financial-pdf", ImportMapping.version == 1))
    if mapping is None:
        mapping = ImportMapping(id=new_id(), name="financial-pdf", source_type="api", version=1, is_active=True, spec={"borrower_key_column": "borrower", "fy_label_column": "fy", "period_type_column": "period_type", "period_start_column": "start", "period_end_column": "end", "is_audited_column": None, "unit": "crore", "currency": "INR", "sign": "as_reported", "columns": {"revenue": "revenue"}, "totals_row": None}, created_at=now, updated_at=now, created_by_id=principal.id, updated_by_id=principal.id, request_id=review.request_id)
        session.add(mapping)
        session.flush()
    digest = sha256(str(review.id).encode()).hexdigest()
    batch = ImportBatch(id=new_id(), source_type="api", source_reference=f"financial-pdf:{review.id}", mapping_id=mapping.id, content_hash=digest, started_at=now, finished_at=now, row_count=len(review.candidates), accepted_count=len(review.candidates), quarantined_count=0, state="completed", report={"source": "financial_pdf", "review_batch_id": str(review.id)}, created_at=now, updated_at=now, created_by_id=principal.id, updated_by_id=principal.id, request_id=review.request_id)
    session.add(batch)
    session.flush()
    return batch.id


def _extract(document_id: UUID, pages: tuple[DocumentPage, ...]) -> dict[str, object]:
    text = "\n".join(page.text or "" for page in pages)
    match = _DATE.search(text)
    if match is None or any(page.needs_review for page in pages):
        return {"state": "held", "reason": "The quarter or OCR text requires review.", "period_end": str(document_id), "fy_label": str(document_id), "lines": []}
    try:
        period_end = _parse_date(match.group(1))
    except ValueError:
        return {"state": "held", "reason": "The statement quarter could not be read.", "period_end": str(document_id), "fy_label": str(document_id), "lines": []}
    lines: list[dict[str, object]] = []
    for code, labels in _ALIASES.items():
        found = _line_value(document_id, pages, labels)
        if found is not None:
            lines.append({"code": code, **found})
    normal = default_chart().normalise({line["code"]: Decimal(str(line["value"])) for line in lines})
    required = {"revenue", "ebitda", "total_debt", "tangible_net_worth"}
    if not required <= {line["code"] for line in lines} or normal.flags or normal.failing_identities:
        return {"state": "held", "reason": "Required values are missing or could not be reconciled.", "period_end": period_end.isoformat(), "fy_label": _fy_label(period_end), "lines": lines}
    return {"state": "ready", "reason": "", "period_end": period_end.isoformat(), "fy_label": _fy_label(period_end), "lines": lines}


def _line_value(document_id: UUID, pages: tuple[DocumentPage, ...], labels: tuple[str, ...]) -> dict[str, object] | None:
    for page in pages:
        text = page.text or ""
        for label in labels:
            match = re.search(re.escape(label) + r"[^\n]{0,80}", text, re.I)
            if match:
                values = _NUMBER.findall(match.group(0))
                if values:
                    raw = values[-1].replace(",", "").strip("()")
                    try:
                        return {"value": str(Decimal(raw)), "document_id": str(document_id), "page_number": page.page_number, "start": match.start(), "end": match.end(), "confidence": "1.00" if page.ocr_confidence is None else str(page.ocr_confidence)}
                    except InvalidOperation:
                        continue
    return None


def _parse_date(value: str) -> date:
    for form in ("%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d/%b/%Y", "%Y-%m-%d"):
        try:
            return date.fromisoformat(value) if form == "%Y-%m-%d" else __import__("datetime").datetime.strptime(value, form).date()
        except ValueError:
            pass
    raise ValueError(value)


def _fy_label(value: date) -> str:
    quarter = ((value.month - 1) // 3) + 1
    return f"FY{value.year}-Q{quarter}"
