"""Use cases for the shared maker-checker workflow.

The service owns authorization, lifecycle transitions, callback invocation,
audit emission, and expiry.  Repository methods participate in the caller's
transaction and are expected to provide row locking or an equivalent
optimistic-concurrency check; this service never commits independently.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Final
from uuid import UUID

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.security.maker_checker import (
    ApplicationCallbackRegistry,
    ApprovalNotifier,
    AuditWriter,
    MakerCheckerRepository,
    MakerCheckerRequest,
    MakerCheckerSettings,
    MakerCheckerState,
    actor_id,
    subject_ref,
    utc_now,
    validate_reason,
)
from covenant_radar.security.permissions import Permission, coerce_permission
from covenant_radar.security.rbac import Principal, authorize

NotificationSink = ApprovalNotifier | Callable[[str, Mapping[str, object]], object]

_SUBMITTED_EVENT: Final[str] = AuditEventType.MAKER_CHECKER_SUBMITTED.value
_APPROVED_EVENT: Final[str] = AuditEventType.MAKER_CHECKER_APPROVED.value
_REJECTED_EVENT: Final[str] = AuditEventType.MAKER_CHECKER_REJECTED.value
_EXPIRED_EVENT: Final[str] = AuditEventType.MAKER_CHECKER_EXPIRED.value
_DISABLED_EVENT: Final[str] = AuditEventType.MAKER_CHECKER_DISABLED_APPLIED.value
_EXPIRE_BATCH_EVENT: Final[str] = AuditEventType.MAKER_CHECKER_EXPIRE_BATCH_COMPLETED.value
_EXPIRY_NOTIFICATION: Final[str] = "maker_checker_request_expired"


class ApprovalService:
    """Coordinate proposal, approval, rejection, and expiry transitions."""

    def __init__(
        self,
        repository: MakerCheckerRepository,
        audit: AuditWriter,
        *,
        registry: ApplicationCallbackRegistry,
        clock: Clock | None = None,
        settings: MakerCheckerSettings | None = None,
        notifier: NotificationSink,
        request_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.registry = registry
        self.clock = clock or SystemClock()
        self.settings = settings or MakerCheckerSettings()
        self.notifier = notifier
        self.request_id = request_id or get_request_id() or new_request_id()

    def submit(
        self,
        operation: str,
        subject: object,
        payload: Mapping[str, object],
        maker: object,
    ) -> MakerCheckerRequest:
        """Submit a proposal or apply it directly when the control is off."""
        definition = self.registry.get(operation)
        maker_id = actor_id(maker, field_name="maker")
        _require_permission(maker, definition.propose_permission)
        normalized_subject = subject_ref(subject)
        now = utc_now(self.clock)
        request = MakerCheckerRequest(
            id=_new_request_id(),
            subject_type=normalized_subject.subject_type,
            subject_id=normalized_subject.subject_id,
            operation=definition.name,
            payload=payload,
            maker_id=maker_id,
            checker_id=None,
            state=MakerCheckerState.PENDING,
            created_at=now,
        )

        if not self.settings.is_enabled(definition.name):
            direct_request = replace(
                request,
                state=MakerCheckerState.APPROVED,
                decided_at=now,
            )
            definition.callback(direct_request, maker_id)
            self._audit(
                _DISABLED_EVENT,
                direct_request,
                actor=maker,
                payload=self._event_payload(
                    direct_request,
                    checker_required=False,
                    no_second_actor_required=True,
                    applied=True,
                ),
            )
            return direct_request

        persisted = self.repository.create(request)
        if persisted.state is not MakerCheckerState.PENDING:
            raise Conflict(
                f"Maker-checker repository returned request {persisted.id} in state "
                f"{persisted.state.value}; a submitted request must be pending."
            )
        self._audit(
            _SUBMITTED_EVENT,
            persisted,
            actor=maker,
            payload=self._event_payload(persisted, checker_required=True),
        )
        return persisted

    def decide(
        self,
        request_id: UUID | str,
        checker: object,
        approved: bool,
        reason: object = None,
    ) -> MakerCheckerRequest:
        """Approve or reject one pending request under a locked read."""
        parsed_id = _request_uuid(request_id)
        request = self.repository.get_for_update(parsed_id)
        if request is None:
            raise NotFound(f"Maker-checker request {parsed_id} was not found.")
        definition = self.registry.get(request.operation)
        checker_id = actor_id(checker, field_name="checker")
        _require_permission(checker, definition.approve_permission)

        if request.state is not MakerCheckerState.PENDING:
            raise Conflict(
                f"Maker-checker request {parsed_id} was already decided as "
                f"{request.state.value}; a prior decision cannot be changed."
            )
        if request.maker_id == checker_id:
            raise Conflict(
                f"Maker-checker request {parsed_id} cannot be decided by its maker; "
                "the distinct-actor rule requires a different checker."
            )
        if not isinstance(approved, bool):
            raise ValidationError("approved must be a boolean.", field="approved")

        now = utc_now(self.clock)
        if _is_expired(request, now, self.settings):
            self._expire_locked(request, now)
            raise Conflict(
                f"Maker-checker request {parsed_id} has expired and cannot be approved "
                "or rejected; its state is expired."
            )

        decision_reason = validate_reason(reason, required=not approved)
        state = MakerCheckerState.APPROVED if approved else MakerCheckerState.REJECTED
        candidate = replace(
            request,
            checker_id=checker_id,
            state=state,
            decided_at=now,
            reason=decision_reason,
            version=request.version + 1,
        )

        if approved:
            # The callback and repository transition must share the caller's
            # transaction.  If either fails, the transaction boundary rolls
            # back both the aggregate application and this state transition.
            definition.callback(candidate, checker_id)

        decided = self.repository.decide(
            parsed_id,
            checker_id=checker_id,
            state=state,
            decided_at=now,
            reason=decision_reason,
            expected_version=request.version,
        )
        if decided.state is not state:
            raise Conflict(
                f"Maker-checker request {parsed_id} did not transition to the requested "
                f"state {state.value}."
            )
        event_type = _APPROVED_EVENT if approved else _REJECTED_EVENT
        self._audit(
            event_type,
            decided,
            actor=checker,
            payload=self._event_payload(
                decided,
                checker_required=True,
                approved=approved,
                applied=approved,
            ),
        )
        return decided

    def list_pending(self, checker: object) -> tuple[MakerCheckerRequest, ...]:
        """List only pending requests the checker can approve.

        A maker's own requests are omitted even when the actor happens to
        hold the approval permission.  That keeps the queue aligned with the
        distinct-actor rule and prevents a misleading approval control from
        being rendered.
        """
        checker_id = actor_id(checker, field_name="checker")
        now = utc_now(self.clock)
        requests: list[MakerCheckerRequest] = []
        for pending in self.repository.list_pending():
            if pending.state is not MakerCheckerState.PENDING:
                continue
            definition = self.registry.get(pending.operation)
            if not self.settings.is_enabled(definition.name):
                continue
            if pending.maker_id == checker_id:
                continue
            if not _has_permission(checker, definition.approve_permission):
                continue
            if _is_expired(pending, now, self.settings):
                locked = self.repository.get_for_update(pending.id)
                if locked is not None and locked.state is MakerCheckerState.PENDING:
                    if _is_expired(locked, now, self.settings):
                        self._expire_locked(locked, now)
                continue
            requests.append(pending)
        requests.sort(key=lambda item: (item.created_at, item.id.hex))
        return tuple(requests)

    pending = list_pending
    pending_approvals = list_pending

    def expire(self, request_id: UUID | str) -> MakerCheckerRequest:
        """Expire one request when its configured approval window has elapsed."""
        parsed_id = _request_uuid(request_id)
        request = self.repository.get_for_update(parsed_id)
        if request is None:
            raise NotFound(f"Maker-checker request {parsed_id} was not found.")
        if request.state is not MakerCheckerState.PENDING:
            return request
        now = utc_now(self.clock)
        if not _is_expired(request, now, self.settings):
            return request
        return self._expire_locked(request, now)

    def expire_pending(self) -> tuple[MakerCheckerRequest, ...]:
        """Expire every due pending request, returning only transitioned rows.

        Each expiry is already audited individually by :meth:`_expire_locked`
        (one event per affected request); this batch entry point adds the
        one summary event the whole sweep needs so its shape — how many
        requests were due, how many actually expired — is never lost inside
        a list of otherwise-identical per-request events.
        """
        candidates = tuple(
            request
            for request in self.repository.list_pending()
            if request.state is MakerCheckerState.PENDING
        )
        expired: list[MakerCheckerRequest] = []
        for request in candidates:
            transitioned = self.expire(request.id)
            if transitioned.state is MakerCheckerState.EXPIRED:
                expired.append(transitioned)
        expired.sort(key=lambda item: (item.decided_at or item.created_at, item.id.hex))
        self.audit.record(
            _EXPIRE_BATCH_EVENT,
            ("maker_checker_batch", new_id()),
            {
                "candidates": len(candidates),
                "expired": len(expired),
                "expired_request_ids": [str(item.id) for item in expired],
            },
            actor=None,
            request_id=self.request_id,
        )
        return tuple(expired)

    expire_due = expire_pending

    def _expire_locked(self, request: MakerCheckerRequest, now: datetime) -> MakerCheckerRequest:
        expired = self.repository.expire(
            request.id,
            expired_at=now,
            expected_version=request.version,
        )
        if expired.state is not MakerCheckerState.EXPIRED:
            raise Conflict(
                f"Maker-checker request {request.id} did not transition to expired."
            )
        notification_payload = self._event_payload(
            expired,
            checker_required=True,
            notification_required=True,
        )
        self._audit(_EXPIRED_EVENT, expired, actor=None, payload=notification_payload)
        self._notify_expiry(notification_payload)
        return expired

    def _notify_expiry(self, payload: Mapping[str, object]) -> None:
        if callable(self.notifier):
            self.notifier(_EXPIRY_NOTIFICATION, payload)
            return
        self.notifier.notify(_EXPIRY_NOTIFICATION, payload)

    def _event_payload(
        self,
        request: MakerCheckerRequest,
        *,
        checker_required: bool,
        no_second_actor_required: bool = False,
        approved: bool | None = None,
        applied: bool | None = None,
        notification_required: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "request_id": str(request.id),
            "operation": request.operation,
            "subject_type": request.subject_type,
            "subject_id": str(request.subject_id),
            "maker_id": str(request.maker_id),
            "checker_id": str(request.checker_id) if request.checker_id is not None else None,
            "state": request.state.value,
            "request_payload": dict(request.payload),
            "checker_required": checker_required,
            "no_second_actor_required": no_second_actor_required,
        }
        if approved is not None:
            payload["approved"] = approved
        if applied is not None:
            payload["applied"] = applied
        if request.reason is not None:
            payload["reason"] = request.reason
        if notification_required:
            payload["notification_required"] = True
        return payload

    def _audit(
        self,
        event_type: str,
        request: MakerCheckerRequest,
        *,
        actor: object,
        payload: Mapping[str, object],
    ) -> None:
        self.audit.record(
            event_type,
            (request.subject_type, request.subject_id),
            dict(payload),
            actor=actor,
            request_id=self.request_id,
        )


# The shorter name is useful to callers that do not need to distinguish this
# service from other approval workflows; both names identify the same class.
MakerCheckerService = ApprovalService


def _new_request_id() -> UUID:
    return new_id()


def _request_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValidationError("request_id must be a UUID.", field="request_id") from error
    raise ValidationError("request_id must be a UUID.", field="request_id")


def _is_expired(
    request: MakerCheckerRequest,
    now: datetime,
    settings: MakerCheckerSettings,
) -> bool:
    return now >= request.created_at + settings.expiry_window


def _require_permission(actor: object, permission: Permission) -> None:
    if isinstance(actor, Principal):
        authorize(actor, permission)
        return
    if not _has_permission(actor, permission):
        raise AuthorizationError(f"Missing permission: {permission.value}.", field="permission")


def _has_permission(actor: object, permission: Permission) -> bool:
    if isinstance(actor, Principal):
        return actor.has(permission)
    has_permission = getattr(actor, "has_permission", None)
    if callable(has_permission):
        try:
            return bool(has_permission(permission))
        except (TypeError, ValueError) as error:
            raise AuthorizationError(
                f"Unable to resolve permission {permission.value!r} for actor.",
                field="permission",
            ) from error
    permissions = getattr(actor, "permissions", None)
    if permissions is None:
        raise AuthorizationError(
            "An authenticated principal with permissions is required.", field="permission"
        )
    try:
        return permission in {coerce_permission(value) for value in permissions}
    except (TypeError, ValueError) as error:
        raise AuthorizationError("Actor permissions are invalid.", field="permission") from error


__all__ = ["ApprovalService", "MakerCheckerService", "NotificationSink"]
