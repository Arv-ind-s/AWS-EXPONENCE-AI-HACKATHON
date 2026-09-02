"""Unit tests for the T-015 permission vocabulary and role cache."""

from __future__ import annotations

from uuid import UUID

import pytest

from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import (
    RolePermissionResolver,
    check_unreachable_permissions,
)

pytestmark = pytest.mark.unit

_USER_ID = UUID("00000000-0000-7000-8000-000000000015")


def test_enumeration_matches_spec_matrix() -> None:
    expected = {
        "VIEW_QUEUE",
        "VIEW_BORROWER",
        "VIEW_CASE",
        "VIEW_MEMO",
        "VIEW_COVENANT",
        "VIEW_FORECAST",
        "VIEW_EVIDENCE",
        "VIEW_DOCUMENT",
        "VIEW_AUDIT",
        "UPLOAD_DOCUMENT",
        "RUN_INTAKE",
        "REGISTER_COVENANT",
        "APPROVE_COVENANT",
        "RECORD_WAIVER",
        "GENERATE_MEMO",
        "RUN_SIMULATION",
        "LOG_ACTION",
        "UPDATE_CASE",
        "RECORD_DISPOSITION",
        "OVERRIDE_RISK_VIEW",
        "PROPOSE_THRESHOLDS",
        "APPROVE_THRESHOLDS",
        "MANAGE_USERS",
        "MANAGE_CONNECTORS",
        "MANAGE_JOBS",
        "APPROVE_MODEL_PROMOTION",
        "RESOLVE_QUARANTINE",
        "CORRECT_SOURCE_DATA",
        "INGEST_DATA",
        "EXPORT_EVIDENCE",
        "READ_PERSONAL_DATA",
        # Two grants the matrix in `spec.md` predates. Both are real, seeded
        # in `db/seed/data/permissions.json`, and enforced: statement import
        # gates `/financial-statements`, and persona assumption is the demo
        # sign-in path `rbac.py` explicitly refuses to let impersonate itself.
        "INGEST_FINANCIAL_STATEMENTS",
        "ASSUME_PERSONA",
    }

    assert {permission.value for permission in Permission} == expected
    assert "CONFIRM_FAILED_COVENANT" not in expected
    assert "CREDIT_DECISION" not in expected


def test_role_resolution_cached_and_invalidated() -> None:
    calls: list[UUID] = []
    grants = {"VIEW_QUEUE"}

    def lookup(user_id: UUID) -> set[str]:
        calls.append(user_id)
        return grants

    resolver = RolePermissionResolver(lookup)

    assert resolver.permissions_for(_USER_ID) == frozenset({Permission.VIEW_QUEUE})
    assert resolver.permissions_for(_USER_ID) == frozenset({Permission.VIEW_QUEUE})
    assert calls == [_USER_ID]

    grants.add("VIEW_CASE")
    resolver.role_changed(_USER_ID)

    assert resolver.permissions_for(_USER_ID) == frozenset(
        {Permission.VIEW_QUEUE, Permission.VIEW_CASE}
    )
    assert calls == [_USER_ID, _USER_ID]


def test_unreachable_permission_reported() -> None:
    report = check_unreachable_permissions({"relationship_manager": ("VIEW_QUEUE", "VIEW_CASE")})

    assert not report.ok
    assert Permission.VIEW_MEMO in report.unreachable
    assert "VIEW_MEMO" in report.message
