"""The closed permission vocabulary used by the authorization layer.

The values in this enum are the stable codes stored in ``permission.code``
and in seeded role assignments.  They intentionally mirror the actionable
permissions in ``spec \u00a716.1`` and ``db/seed/data/permissions.json``.  The two
capabilities that the specification forbids structurally are not members:
there is no permission that could accidentally make either operation
reachable through configuration.
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class Permission(str, Enum):
    """A permission that may be declared on a route or granted to a role."""

    VIEW_QUEUE = "VIEW_QUEUE"
    VIEW_BORROWER = "VIEW_BORROWER"
    VIEW_CASE = "VIEW_CASE"
    VIEW_MEMO = "VIEW_MEMO"
    VIEW_COVENANT = "VIEW_COVENANT"
    VIEW_FORECAST = "VIEW_FORECAST"
    VIEW_EVIDENCE = "VIEW_EVIDENCE"
    VIEW_DOCUMENT = "VIEW_DOCUMENT"
    VIEW_AUDIT = "VIEW_AUDIT"
    UPLOAD_DOCUMENT = "UPLOAD_DOCUMENT"
    RUN_INTAKE = "RUN_INTAKE"
    REGISTER_COVENANT = "REGISTER_COVENANT"
    APPROVE_COVENANT = "APPROVE_COVENANT"
    RECORD_WAIVER = "RECORD_WAIVER"
    GENERATE_MEMO = "GENERATE_MEMO"
    RUN_SIMULATION = "RUN_SIMULATION"
    LOG_ACTION = "LOG_ACTION"
    UPDATE_CASE = "UPDATE_CASE"
    RECORD_DISPOSITION = "RECORD_DISPOSITION"
    OVERRIDE_RISK_VIEW = "OVERRIDE_RISK_VIEW"
    PROPOSE_THRESHOLDS = "PROPOSE_THRESHOLDS"
    APPROVE_THRESHOLDS = "APPROVE_THRESHOLDS"
    MANAGE_USERS = "MANAGE_USERS"
    MANAGE_CONNECTORS = "MANAGE_CONNECTORS"
    MANAGE_JOBS = "MANAGE_JOBS"
    ASSUME_PERSONA = "ASSUME_PERSONA"
    APPROVE_MODEL_PROMOTION = "APPROVE_MODEL_PROMOTION"
    RESOLVE_QUARANTINE = "RESOLVE_QUARANTINE"
    CORRECT_SOURCE_DATA = "CORRECT_SOURCE_DATA"
    INGEST_DATA = "INGEST_DATA"
    INGEST_FINANCIAL_STATEMENTS = "INGEST_FINANCIAL_STATEMENTS"
    EXPORT_EVIDENCE = "EXPORT_EVIDENCE"
    READ_PERSONAL_DATA = "READ_PERSONAL_DATA"

    @property
    def code(self) -> str:
        """Return the persistence and API code for this permission."""
        return self.value

    @property
    def description(self) -> str:
        """Return the matrix action described by this permission."""
        return PERMISSION_DESCRIPTIONS[self]


# Keep descriptions in one immutable mapping so seed validation and UI code
# can use the same human-readable text without duplicating enum members.
PERMISSION_DESCRIPTIONS: Final[dict[Permission, str]] = {
    Permission.VIEW_QUEUE: "View the scoped portfolio queue.",
    Permission.VIEW_BORROWER: "View scoped borrower case files.",
    Permission.VIEW_CASE: "View scoped case records.",
    Permission.VIEW_MEMO: "View scoped warning memos.",
    Permission.VIEW_COVENANT: "View scoped covenant records.",
    Permission.VIEW_FORECAST: "View stored scoped forecasts.",
    Permission.VIEW_EVIDENCE: "View scoped evidence records.",
    Permission.VIEW_DOCUMENT: "View scoped documents.",
    Permission.VIEW_AUDIT: "View the append-only audit trail.",
    Permission.UPLOAD_DOCUMENT: "Upload a scoped source document.",
    Permission.RUN_INTAKE: "Run covenant intake on scoped source data.",
    Permission.REGISTER_COVENANT: "Register or amend a covenant.",
    Permission.APPROVE_COVENANT: "Approve a verified covenant registration.",
    Permission.RECORD_WAIVER: "Record a covenant waiver.",
    Permission.GENERATE_MEMO: "Generate a grounded warning memo.",
    Permission.RUN_SIMULATION: "Run a scoped intervention simulation.",
    Permission.LOG_ACTION: "Log an action taken on a case.",
    Permission.UPDATE_CASE: "Update a scoped case.",
    Permission.RECORD_DISPOSITION: "Record a case disposition.",
    Permission.OVERRIDE_RISK_VIEW: "Override a risk view with a reason.",
    Permission.PROPOSE_THRESHOLDS: "Propose a threshold change.",
    Permission.APPROVE_THRESHOLDS: "Approve a threshold change.",
    Permission.MANAGE_USERS: "Manage users, roles and assignments.",
    Permission.MANAGE_CONNECTORS: "Manage connector configuration.",
    Permission.MANAGE_JOBS: "Manage and run scheduled jobs.",
    Permission.ASSUME_PERSONA: "Assume a demonstration persona without re-authenticating.",
    Permission.APPROVE_MODEL_PROMOTION: "Approve a model promotion.",
    Permission.RESOLVE_QUARANTINE: "Resolve quarantined source rows.",
    Permission.CORRECT_SOURCE_DATA: "Correct source data with a reason.",
    Permission.INGEST_DATA: "Ingest reconciled source data.",
    Permission.INGEST_FINANCIAL_STATEMENTS: "Upload and approve scoped financial statements.",
    Permission.EXPORT_EVIDENCE: "Export a scoped evidence bundle.",
    Permission.READ_PERSONAL_DATA: "Read personal-class fields under the access policy.",
}


# These are the permissions represented by the rows of the access matrix.
# Keeping the tuple ordered makes diagnostics and startup output stable.
SPEC_MATRIX_PERMISSIONS: Final[tuple[Permission, ...]] = tuple(Permission)


def coerce_permission(value: Permission | str) -> Permission:
    """Convert a permission code to the closed enum, failing closed.

    Unknown strings are configuration errors.  Silently ignoring one would
    make a misspelled API-key scope or role assignment appear valid while
    producing an incomplete authorization decision.
    """
    if isinstance(value, Permission):
        return value
    if not isinstance(value, str):
        raise TypeError("Permission values must be Permission members or string codes.")
    try:
        return Permission(value)
    except ValueError as error:
        raise ValueError(f"Unknown permission code: {value!r}.") from error


def permission_description(permission: Permission | str) -> str:
    """Return the canonical description for a permission code."""
    return PERMISSION_DESCRIPTIONS[coerce_permission(permission)]


__all__ = [
    "PERMISSION_DESCRIPTIONS",
    "Permission",
    "SPEC_MATRIX_PERMISSIONS",
    "coerce_permission",
    "permission_description",
]
