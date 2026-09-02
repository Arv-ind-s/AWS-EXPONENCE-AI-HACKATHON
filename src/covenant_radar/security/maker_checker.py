"""Shared maker-checker primitives.

The maker-checker workflow is deliberately persistence-neutral.  This module
defines the request value object, operation registry, and the ports used by
the approval service; database adapters implement the ports without making
the security rules depend on SQLAlchemy.

An operation must register exactly one callback and two different permission
codes: one for proposing and one for approving.  The callback is the only
place where an approved payload is applied to the operation's aggregate.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

from covenant_radar.core.clock import Clock
from covenant_radar.core.errors import Conflict, ValidationError
from covenant_radar.security.permissions import Permission, coerce_permission
from covenant_radar.security.rbac import Principal

type PermissionLike = Permission | str

MAX_OPERATION_LENGTH: Final[int] = 50
MAX_SUBJECT_TYPE_LENGTH: Final[int] = 50
MAX_REASON_LENGTH: Final[int] = 2_000

_OPERATION_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_ALLOWED_REASON_CONTROLS = frozenset("\t\n\r")


class MakerCheckerState(str, Enum):
    """The persisted lifecycle of a maker-checker request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """The stable type and identifier of the aggregate being changed."""

    subject_type: str
    subject_id: UUID

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject_type", _validated_subject_type(self.subject_type))
        object.__setattr__(self, "subject_id", _validated_uuid(self.subject_id, "subject_id"))


@dataclass(frozen=True, slots=True)
class MakerCheckerRequest:
    """Persistence-neutral representation of ``maker_checker_request``.

    ``created_at`` is included even though it is a standard column on the
    database model because expiry is calculated from it.  ``version`` is
    passed to write methods so adapters can enforce optimistic concurrency in
    addition to row locking.
    """

    id: UUID
    subject_type: str
    subject_id: UUID
    operation: str
    payload: Mapping[str, object]
    maker_id: UUID
    checker_id: UUID | None
    state: MakerCheckerState
    created_at: datetime
    decided_at: datetime | None = None
    reason: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _validated_uuid(self.id, "request_id"))
        object.__setattr__(self, "subject_type", _validated_subject_type(self.subject_type))
        object.__setattr__(self, "subject_id", _validated_uuid(self.subject_id, "subject_id"))
        object.__setattr__(self, "operation", _validated_operation(self.operation))
        object.__setattr__(self, "payload", _copy_json_object(self.payload))
        object.__setattr__(self, "maker_id", _validated_uuid(self.maker_id, "maker_id"))
        if self.checker_id is not None:
            object.__setattr__(
                self, "checker_id", _validated_uuid(self.checker_id, "checker_id")
            )
        try:
            state = (
                self.state
                if isinstance(self.state, MakerCheckerState)
                else MakerCheckerState(self.state)
            )
        except (TypeError, ValueError) as error:
            raise ValidationError(
                f"Unknown maker-checker state: {self.state!r}.", field="state"
            ) from error
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.decided_at is not None:
            object.__setattr__(self, "decided_at", _utc(self.decided_at, "decided_at"))
        if self.reason is not None:
            object.__setattr__(self, "reason", _validated_reason(self.reason, required=False))
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValidationError("Request version must be a positive integer.", field="version")

    @property
    def subject(self) -> SubjectRef:
        """Return the subject in the shape expected by the audit port."""
        return SubjectRef(self.subject_type, self.subject_id)

    @property
    def is_pending(self) -> bool:
        """Whether the request can still receive a decision."""
        return self.state is MakerCheckerState.PENDING


ApplicationCallback = Callable[[MakerCheckerRequest, UUID], object]


@dataclass(frozen=True, slots=True)
class ApprovalOperation:
    """An operation's permissions and its single payload-application hook."""

    name: str
    propose_permission: Permission
    approve_permission: Permission
    callback: ApplicationCallback

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validated_operation(self.name))
        try:
            propose = coerce_permission(self.propose_permission)
            approve = coerce_permission(self.approve_permission)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid permissions for operation {self.name!r}: {error}") from error
        if propose is approve:
            raise ValueError(
                f"Operation {self.name!r} must use distinct propose and approve permissions."
            )
        if not callable(self.callback):
            raise TypeError(f"Operation {self.name!r} callback must be callable.")
        object.__setattr__(self, "propose_permission", propose)
        object.__setattr__(self, "approve_permission", approve)


