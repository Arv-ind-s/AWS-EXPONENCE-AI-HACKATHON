"""Presentation models for the covenant-intake review screen.

The intake page is deliberately shaped before it reaches Jinja.  Templates
may arrange values, but they must not decide whether a proposal passed, which
check failed, or whether a confirmation control is safe to render.  Those
decisions remain owned by ``IntakeService`` and the persisted verification
outcome from T-096.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from covenant_radar.ai.shapes import Stage1VerificationOutcome
from covenant_radar.db.models.document import Document, DocumentSpan
from covenant_radar.db.models.intake import CovenantProposal
from covenant_radar.services.intake import ProposalRecord


@dataclass(frozen=True, slots=True)
class IntakeCheckView:
    """One named code verdict and its human-readable explanation."""

    code: str
    label: str
    state: str
    detail: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.label.strip() or not self.detail.strip():
            raise ValueError("An intake check requires code, label and detail text.")
        if self.state not in {"passed", "struck", "pending"}:
            raise ValueError(f"Unsupported intake check state: {self.state!r}.")


@dataclass(frozen=True, slots=True)
class IntakeFieldView:
    """One proposed field shown in the side-by-side comparison."""

    name: str
    label: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.label.strip() or not self.value.strip():
            raise ValueError("An intake field requires non-empty name, label and value.")


@dataclass(frozen=True, slots=True)
class IntakeSourceView:
    """The source quote and optional addressable document-span destination."""

    quote: str
    document_id: UUID | None
    filename: str | None
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    href: str
    is_hand_entry: bool

    def __post_init__(self) -> None:
        if not self.quote.strip():
            raise ValueError("An intake source requires a non-empty quote.")
        if self.is_hand_entry and self.document_id is not None:
            raise ValueError("A hand-entered source cannot carry a document id.")
        offsets = (self.page_number, self.start_offset, self.end_offset)
        if any(value is not None for value in offsets) and not all(
            value is not None for value in offsets
        ):
            raise ValueError("Document source coordinates must be complete or absent.")


@dataclass(frozen=True, slots=True)
class IntakeDocumentView:
    """Safe document upload/extraction status for the intake screen."""

    document_id: UUID
    filename: str
    doc_type: str
    extraction_state: str
    ocr_applied: bool
    page_count: int | None
    review_page_count: int
    progress_value: int
    status_label: str
    ocr_label: str

    def __post_init__(self) -> None:
        if not self.filename.strip() or not self.doc_type.strip():
            raise ValueError("An intake document requires filename and document type.")
        if self.extraction_state not in {"pending", "in_progress", "complete", "failed"}:
            raise ValueError(f"Unsupported extraction state: {self.extraction_state!r}.")
        if not 0 <= self.progress_value <= 100:
            raise ValueError("Document progress must be between 0 and 100.")
        if self.review_page_count < 0:
            raise ValueError("Document review page count cannot be negative.")


@dataclass(frozen=True, slots=True)
class IntakePendingReviewView:
    """A document page held out of automated detection pending review."""

    page_number: int
    reason: str
    href: str

    def __post_init__(self) -> None:
        if self.page_number < 1 or not self.reason.strip() or not self.href.startswith("/"):
            raise ValueError("An OCR review item must have a valid page, reason and link.")


@dataclass(frozen=True, slots=True)
class IntakeProposalView:
    """One proposal comparison card, including its complete verdict set."""

    proposal_id: UUID
    status: str
    source: IntakeSourceView
    fields: tuple[IntakeFieldView, ...]
    form: Mapping[str, str]
    checks: tuple[IntakeCheckView, ...]
    all_passed: bool
    struck: bool
    confirmable: bool
    bulk_included: bool
    failed_check_labels: tuple[str, ...]
    refusal_message: str | None

    def __post_init__(self) -> None:
        if self.status not in {"open", "confirmed", "abandoned"}:
            raise ValueError(f"Unsupported proposal status: {self.status!r}.")
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "failed_check_labels", tuple(self.failed_check_labels))
        object.__setattr__(self, "form", MappingProxyType(dict(self.form)))
        if self.confirmable and (not self.all_passed or self.status != "open"):
            raise ValueError("Only an open, fully verified proposal can be confirmable.")
        if self.struck != (not self.all_passed):
            raise ValueError("A proposal's struck state must mirror its verification outcome.")


@dataclass(frozen=True, slots=True)
class IntakeScreenView:
    """Complete intake screen state for a single request."""

    document: IntakeDocumentView | None
    proposals: tuple[IntakeProposalView, ...]
    pending_reviews: tuple[IntakePendingReviewView, ...]
    form: Mapping[str, str]
    error: str
    status_message: str
    provider_unavailable: bool
    hand_entry: bool
    can_upload: bool
    can_run_intake: bool
    bulk_proposal_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposals", tuple(self.proposals))
        object.__setattr__(self, "pending_reviews", tuple(self.pending_reviews))
        object.__setattr__(self, "form", MappingProxyType(dict(self.form)))
        object.__setattr__(self, "bulk_proposal_ids", tuple(self.bulk_proposal_ids))
        if not isinstance(self.error, str) or not isinstance(self.status_message, str):
            raise TypeError("Intake screen messages must be strings.")


def build_document_view(
    document: Document,
    *,
    review_page_count: int,
    labels: Mapping[str, str],
) -> IntakeDocumentView:
    """Shape one stored document without exposing storage keys or bytes."""

    progress = {
        "pending": 25,
        "in_progress": 60,
        "complete": 100,
        "failed": 100,
    }.get(document.extraction_state, 0)
    status_key = f"extraction_{document.extraction_state}"
    status_label = labels.get(status_key, document.extraction_state.replace("_", " ").title())
    ocr_label = labels["ocr_complete"] if document.ocr_applied else labels["ocr_not_run"]
    return IntakeDocumentView(
        document_id=document.id,
        filename=document.filename,
        doc_type=document.doc_type,
        extraction_state=document.extraction_state,
        ocr_applied=document.ocr_applied,
        page_count=document.page_count,
        review_page_count=review_page_count,
        progress_value=progress,
        status_label=status_label,
        ocr_label=ocr_label,
    )


def build_proposal_view(
    record: ProposalRecord,
    *,
    source_span: DocumentSpan | None,
    document: Document | None,
    labels: Mapping[str, str],
    can_confirm: bool,
) -> IntakeProposalView:
    """Shape a persisted proposal and its stored outcome for review."""

    row = record.row
    outcome = record.outcome
    source = _source_view(row, source_span=source_span, document=document, labels=labels)
    checks = _check_views(outcome, labels)
    failed_labels = tuple(check.label for check in checks if check.state == "struck")
    return IntakeProposalView(
        proposal_id=row.id,
        status=row.status,
        source=source,
        fields=_field_views(row, labels),
        form=_proposal_form(row),
        checks=checks,
        all_passed=outcome.all_passed,
        struck=not outcome.all_passed,
        confirmable=outcome.all_passed and row.status == "open" and can_confirm,
        bulk_included=outcome.all_passed and row.status == "open" and can_confirm,
        failed_check_labels=failed_labels,
        refusal_message=(outcome.refusal_message if outcome.injection_detected else None),
    )


def _source_view(
    row: CovenantProposal,
    *,
    source_span: DocumentSpan | None,
    document: Document | None,
    labels: Mapping[str, str],
) -> IntakeSourceView:
    quote = (row.source_quote or row.clause_text).strip()
    if source_span is None or document is None:
        return IntakeSourceView(
            quote=quote,
            document_id=None,
            filename=None,
            page_number=None,
            start_offset=None,
            end_offset=None,
            href="",
            is_hand_entry=row.document_id is None,
        )
    href = (
        f"/documents/{document.id}/view?page={source_span.page_number}"
        f"&start={source_span.start_offset}&end={source_span.end_offset}"
    )
    return IntakeSourceView(
        quote=quote,
        document_id=document.id,
        filename=document.filename,
        page_number=source_span.page_number,
        start_offset=source_span.start_offset,
        end_offset=source_span.end_offset,
        href=href,
        is_hand_entry=False,
    )


def _check_views(
    outcome: Stage1VerificationOutcome,
    labels: Mapping[str, str],
) -> tuple[IntakeCheckView, ...]:
    checks = tuple(
        IntakeCheckView(
            code=check.check.value,
            label=labels.get(check.check.value, check.check.value.replace("_", " ").title()),
            state="passed" if check.passed else "struck",
            detail=check.detail,
        )
        for check in outcome.verification.checks
    )
    if not outcome.injection_detected:
        return checks
    return checks + (
        IntakeCheckView(
            code="input_safety",
            label=labels["input_safety"],
            state="struck",
            detail=outcome.refusal_message or labels["input_safety_failed"],
        ),
    )


def _field_views(row: CovenantProposal, labels: Mapping[str, str]) -> tuple[IntakeFieldView, ...]:
    values = _proposal_form(row)
    names = (
        ("definition", labels["definition"]),
        ("custom_formula", labels["custom_formula"]),
        ("threshold", labels["threshold"]),
        ("direction", labels["direction"]),
        ("unit", labels["unit"]),
        ("currency", labels["currency"]),
        ("frequency", labels["frequency"]),
        ("effective_from", labels["effective_from"]),
        ("effective_to", labels["effective_to"]),
        ("exceptions", labels["exceptions"]),
        ("cure_period_days", labels["cure_period_days"]),
    )
    return tuple(
        IntakeFieldView(name=name, label=label, value=values[name] or labels["not_supplied"])
        for name, label in names
    )


def _proposal_form(row: CovenantProposal) -> dict[str, str]:
    direction = {"min": "above", "max": "below"}.get(row.direction or "", row.direction or "")
    return {
        "clause_text": row.clause_text,
        "definition": row.definition_ref or "",
        "custom_formula": row.custom_formula or "",
        "threshold": str(row.threshold) if row.threshold is not None else "",
        "direction": direction,
        "unit": row.unit or "",
        "currency": row.currency or "",
        "frequency": row.frequency or "",
        "effective_from": row.effective_from.isoformat() if row.effective_from else "",
        "effective_to": row.effective_to.isoformat() if row.effective_to else "",
        "exceptions": "\n".join(row.exceptions),
        "cure_period_days": (str(row.cure_period_days) if row.cure_period_days is not None else ""),
        "source_quote": row.source_quote or row.clause_text,
        "test_basis": "standalone",
        "reference": "",
        "name": "",
        "covenant_class": "financial",
    }


__all__ = [
    "IntakeCheckView",
    "IntakeDocumentView",
    "IntakeFieldView",
    "IntakePendingReviewView",
    "IntakeProposalView",
    "IntakeScreenView",
    "IntakeSourceView",
    "build_document_view",
    "build_proposal_view",
]
