"""Security coverage for T-113's self-escalation boundary."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from covenant_radar.core.errors import Conflict
from covenant_radar.db.models import MakerCheckerRequest
from tests.integration.test_admin_users import _principal

pytest_plugins = ("tests.integration.test_admin_users",)
pytestmark = pytest.mark.security


def test_self_grant_requires_distinct_approver(fixture) -> None:
    _session, service, _audit, admin, checker, _target, _admin_role, _analyst_role = (
        fixture
    )
    proposed = service.assign_roles(
        _principal(admin),
        admin.id,
        ("administrator", "credit"),
        reason="Temporary credit coverage",
    )

    assert proposed.pending
    assert proposed.request_id is not None
    assert service.get_user(_principal(admin), admin.id).role_codes == ("administrator",)
    with pytest.raises(Conflict, match="distinct administrator"):
        service.approve_role_assignment(_principal(admin), proposed.request_id)

    approved = service.approve_role_assignment(_principal(checker), proposed.request_id)
    assert approved.applied
    assert service.get_user(_principal(checker), admin.id).role_codes == (
        "administrator",
        "credit",
    )


def test_no_route_bypasses_the_guard(fixture) -> None:
    session, service, _audit, admin, _checker, _target, _admin_role, _analyst_role = (
        fixture
    )
    proposed = service.assign_role(
        _principal(admin),
        admin.id,
        "credit",
        reason="Attempted direct escalation",
    )

    pending = session.scalar(
        select(MakerCheckerRequest).where(MakerCheckerRequest.id == proposed.request_id)
    )
    assert proposed.pending
    assert pending is not None
    assert pending.state == "pending"
