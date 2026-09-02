"""Canonical audit event-type enumeration and the exemption registry it
protects (contract `C-60`, `T-067`).

Every audit event a service emits declares its event type as one of the
members below.  The coverage test in `tests/security/test_audit_coverage.py`
asserts every literal event-type string used at a `self.audit.record(...)`
call site across `services/*` is a member of this enumeration, so a new
event type is a deliberate addition to this file rather than a typo that
only a later reconciliation of the audit trail would notice.

The exemption registry is the other half of the same guarantee: a
state-changing service method, or one that reads personal-class data under
a privileged permission, is required to record an audit event unless its
``(class name, method name)`` pair is listed here with a non-empty reason.
``validate_exemptions`` enforces the "no reason, no exemption" rule at
import time, and is reused directly by the coverage test.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Final


class AuditEventType(str, Enum):
    """Every event type recorded through `covenant_radar.audit.record`."""

    # Authentication (`services/auth.py`).
    AUTHENTICATION_SUCCEEDED = "authentication_succeeded"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHENTICATION_ROLE_CHANGED_SESSIONS_REVOKED = "authentication_role_changed_sessions_revoked"

    # Administration console (`services/admin_users.py`).
    ADMIN_USER_CREATED = "admin_user_created"
    ADMIN_USER_DEACTIVATED = "admin_user_deactivated"
    ADMIN_USER_REACTIVATED = "admin_user_reactivated"
    ADMIN_USER_PASSWORD_RESET = "admin_user_password_reset"
    ADMIN_USER_ROLES_CHANGED = "admin_user_roles_changed"
    ADMIN_USER_SCOPE_CHANGED = "admin_user_scope_changed"
    ADMIN_USER_SESSION_REVOKED = "admin_user_session_revoked"
    ADMIN_USER_SSO_MAPPING_CHANGED = "admin_user_sso_mapping_changed"
    ADMIN_ROLE_ASSIGNMENT_PROPOSED = "admin_role_assignment_proposed"
    ADMIN_ROLE_ASSIGNMENT_APPROVED = "admin_role_assignment_approved"
    ADMIN_ROLE_ASSIGNMENT_REJECTED = "admin_role_assignment_rejected"

    # Maker-checker workflow (`services/approvals.py`).
    MAKER_CHECKER_SUBMITTED = "maker_checker_submitted"
    MAKER_CHECKER_APPROVED = "maker_checker_approved"
    MAKER_CHECKER_REJECTED = "maker_checker_rejected"
    MAKER_CHECKER_EXPIRED = "maker_checker_expired"
    MAKER_CHECKER_DISABLED_APPLIED = "maker_checker_disabled_applied"
    MAKER_CHECKER_EXPIRE_BATCH_COMPLETED = "maker_checker_expire_batch_completed"

    # Master data (`services/master_data.py`).
    MASTER_DATA_BORROWER_CREATED = "master_data_borrower_created"
    MASTER_DATA_BORROWER_UPDATED = "master_data_borrower_updated"
    MASTER_DATA_BORROWER_DEACTIVATED = "master_data_borrower_deactivated"
    MASTER_DATA_REFERENCE_IDENTITIES_UPGRADED = "master_data_reference_identities_upgraded"
    MASTER_DATA_FACILITY_CREATED = "master_data_facility_created"
    MASTER_DATA_FACILITY_LIMIT_CHANGED = "master_data_facility_limit_changed"
    MASTER_DATA_FACILITY_UPDATED = "master_data_facility_updated"
    MASTER_DATA_FACILITY_DEACTIVATED = "master_data_facility_deactivated"
    MASTER_DATA_PORTFOLIO_CREATED = "master_data_portfolio_created"
    MASTER_DATA_PORTFOLIO_UPDATED = "master_data_portfolio_updated"
    MASTER_DATA_PERSONAL_DATA_ACCESSED = "master_data_personal_data_accessed"

    # Covenant registry (`services/registry.py`).
    COVENANT_REGISTERED = "covenant_registered"
    COVENANT_AMENDED = "covenant_amended"
    COVENANT_EXCEPTION_REGISTERED = "covenant_exception_registered"
    COVENANT_WAIVER_REQUESTED = "covenant_waiver_requested"
    COVENANT_WAIVER_APPROVED = "covenant_waiver_approved"
    COVENANT_WAIVER_REJECTED = "covenant_waiver_rejected"
    COVENANT_REGISTRATION_APPROVED = "covenant_registration_approved"
    COVENANT_AMENDMENT_APPROVED = "covenant_amendment_approved"
    COVENANT_RETIRED = "covenant_retired"

    # Deterministic covenant engine (`services/engine.py`).
    COVENANT_TESTED = "covenant_tested"
    COVENANT_RETEST_QUEUED = "covenant_retest_queued"

    # Documents (`services/documents.py`).
    DOCUMENT_UPLOAD_QUARANTINED = "document_upload_quarantined"
    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_PAGE_CORRECTED = "document_page_corrected"
    DOCUMENT_CLASSIFICATION_OVERRIDDEN = "document_classification_overridden"
    DOCUMENT_NATIVE_EXTRACTED = "document_native_extracted"
    DOCUMENT_OCR_PROCESSED = "document_ocr_processed"
    DOCUMENT_NATIVE_EXTRACTION_FAILED = "document_native_extraction_failed"

    # Intake (`services/intake.py`, plus the injection-scan security event
    # `ai/shapes.py` attaches to a failed proposal).
    INTAKE_PROPOSAL_CREATED = "intake_proposal_created"
    INTAKE_PROPOSAL_CORRECTED = "intake_proposal_corrected"
    INTAKE_PROPOSAL_ABANDONED = "intake_proposal_abandoned"
    INTAKE_PROPOSAL_CONFIRMED = "intake_proposal_confirmed"
    INTAKE_INJECTION_ATTEMPT = "intake.injection_attempt"

    # Signal ingestion (`services/ingestion.py`).
    SIGNAL_EVENT_QUARANTINED = "signal_event_quarantined"
    SIGNAL_INGESTION_COMPLETED = "signal_ingestion_completed"

    # Statement import (`services/statements.py`, `T-025`).
    STATEMENT_ROW_QUARANTINED = "statement_row_quarantined"
    STATEMENT_IMPORT_COMPLETED = "statement_import_completed"

    # Statement restatement and quarantine resolution (`services/statements.py`, `T-026`).
    STATEMENT_PERIOD_RESTATED = "statement_period_restated"
    STATEMENT_QUARANTINE_ROW_CORRECTED = "statement_quarantine_row_corrected"
    STATEMENT_QUARANTINE_ROW_REJECTED = "statement_quarantine_row_rejected"

    # Evidence ledger (`services/ledger.py`).
    EVIDENCE_LEDGER_REVISED = "evidence_ledger_revised"
    EVIDENCE_SUPERSEDED = "evidence_superseded"

    # Forecast scoring (`services/scoring.py`).
    FORECAST_CANDIDATE_SCORED = "forecast_candidate_scored"
    FORECAST_RUN_SCORED = "forecast_run_scored"

    # Portfolio triage (`services/triage.py`).
    TRIAGE_WHAT_CHANGED_RECORDED = "triage_what_changed_recorded"
    TRIAGE_COMPARISON_PERSISTED = "triage_comparison_persisted"

    # Case management (`services/cases.py`, `T-109`).
    CASE_LIFECYCLE_CHANGED = "case_lifecycle_changed"

    # Grounded memo generation (`services/memo.py`, `T-101`).
    MEMO_GENERATED = "memo_generated"
    MEMO_REFUSED = "memo_refused"

    # Human risk-view revision (`services/overrides.py`, `T-111`).
    OVERRIDE_RECORDED = "override_recorded"

    # Evidence bundle export (`services/reconstruction.py`, `T-069`).
    EVIDENCE_BUNDLE_EXPORTED = "evidence_bundle_exported"

    # Compliance certificate workflow (`services/certificates.py`, `T-038`, `T-039`).
    CERTIFICATE_REQUEST_RAISED = "certificate_request_raised"
    CERTIFICATE_REQUEST_CANCELLED = "certificate_request_cancelled"
    CERTIFICATE_REQUEST_OVERDUE = "certificate_request_overdue"
    CERTIFICATE_RECEIVED = "certificate_received"
    CERTIFICATE_ACCEPTED = "certificate_accepted"
    CERTIFICATE_REJECTED = "certificate_rejected"
    CERTIFICATE_OVERDUE_EVIDENCE_CREATED = "certificate_overdue_evidence_created"

    # Model registry and approval path (`services/model_governance.py`, `T-107`).
    MODEL_REGISTRATION_REGISTERED = "model_registration_registered"
    MODEL_REGISTRATION_REAPPROVAL_REQUIRED = "model_registration_reapproval_required"
    MODEL_REGISTRATION_APPROVED = "model_registration_approved"

    # In-app notification centre (`T-119`).
    NOTIFICATION_MARKED_READ = "notification_marked_read"
    NOTIFICATIONS_MARKED_READ = "notifications_marked_read"

    # Regulatory reporting: CRILC export (`services/reporting.py`, `T-132`).
    CRILC_REPORT_GENERATED = "crilc_report_generated"

    # Regulatory reporting: EWS/RFA pack export (`reporting/rfa_pack.py`, `T-133`).
    RFA_PACK_EXPORTED = "rfa_pack_exported"

    # Board MIS generation and scheduled delivery (`reporting/mis.py`, `T-134`).
    MIS_REPORT_GENERATED = "mis_report_generated"
    MIS_REPORT_DELIVERY_FAILED = "mis_report_delivery_failed"


ALL_EVENT_TYPES: Final[frozenset[str]] = frozenset(member.value for member in AuditEventType)


#: `(class name, method name)` -> reason. Keyed on `type(obj).__name__` and
#: the *underlying* function's `__name__` (so an alias assignment such as
#: ``authenticate = sign_in`` is the same entry as ``sign_in``), not on
#: whatever attribute name a caller happens to use.
AuditExemptions = Mapping[tuple[str, str], str]


def validate_exemptions(exemptions: AuditExemptions) -> None:
    """Refuse an exemption registry carrying an entry with no reason.

    `T-067`'s "Every case" is explicit: an exemption with no reason is
    refused outright, because an unreasoned exemption is not a documented
    decision, it is a method nobody looked at.
    """

    for (class_name, method_name), reason in exemptions.items():
        if not isinstance(class_name, str) or not class_name.strip():
            raise ValueError("Audit exemption keys require a non-empty class name.")
        if not isinstance(method_name, str) or not method_name.strip():
            raise ValueError("Audit exemption keys require a non-empty method name.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"Audit exemption for {class_name}.{method_name} requires a non-empty reason."
            )


# No production service method needs an exemption as of `T-067`: every
# state-changing method, and the one privileged personal-data read
# (`MasterDataService.reveal_identity`), reaches an audit call either
# directly or through a same-class private helper — see
# `tests/security/test_audit_coverage.py`. The registry is still a real,
# validated mechanism rather than empty scaffolding: a future method that
# genuinely cannot audit itself (a pure delegation to an already-audited
# collaborator, say) is added here with its reason, not left uncovered.
AUDIT_EXEMPTIONS: Final[AuditExemptions] = {}

validate_exemptions(AUDIT_EXEMPTIONS)


__all__ = [
    "ALL_EVENT_TYPES",
    "AUDIT_EXEMPTIONS",
    "AuditEventType",
    "AuditExemptions",
    "validate_exemptions",
]
