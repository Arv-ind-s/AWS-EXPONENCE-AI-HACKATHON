"""Unit tests for maker-checker policy and lifecycle rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.security.maker_checker import (
    ApplicationCallbackRegistry,
    MakerCheckerRequest,
    MakerCheckerSettings,
    MakerCheckerState,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.approvals import ApprovalService

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class _Repository:
    def __init__(self) -> None:
        self.requests: dict[UUID, MakerCheckerRequest] = {}

    def create(self, request: MakerCheckerRequest) -> MakerCheckerRequest:
        self.requests[request.id] = request
        return request

    def get_for_update(self, request_id: UUID) -> MakerCheckerRequest | None:
        return self.requests.get(request_id)

    def list_pending(self) -> Sequence[MakerCheckerRequest]:
        return tuple(
            request
            for request in self.requests.values()
            if request.state is MakerCheckerState.PENDING
        )

    def decide(
        self,
        request_id: UUID,
        *,
        checker_id: UUID,
        state: MakerCheckerState,
        decided_at: datetime,
        reason: str | None,
        expected_version: int,
    ) -> MakerCheckerRequest:
        request = self.requests[request_id]
        if request.version != expected_version or request.state is not MakerCheckerState.PENDING:
            raise Conflict("The request changed before it could be decided.")
        updated = replace(
            request,
            checker_id=checker_id,
            state=state,
            decided_at=decided_at,
            reason=reason,
            version=request.version + 1,
        )
        self.requests[request_id] = updated
        return updated

    def expire(
        self,
        request_id: UUID,
        *,
        expired_at: datetime,
        expected_version: int,
    ) -> MakerCheckerRequest:
        request = self.requests[request_id]
        if request.version != expected_version or request.state is not MakerCheckerState.PENDING:
            raise Conflict("The request changed before it could expire.")
        updated = replace(
            request,
            state=MakerCheckerState.EXPIRED,
            decided_at=expired_at,
            reason="Approval window elapsed.",
            version=request.version + 1,
        )
        self.requests[request_id] = updated
        return updated


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, dict(payload), actor))
        return object()


class _Notifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def notify(self, event_type: str, payload: Mapping[str, object]) -> object:
        self.events.append((event_type, dict(payload)))
        return object()


def _actors() -> tuple[Principal, Principal]:
    maker_id = new_id()
    checker_id = new_id()
    maker = Principal.user(maker_id, (Permission.REGISTER_COVENANT,))
    checker = Principal.user(checker_id, (Permission.APPROVE_COVENANT,))
    return maker, checker


def _service(
    *,
    clock: FixedClock | None = None,
    settings: MakerCheckerSettings | None = None,
    notifier: _Notifier | None = None,
) -> tuple[
    ApprovalService,
    _Repository,
    _Audit,
    ApplicationCallbackRegistry,
    list[MakerCheckerRequest],
]:
    repository = _Repository()
    audit = _Audit()
    applied: list[MakerCheckerRequest] = []
    registry = ApplicationCallbackRegistry()
    registry.register(
        "covenant_registration",
        lambda request, _actor: applied.append(request),
        propose_permission=Permission.REGISTER_COVENANT,
        approve_permission=Permission.APPROVE_COVENANT,
    )
    service = ApprovalService(
        repository,
        audit,
        registry=registry,
        clock=clock or FixedClock(_NOW),
        settings=settings,
        notifier=notifier or _Notifier(),
        request_id="rq-unit-maker-checker",
    )
    return service, repository, audit, registry, applied


def _submit(service: ApprovalService, maker: Principal) -> MakerCheckerRequest:
    return service.submit(
        "covenant_registration",
        ("covenant", new_id()),
        {"definition": "net_debt_to_ebitda", "threshold": "3.0"},
        maker,
    )


def test_maker_cannot_check() -> None:
    service, repository, _audit, _registry, _applied = _service()
    maker, _checker = _actors()
    maker_with_both_permissions = Principal.user(
        maker.id,
        (Permission.REGISTER_COVENANT, Permission.APPROVE_COVENANT),
    )
    request = _submit(service, maker_with_both_permissions)

    with pytest.raises(Conflict, match="distinct-actor rule"):
        service.decide(request.id, maker_with_both_permissions, True)

    assert repository.requests[request.id].state is MakerCheckerState.PENDING


def test_double_decision_refused() -> None:
    service, repository, audit, _registry, applied = _service()
    maker, checker = _actors()
    request = _submit(service, maker)

    decided = service.decide(request.id, checker, True)

    with pytest.raises(Conflict, match="already decided"):
        service.decide(request.id, checker, False, "No longer required")

    assert decided.state is MakerCheckerState.APPROVED
    assert repository.requests[request.id].state is MakerCheckerState.APPROVED
    assert len(applied) == 1
    assert [event[0] for event in audit.events] == [
        "maker_checker_submitted",
        "maker_checker_approved",
    ]


def test_rejection_requires_reason() -> None:
    service, repository, audit, _registry, applied = _service()
    maker, checker = _actors()
    request = _submit(service, maker)

    with pytest.raises(ValidationError, match="reason is required"):
        service.decide(request.id, checker, False)

    assert repository.requests[request.id].state is MakerCheckerState.PENDING
    assert applied == []
    assert [event[0] for event in audit.events] == ["maker_checker_submitted"]


def test_expiry_blocks_approval() -> None:
    clock = FixedClock(_NOW)
    notifier = _Notifier()
    service, repository, audit, _registry, applied = _service(
        clock=clock,
        settings=MakerCheckerSettings(expiry_window=timedelta(hours=1)),
        notifier=notifier,
    )
    maker, checker = _actors()
    request = _submit(service, maker)
    clock.advance(timedelta(hours=1))

    with pytest.raises(Conflict, match="expired"):
        service.decide(request.id, checker, True)

    assert repository.requests[request.id].state is MakerCheckerState.EXPIRED
    assert applied == []
    assert [event[0] for event in audit.events] == [
        "maker_checker_submitted",
        "maker_checker_expired",
    ]
    assert notifier.events[0][0] == "maker_checker_request_expired"
