"""Audit trail boundary and chain-integrity primitives."""

from covenant_radar.audit.bundle import (
    BundleDocument,
    BundleVerification,
    EvidenceBundle,
    EvidenceBundleError,
    EvidenceBundleVerifier,
    build_bundle,
    verify_bundle,
)
from covenant_radar.audit.chain import (
    AuditChainBreak,
    AuditPayloadError,
    PersonalDataRefused,
    PersonalReference,
    PersonalValue,
    canonical_payload,
    compute_event_hash,
    verify_chain,
)
from covenant_radar.audit.events import (
    ALL_EVENT_TYPES,
    AUDIT_EXEMPTIONS,
    AuditEventType,
    AuditExemptions,
    validate_exemptions,
)
from covenant_radar.audit.record import (
    AuditRecord,
    AuditRecorder,
    AuditSubject,
    AuditWriter,
    record,
)
from covenant_radar.audit.store import AuditStore, InMemoryAuditEvent, InMemoryAuditStore

__all__ = [
    "ALL_EVENT_TYPES",
    "AUDIT_EXEMPTIONS",
    "AuditChainBreak",
    "AuditEventType",
    "AuditExemptions",
    "AuditPayloadError",
    "AuditRecord",
    "AuditRecorder",
    "AuditStore",
    "AuditSubject",
    "AuditWriter",
    "InMemoryAuditEvent",
    "InMemoryAuditStore",
    "PersonalDataRefused",
    "PersonalReference",
    "PersonalValue",
    "BundleDocument",
    "BundleVerification",
    "EvidenceBundle",
    "EvidenceBundleError",
    "EvidenceBundleVerifier",
    "build_bundle",
    "canonical_payload",
    "compute_event_hash",
    "record",
    "validate_exemptions",
    "verify_chain",
    "verify_bundle",
]