class ApplicationCallbackRegistry:
    """Thread-safe registry enforcing one callback per operation type."""

    def __init__(self, operations: Sequence[ApprovalOperation] | None = None) -> None:
        self._operations: dict[str, ApprovalOperation] = {}
        self._lock = RLock()
        for operation in operations or ():
            self.register(
                operation.name,
                operation.callback,
                propose_permission=operation.propose_permission,
                approve_permission=operation.approve_permission,
            )

    def register(
        self,
        operation: str,
        callback: ApplicationCallback,
        *,
        propose_permission: PermissionLike,
        approve_permission: PermissionLike,
    ) -> ApprovalOperation:
        """Register one operation, refusing duplicate or ambiguous wiring."""
        definition = ApprovalOperation(
            name=operation,
            propose_permission=coerce_permission(propose_permission),
            approve_permission=coerce_permission(approve_permission),
            callback=callback,
        )
        with self._lock:
            if definition.name in self._operations:
                raise Conflict(
                    f"An application callback is already registered for operation "
                    f"{definition.name!r}."
                )
            self._operations[definition.name] = definition
        return definition

    register_operation = register

    def get(self, operation: str) -> ApprovalOperation:
        """Return an operation definition or a configuration error."""
        name = _validated_operation(operation)
        with self._lock:
            definition = self._operations.get(name)
        if definition is None:
            raise ValidationError(
                f"No maker-checker callback is registered for operation {name!r}.",
                field="operation",
            )
        return definition

    def callback_for(self, operation: str) -> ApplicationCallback:
        """Return the one callback registered for *operation*."""
        return self.get(operation).callback

    def registered_operations(self) -> tuple[str, ...]:
        """Return registered operation names in stable order."""
        with self._lock:
            return tuple(sorted(self._operations))

    def definitions(self) -> tuple[ApprovalOperation, ...]:
        """Return an immutable, stable snapshot of the registry."""
        with self._lock:
            return tuple(self._operations[name] for name in sorted(self._operations))

    def __contains__(self, operation: object) -> bool:
        if not isinstance(operation, str):
            return False
        with self._lock:
            return operation in self._operations

    def __len__(self) -> int:
        with self._lock:
            return len(self._operations)


@dataclass(frozen=True, slots=True)
class MakerCheckerSettings:
    """Runtime policy for approval expiry and per-operation enablement.

    The expiry duration is intentionally injected as a setting rather than
    embedded in the service.  The seven-day default is a safe local default;
    deployments should provide their approved operational value explicitly.
    """

    expiry_window: timedelta = timedelta(days=7)
    enabled_operations: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.expiry_window <= timedelta(0):
            raise ValueError("Maker-checker expiry_window must be positive.")
        normalized: dict[str, bool] = {}
        for operation, enabled in self.enabled_operations.items():
            name = _validated_operation(operation)
            if not isinstance(enabled, bool):
                raise ValueError(f"Enablement for operation {name!r} must be boolean.")
            normalized[name] = enabled
        object.__setattr__(self, "enabled_operations", MappingProxyType(normalized))

    def is_enabled(self, operation: str) -> bool:
        """Return whether the second-actor control is enabled for an operation."""
        name = _validated_operation(operation)
        return self.enabled_operations.get(name, True)


class MakerCheckerRepository(Protocol):
    """Persistence port used by :class:`ApprovalService`.

    Implementations must lock the row in ``get_for_update`` and make the
    ``expected_version`` part of each write's concurrency check.  None of the
    methods commits: the enclosing use case owns the transaction.
    """

    def create(self, request: MakerCheckerRequest) -> MakerCheckerRequest:
        """Insert and return a pending request."""
        ...

    def get_for_update(self, request_id: UUID) -> MakerCheckerRequest | None:
        """Load a request under a row lock, or return ``None``."""
        ...

    def list_pending(self) -> Sequence[MakerCheckerRequest]:
        """Return requests currently persisted as pending."""
        ...

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
        """Atomically transition a locked request to approved or rejected."""
        ...

    def expire(
        self,
        request_id: UUID,
        *,
        expired_at: datetime,
        expected_version: int,
    ) -> MakerCheckerRequest:
        """Atomically transition a locked pending request to expired."""
        ...


class AuditWriter(Protocol):
    """The C-60 audit surface used by the approval service."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's transaction."""
        ...


class ApprovalNotifier(Protocol):
    """Durable notification boundary used when a request expires."""

    def notify(self, event_type: str, payload: Mapping[str, object]) -> object:
        """Enqueue or deliver one notification."""
        ...


def actor_id(actor: object, *, field_name: str = "actor") -> UUID:
    """Extract and validate an actor identifier without trusting display data."""
    if isinstance(actor, Principal):
        return actor.id
    candidate: object = actor if isinstance(actor, UUID) else getattr(actor, "id", None)
    if isinstance(candidate, str):
        try:
            candidate = UUID(candidate)
        except ValueError:
            candidate = None
    if not isinstance(candidate, UUID):
        raise ValidationError(
            f"{field_name} must identify an authenticated user.", field=field_name
        )
    return candidate


