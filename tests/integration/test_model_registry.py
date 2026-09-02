"""Integration coverage for T-107's model registry and approval path."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.ai.registry import (
    DEVELOPMENT,
    KNOWN_MODEL_COMPONENTS,
    PRODUCTION,
    ComponentNotRegistered,
    ModelCardMissingError,
    ModelRegistrationRecord,
    ModelRegistryGuard,
    check_model_cards,
)
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import AuthorizationError, Conflict
from covenant_radar.db.base import Base
from covenant_radar.db.models.identity import AppUser
from covenant_radar.security.maker_checker import MakerCheckerState
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.model_governance import ModelGovernanceService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Audit:
    """Fake `AuditWriter` recording every call, the same shape used by
    `tests/integration/test_registry_service.py`."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object, str]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, dict(payload), actor, request_id))
        return object()


class _FakeRepository:
    """A two-line fake satisfying the guard's minimal `ModelRegistryRepository`."""

    def __init__(self, record: ModelRegistrationRecord | None) -> None:
        self._record = record

    def get_by_component(self, component: str) -> ModelRegistrationRecord | None:
        return self._record


class _World:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.registrar = Principal.user(uuid4(), (Permission.MANAGE_JOBS,))
        self.approver = Principal.user(uuid4(), (Permission.APPROVE_MODEL_PROMOTION,))
        self.auditor = Principal.user(uuid4(), (Permission.VIEW_AUDIT,))
        self._add_user(self.registrar, "t107-registrar")
        self._add_user(self.approver, "t107-approver")
        self._add_user(self.auditor, "t107-auditor")
        self.service = ModelGovernanceService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t107-test",
        )

    def _add_user(self, principal: Principal, username: str) -> None:
        self.session.add(
            AppUser(
                id=principal.id,
                username=username,
                email=f"{username}@example.com",
                full_name=username.title(),
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-user-{username}",
            )
        )
        self.session.flush()

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_unregistered_component_refused_in_production() -> None:
    production_guard = ModelRegistryGuard(_FakeRepository(None), environment=PRODUCTION)
    with pytest.raises(ComponentNotRegistered, match="stage9_unknown"):
        production_guard.ensure_permitted("stage9_unknown")

    # In development the same condition is a loud warning, never a refusal,
    # so the constraint stays real without blocking local work.
    development_guard = ModelRegistryGuard(_FakeRepository(None), environment=DEVELOPMENT)
    assert development_guard.ensure_permitted("stage9_unknown") is None


def test_self_approval_refused() -> None:
    world = _World()
    try:
        # The same physical actor holds both permissions, simulating a
        # registration "approved by its own owner".
        registrar_and_approver = Principal.user(
            world.registrar.id, (Permission.MANAGE_JOBS, Permission.APPROVE_MODEL_PROMOTION)
        )
        registered = world.service.register(
            registrar_and_approver,
            component="stage1_extraction",
            provider="anthropic",
            model_id="claude-test-v1",
            prompt_version="v1",
        )
        assert registered.approval_request is not None

        with pytest.raises(Conflict, match="distinct"):
            world.service.decide_approval(
                registrar_and_approver,
                registered.approval_request.id,
                approved=True,
            )

        # The registration is still exactly where the maker left it: pending.
        current = world.service.get_registration(world.auditor, "stage1_extraction")
        assert current is not None
        assert current.state == "registered"
        assert current.approved_by_id is None
    finally:
        world.close()


def test_prompt_bump_requires_reapproval() -> None:
    world = _World()
    try:
        first = world.service.register(
            world.registrar,
            component="stage7_memo",
            provider="anthropic",
            model_id="claude-test",
            prompt_version="v1",
            owner_id=world.registrar.id,
        )
        assert first.reapproval_required is False
        assert first.approval_request is not None

        approved = world.service.decide_approval(
            world.approver, first.approval_request.id, approved=True
        )
        assert approved.state is MakerCheckerState.APPROVED

        settled = world.service.get_registration(world.auditor, "stage7_memo")
        assert settled is not None
        assert settled.is_approved is True
        assert settled.state == "approved"

        # The approved thing has changed: the same component, a new prompt
        # version. The registration must fall back to unapproved and a
        # fresh, distinct approval must be required before further use.
        bumped = world.service.register(
            world.registrar,
            component="stage7_memo",
            provider="anthropic",
            model_id="claude-test",
            prompt_version="v2",
        )
        assert bumped.reapproval_required is True
        assert bumped.registration.state == "registered"
        assert bumped.registration.approved_by_id is None
        assert bumped.registration.is_approved is False
        assert bumped.approval_request is not None
        assert bumped.approval_request.state is MakerCheckerState.PENDING

        refreshed = world.service.get_registration(world.auditor, "stage7_memo")
        assert refreshed is not None
        assert refreshed.is_approved is False
        assert refreshed.prompt_version == "v2"
    finally:
        world.close()


def test_missing_model_card_fails_build(tmp_path: Path) -> None:
    with pytest.raises(ModelCardMissingError, match="phantom_component"):
        check_model_cards(["phantom_component"], model_card_dir=tmp_path)

    # Every component that is actually wired to a model today ships a card
    # under the real, checked-in register — no override needed to pass.
    check_model_cards(KNOWN_MODEL_COMPONENTS)


def test_auditor_read_only() -> None:
    world = _World()
    try:
        registered = world.service.register(
            world.registrar,
            component="stage1_extraction",
            provider="anthropic",
            model_id="claude-test",
            prompt_version="v1",
        )
        assert registered.approval_request is not None

        listing = world.service.list_registrations(world.auditor)
        assert any(item.component == "stage1_extraction" for item in listing)

        with pytest.raises(AuthorizationError):
            world.service.register(
                world.auditor,
                component="stage7_memo",
                provider="anthropic",
                model_id="claude-test",
            )
        with pytest.raises(AuthorizationError):
            world.service.decide_approval(
                world.auditor, registered.approval_request.id, approved=True
            )
    finally:
        world.close()


def test_registration_audited() -> None:
    world = _World()
    try:
        registered = world.service.register(
            world.registrar,
            component="stage1_extraction",
            provider="anthropic",
            model_id="claude-test",
            prompt_version="v1",
        )
        assert registered.approval_request is not None

        event_types_after_register = [event[0] for event in world.audit.events]
        assert "model_registration_registered" in event_types_after_register
        assert "maker_checker_submitted" in event_types_after_register

        world.service.decide_approval(world.approver, registered.approval_request.id, approved=True)
        event_types_after_approval = [event[0] for event in world.audit.events]
        assert "model_registration_approved" in event_types_after_approval
        assert "maker_checker_approved" in event_types_after_approval

        approval_event = next(
            event for event in world.audit.events if event[0] == "model_registration_approved"
        )
        _, _, payload, actor, request_id = approval_event
        assert payload["component"] == "stage1_extraction"
        assert actor == world.approver.id
        assert request_id == "rq-t107-test"
    finally:
        world.close()