def subject_ref(subject: object) -> SubjectRef:
    """Normalize the subject forms accepted at service boundaries."""
    if isinstance(subject, SubjectRef):
        return subject
    if isinstance(subject, UUID):
        return SubjectRef("subject", subject)
    if isinstance(subject, tuple) and len(subject) == 2:
        return SubjectRef(str(subject[0]), _coerce_uuid(subject[1], "subject_id"))
    if isinstance(subject, Mapping):
        subject_type = subject.get("subject_type", subject.get("type"))
        subject_id = subject.get("subject_id", subject.get("id"))
        if subject_type is not None and subject_id is not None:
            return SubjectRef(str(subject_type), _coerce_uuid(subject_id, "subject_id"))
    subject_type = getattr(subject, "subject_type", None)
    subject_id = getattr(subject, "subject_id", None)
    if subject_type is not None and subject_id is not None:
        return SubjectRef(str(subject_type), _coerce_uuid(subject_id, "subject_id"))
    subject_id = getattr(subject, "id", None)
    if subject_id is not None:
        table_name = getattr(subject, "__tablename__", None)
        if not isinstance(table_name, str) or not table_name:
            table_name = _snake_case(type(subject).__name__)
        return SubjectRef(table_name, _coerce_uuid(subject_id, "subject_id"))
    raise ValidationError(
        "subject must provide a subject type and UUID identifier.", field="subject"
    )


def validate_reason(reason: object, *, required: bool) -> str | None:
    """Validate a decision reason while preserving an intentional blank rule."""
    return _validated_reason(reason, required=required)


def utc_now(clock: Clock) -> datetime:
    """Read and validate the injected clock at a service boundary."""
    return _utc(clock.now(), "clock")


def _validated_operation(operation: object) -> str:
    if not isinstance(operation, str):
        raise ValidationError("operation must be a string.", field="operation")
    name = operation.strip()
    if (
        not name
        or len(name) > MAX_OPERATION_LENGTH
        or _OPERATION_PATTERN.fullmatch(name) is None
    ):
        raise ValidationError(
            "operation must be a non-empty identifier of at most 50 characters.",
            field="operation",
        )
    return name


def _validated_subject_type(subject_type: object) -> str:
    if not isinstance(subject_type, str):
        raise ValidationError("subject_type must be a string.", field="subject_type")
    value = subject_type.strip()
    if not value or len(value) > MAX_SUBJECT_TYPE_LENGTH:
        raise ValidationError(
            "subject_type must be a non-empty value of at most 50 characters.",
            field="subject_type",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError("subject_type contains a control character.", field="subject_type")
    return value


def _validated_uuid(value: object, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    raise ValidationError(f"{field_name} must be a UUID.", field=field_name)


def _coerce_uuid(value: object, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValidationError(f"{field_name} must be a UUID.", field=field_name) from error
    raise ValidationError(f"{field_name} must be a UUID.", field=field_name)


def _copy_json_object(payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValidationError("payload must be a JSON object.", field="payload")
    try:
        _validate_json_value(payload)
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        copied = copy.deepcopy(dict(payload))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValidationError(
            "payload must contain only JSON-compatible values.", field="payload"
        ) from error
    return copied


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not valid JSON")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _validate_json_value(child)
        return
    if isinstance(value, list | tuple):
        for child in value:
            _validate_json_value(child)
        return
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _validated_reason(reason: object, *, required: bool) -> str | None:
    if reason is None:
        if required:
            raise ValidationError("A reason is required when rejecting a request.", field="reason")
        return None
    if not isinstance(reason, str):
        raise ValidationError("reason must be text.", field="reason")
    value = reason.strip()
    if not value and required:
        raise ValidationError("A reason is required when rejecting a request.", field="reason")
    if len(value) > MAX_REASON_LENGTH:
        raise ValidationError("reason must be at most 2000 characters.", field="reason")
    if any(
        ord(character) < 32 and character not in _ALLOWED_REASON_CONTROLS for character in value
    ):
        raise ValidationError("reason contains a prohibited control character.", field="reason")
    return value or None


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware.", field=field_name)
    return value.astimezone(UTC)


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


__all__ = [
    "ApplicationCallback",
    "ApplicationCallbackRegistry",
    "PermissionLike",
    "ApprovalNotifier",
    "ApprovalOperation",
    "AuditWriter",
    "MakerCheckerRepository",
    "MakerCheckerRequest",
    "MakerCheckerSettings",
    "MakerCheckerState",
    "MAX_REASON_LENGTH",
    "SubjectRef",
    "actor_id",
    "subject_ref",
    "utc_now",
    "validate_reason",
]
