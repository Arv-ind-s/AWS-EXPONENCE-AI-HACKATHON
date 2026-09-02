"""The durable in-app notification channel and notification-centre queries.

The in-app channel is intentionally local: :class:`NotificationService`
persists the notification before delivery and this adapter only acknowledges
that the application has accepted it.  Read state is stored separately from
delivery state on the notification row, so a restart cannot make an old
notification unread or silently lose a bulk-read action.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast
from uuid import UUID

from sqlalchemy import Select, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import NotFound, ValidationError
from covenant_radar.db.models import (
    Borrower,
    Case,
    CertificateRequest,
    Covenant,
    CovenantVersion,
    Document,
    EvidenceItem,
    Facility,
    Forecast,
    Memo,
    Notification,
    NotificationReadState,
    TriageEntry,
)
from covenant_radar.db.models.identity import (
    AppUser,
    RolePermission,
    UserRole,
)
from covenant_radar.db.models.identity import (
    Permission as PermissionRow,
)
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.notifications.digest import deep_link
from covenant_radar.notifications.model import (
    NotificationState,
    TemplateRegistry,
    TemplateRenderError,
)
from covenant_radar.notifications.templates import DEFAULT_TEMPLATE_REGISTRY
from covenant_radar.ports.notifier import (
    DeliveryResult,
    DeliveryStatus,
    NotificationChannel,
    OutboundMessage,
)
from covenant_radar.security.permissions import Permission, coerce_permission
from covenant_radar.security.rbac import Principal

_IN_APP_CHANNELS: Final[tuple[str, ...]] = (
    NotificationChannel.IN_APP.value,
    "in_app",
)
_VISIBLE_STATES: Final[tuple[str, ...]] = (
    NotificationState.PENDING.value,
    NotificationState.SENT.value,
    NotificationState.FAILED.value,
)
_MAX_PAGE_SIZE: Final[int] = 100
_DEFAULT_PAGE_SIZE: Final[int] = 50
_MAX_TEMPLATE_LENGTH: Final[int] = 100
_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_GENERIC_TITLE: Final[str] = "Notification unavailable"
_GENERIC_BODY: Final[str] = "This notification is no longer available in your access scope."

_SUBJECT_MODELS: Final[Mapping[str, type[Any]]] = {
    "borrower": Borrower,
    "case": Case,
    "certificate_request": CertificateRequest,
    "covenant": Covenant,
    "covenant_version": CovenantVersion,
    "document": Document,
    "evidence_item": EvidenceItem,
    "facility": Facility,
    "forecast": Forecast,
    "memo": Memo,
    "triage_entry": TriageEntry,
}


class NotificationAuditWriter(Protocol):
    """The narrow audit seam required by read-state mutations."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one audit event in the caller's transaction."""
        ...


@dataclass(frozen=True, slots=True)
class InAppNotificationView:
    """A disclosure-safe notification representation for the browser."""

    notification_id: UUID
    title: str
    body: str
    created_at: datetime
    is_read: bool
    is_accessible: bool
    deep_link: str | None
    template: str
    state: str

    @property
    def id(self) -> UUID:
        """Return the durable notification identifier."""
        return self.notification_id

    @property
    def read(self) -> bool:
        """Compatibility-friendly name for templates and callers."""
        return self.is_read

    @property
    def accessible(self) -> bool:
        """Whether the original content may be shown to this recipient."""
        return self.is_accessible


@dataclass(frozen=True, slots=True)
class InAppNotificationPage:
    """One bounded page and its independently computed unread count."""

    items: tuple[InAppNotificationView, ...]
    total: int
    unread_count: int
    page: int
    page_size: int
    status: str
    template: str | None = None

    @property
    def notifications(self) -> tuple[InAppNotificationView, ...]:
        """Return the page items under the domain-neutral name."""
        return self.items

    @property
    def rows(self) -> tuple[InAppNotificationView, ...]:
        """Return page items for table-oriented callers."""
        return self.items

    @property
    def has_next(self) -> bool:
        """Whether another bounded page exists."""
        return self.page * self.page_size < self.total


class InAppNotifier:
    """C-54 adapter for the local, durable in-app channel.

    It never stores content in process memory.  The notification service has
    already written the durable row before this acknowledgement is requested;
    keeping this adapter stateless makes retries and multiple web workers
    safe.
    """

    def send(self, message: OutboundMessage) -> DeliveryResult:
        """Acknowledge an accepted in-app message."""

        if not isinstance(message, OutboundMessage):
            raise TypeError("InAppNotifier.send requires an OutboundMessage.")
        if message.channel is not NotificationChannel.IN_APP:
            raise ValueError("InAppNotifier can send only in-app messages.")
        return DeliveryResult(DeliveryStatus.SENT, provider_message_id="in-app-accepted")

    @property
    def capability(self) -> tuple[bool, str]:
        """Expose the always-available channel for capability views."""
        return True, "available"

    deliver = send


class InAppNotificationService:
    """Read, render and mutate notifications for one authenticated user."""

    def __init__(
        self,
        session: Session,
        *,
        template_registry: TemplateRegistry | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        permission_resolver: Callable[[UUID], Iterable[Permission | str]] | None = None,
        audit: NotificationAuditWriter | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("InAppNotificationService requires a SQLAlchemy Session.")
        if template_registry is not None and not isinstance(template_registry, TemplateRegistry):
            raise TypeError("template_registry must be a TemplateRegistry.")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("scope_resolver must be callable.")
        if permission_resolver is not None and not callable(permission_resolver):
            raise TypeError("permission_resolver must be callable.")
        if audit is not None and not callable(getattr(audit, "record", None)):
            raise TypeError("audit must expose record(...).")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("clock must expose now().")

        self.session = session
        self.template_registry = template_registry or DEFAULT_TEMPLATE_REGISTRY
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )
        self.permission_resolver = permission_resolver or self._database_permissions
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())

    def unread_count(self, principal: Principal | UUID | str) -> int:
        """Count unread, deliverable in-app rows using the composite index."""

        user_id = self._user_id(principal)
        if not self._active_user(user_id):
            return 0
        statement = (
            select(func.count(Notification.id))
            .where(
                NotificationReadState.id.is_(None),
                *self._base_conditions(user_id),
            )
            .select_from(Notification)
            .outerjoin(
                NotificationReadState,
                NotificationReadState.notification_id == Notification.id,
            )
        )
        return int(self.session.scalar(statement) or 0)

    count_unread = unread_count
    count = unread_count

    def list_notifications(
        self,
        principal: Principal | UUID | str,
        *,
        status: str = "all",
        template: str | None = None,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        notification_filter: str | None = None,
    ) -> InAppNotificationPage:
        """Return a bounded, recipient-scoped page with safe rendered content.

        The notification row is selected by recipient id before any payload
        is read.  Subject access is then checked again at view time; a scope
        change therefore leaves the existence of the notification visible
        while replacing its title, body and link with a generic safe state.
        """

        user_id = self._user_id(principal)
        normalized_status = _status(notification_filter or status)
        normalized_template = _optional_template(template)
        normalized_page = _positive_int(page, "page")
        normalized_page_size = _page_size(page_size)
        if not self._active_user(user_id):
            return InAppNotificationPage(
                items=(),
                total=0,
                unread_count=0,
                page=normalized_page,
                page_size=normalized_page_size,
                status=normalized_status,
                template=normalized_template,
            )

        conditions: list[ColumnElement[bool]] = self._base_conditions(user_id)
        if normalized_status == "unread":
            conditions.append(NotificationReadState.id.is_(None))
        elif normalized_status == "read":
            conditions.append(NotificationReadState.id.is_not(None))
        if normalized_template is not None:
            conditions.append(Notification.template == normalized_template)

        total_statement = (
            select(func.count(Notification.id))
            .select_from(Notification)
            .outerjoin(
                NotificationReadState,
                NotificationReadState.notification_id == Notification.id,
            )
            .where(*conditions)
        )
        total = int(self.session.scalar(total_statement) or 0)
        unread_count = self.unread_count(user_id)
        statement: Select[tuple[Notification, datetime | None]] = (
            select(Notification, NotificationReadState.read_at)
            .select_from(Notification)
            .outerjoin(
                NotificationReadState,
                NotificationReadState.notification_id == Notification.id,
            )
            .where(*conditions)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset((normalized_page - 1) * normalized_page_size)
            .limit(normalized_page_size)
        )
        rows = tuple(self.session.execute(statement).all())
        scope = self._recipient_scope(user_id, principal)
        permissions = (
            principal.permissions
            if isinstance(principal, Principal)
            else self._permissions_for(user_id)
        )
        items = tuple(self._view(row, read_at, scope, permissions) for row, read_at in rows)
        return InAppNotificationPage(
            items=items,
            total=total,
            unread_count=unread_count,
            page=normalized_page,
            page_size=normalized_page_size,
            status=normalized_status,
            template=normalized_template,
        )

    list_for_user = list_notifications
    page = list_notifications

    def mark_read(self, principal: Principal | UUID | str, notification_id: UUID | str) -> bool:
        """Mark one recipient-owned in-app row read, idempotently."""

        user_id = self._user_id(principal)
        resolved_id = _uuid(notification_id, "notification_id")
        row = self.session.scalar(
            select(Notification)
            .where(
                Notification.id == resolved_id,
                *self._base_conditions(user_id),
            )
            .with_for_update()
            .limit(1)
        )
        if row is None:
            raise NotFound("Notification was not found.")
        if (
            self.session.scalar(
                select(NotificationReadState)
                .where(NotificationReadState.notification_id == row.id)
                .with_for_update()
                .limit(1)
            )
            is not None
        ):
            return False
        now = _utc(self.clock.now(), "clock.now()")
        self.session.add(
            NotificationReadState(
                id=row.id,
                notification_id=row.id,
                recipient_id=user_id,
                read_at=now,
                created_at=now,
                updated_at=now,
                created_by_id=user_id,
                updated_by_id=user_id,
                request_id=self.request_id,
            )
        )
        self.session.flush()
        self._audit(
            AuditEventType.NOTIFICATION_MARKED_READ.value,
            "notification",
            resolved_id,
            {"notification_id": str(resolved_id), "count": 1},
            user_id,
        )
        return True

    mark_as_read = mark_read
    mark_notification_read = mark_read

    def mark_all_read(self, principal: Principal | UUID | str) -> int:
        """Mark all unread in-app rows in one bulk insert and audit event."""

        user_id = self._user_id(principal)
        now = _utc(self.clock.now(), "clock.now()")
        source = (
            select(
                Notification.id,
                Notification.id,
                Notification.recipient_id,
                literal(now),
                literal(now),
                literal(now),
                literal(user_id),
                literal(user_id),
                literal(self.request_id),
            )
            .select_from(Notification)
            .outerjoin(
                NotificationReadState,
                NotificationReadState.notification_id == Notification.id,
            )
            .where(
                *self._base_conditions(user_id),
                NotificationReadState.id.is_(None),
            )
        )
        columns = (
            "id",
            "notification_id",
            "recipient_id",
            "read_at",
            "created_at",
            "updated_at",
            "created_by_id",
            "updated_by_id",
            "request_id",
        )
        dialect = self.session.bind.dialect.name if self.session.bind is not None else ""
        if dialect == "sqlite":
            statement = sqlite_insert(NotificationReadState).from_select(columns, source)
            statement = statement.on_conflict_do_nothing(
                index_elements=[NotificationReadState.notification_id]
            )
        elif dialect == "postgresql":
            statement = postgresql_insert(NotificationReadState).from_select(columns, source)
            statement = statement.on_conflict_do_nothing(
                index_elements=[NotificationReadState.notification_id]
            )
        else:
            statement = insert(NotificationReadState).from_select(columns, source)
        result = cast(CursorResult[Any], self.session.execute(statement))
        count = max(0, int(result.rowcount or 0))
        self.session.flush()
        self._audit(
            AuditEventType.NOTIFICATIONS_MARKED_READ.value,
            "app_user",
            user_id,
            {"recipient_id": str(user_id), "count": count},
            user_id,
        )
        return count

    mark_all_as_read = mark_all_read
    mark_all_notifications_read = mark_all_read

    def _view(
        self,
        row: Notification,
        read_at: datetime | None,
        scope: Scope,
        permissions: frozenset[Permission],
    ) -> InAppNotificationView:
        created_at = _utc(row.created_at, "notification.created_at")
        subject_type, subject_id = _subject(row)
        if subject_type is not None and not self._subject_visible(subject_type, subject_id, scope):
            return _inaccessible_view(row, read_at, created_at)
        payload = row.payload if isinstance(row.payload, Mapping) else {}
        try:
            template = self.template_registry.get(row.template)
            filtered = template.filter_payload(
                payload,
                can_disclose=lambda value: self._can_disclose(value, scope, permissions),
                permissions=permissions,
            )
            rendered = template.render(filtered, allow_removed_slots=filtered.removed_slots)
            link = deep_link(subject_type, subject_id, rendered.payload)
        except (TemplateRenderError, TypeError, ValueError):
            # A malformed or retired template is never rendered from a raw
            # database payload.  The row remains visible as an inaccessible
            # notification, preserving the audit/reconciliation surface.
            return _inaccessible_view(row, read_at, created_at)
        return InAppNotificationView(
            notification_id=row.id,
            title=rendered.subject,
            body=rendered.body,
            created_at=created_at,
            is_read=read_at is not None,
            is_accessible=True,
            deep_link=link,
            template=template.name,
            state=row.state,
        )

    def _can_disclose(
        self,
        value: object,
        scope: Scope,
        permissions: frozenset[Permission],
    ) -> bool:
        """Apply the same fail-closed policy used while queueing content."""

        required_permission = getattr(value, "required_permission", None)
        if required_permission is not None and required_permission not in permissions:
            return False
        subject_type = getattr(value, "subject_type", None)
        subject_id = getattr(value, "subject_id", None)
        if subject_type is not None or subject_id is not None:
            if not isinstance(subject_type, str) or not isinstance(subject_id, UUID):
                return False
            if not self._subject_visible(subject_type, subject_id, scope):
                return False
        portfolio_path = getattr(value, "portfolio_path", None)
        if portfolio_path is not None:
            if not isinstance(portfolio_path, str):
                return False
            normalized_path = (
                portfolio_path if portfolio_path.endswith("/") else f"{portfolio_path}/"
            )
            if normalized_path not in scope.exact_paths and not any(
                normalized_path.startswith(prefix) for prefix in scope.descendant_paths
            ):
                return False
        return True

    def _recipient_scope(
        self,
        user_id: UUID,
        principal: Principal | UUID | str,
    ) -> Scope:
        resolved_principal = (
            principal
            if isinstance(principal, Principal)
            else Principal.user(user_id, self._permissions_for(user_id))
        )
        scope = self.scope_resolver(resolved_principal)
        if not isinstance(scope, Scope) or scope.principal_id != user_id:
            raise ValidationError("Recipient scope does not belong to the recipient.")
        return scope

    def _permissions_for(self, user_id: UUID) -> frozenset[Permission]:
        try:
            return frozenset(
                coerce_permission(value) for value in self.permission_resolver(user_id)
            )
        except (TypeError, ValueError) as error:
            raise ValidationError(f"Recipient permissions are invalid: {error}.") from error

    def _database_permissions(self, user_id: UUID) -> frozenset[Permission]:
        statement = (
            select(PermissionRow.code)
            .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
        )
        return frozenset(
            coerce_permission(value) for value in self.session.execute(statement).scalars()
        )

    def _subject_visible(
        self, subject_type: str | None, subject_id: UUID | None, scope: Scope
    ) -> bool:
        if subject_type is None or subject_id is None:
            return subject_type is None and subject_id is None
        model = _SUBJECT_MODELS.get(subject_type.strip().lower())
        if model is None:
            return False
        try:
            ownership = ownership_path_for(model)
            statement = select(ownership.path_column).select_from(model)
            statement = ownership.apply(statement).where(model.id == subject_id).limit(1)
            path = self.session.scalar(statement)
        except Exception:
            return False
        if not isinstance(path, str):
            return False
        return path in scope.exact_paths or any(
            path.startswith(prefix) for prefix in scope.descendant_paths
        )

    def _base_conditions(self, user_id: UUID) -> list[ColumnElement[bool]]:
        return [
            Notification.recipient_id == user_id,
            Notification.channel.in_(_IN_APP_CHANNELS),
            Notification.state.in_(_VISIBLE_STATES),
            or_(
                Notification.scheduled_for.is_(None),
                Notification.scheduled_for <= _utc(self.clock.now(), "clock.now()"),
            ),
        ]

    def _active_user(self, user_id: UUID) -> bool:
        return bool(
            self.session.scalar(
                select(AppUser.id)
                .where(AppUser.id == user_id, AppUser.is_active.is_(True))
                .limit(1)
            )
        )

    def _user_id(self, principal: Principal | UUID | str) -> UUID:
        if isinstance(principal, Principal):
            return principal.id
        return _uuid(principal, "principal")

    def _audit(
        self,
        event_type: str,
        subject_type: str,
        notification_id: UUID,
        payload: Mapping[str, object],
        actor_id: UUID,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            event_type,
            (subject_type, notification_id),
            payload,
            actor=actor_id,
            request_id=self.request_id,
        )


def _inaccessible_view(
    row: Notification, read_at: datetime | None, created_at: datetime
) -> InAppNotificationView:
    """Build a view that preserves existence while discarding content."""

    return InAppNotificationView(
        notification_id=row.id,
        title=_GENERIC_TITLE,
        body=_GENERIC_BODY,
        created_at=created_at,
        is_read=read_at is not None,
        is_accessible=False,
        deep_link=None,
        template="",
        state=row.state,
    )


def _subject(row: Notification) -> tuple[str | None, UUID | None]:
    subject_type = row.subject_type
    subject_id = row.subject_id
    if subject_type is None and subject_id is None:
        return None, None
    if (
        not isinstance(subject_type, str)
        or not subject_type.strip()
        or not isinstance(subject_id, UUID)
    ):
        return "", None
    return subject_type.strip().lower(), subject_id


def _status(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("status must be text.", field="status")
    normalized = value.strip().lower()
    if normalized not in {"all", "unread", "read"}:
        raise ValidationError("status must be one of: all, unread, read.", field="status")
    return normalized


def _optional_template(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("template must be text.", field="template")
    normalized = value.strip().lower()
    if not normalized or len(normalized) > _MAX_TEMPLATE_LENGTH:
        raise ValidationError("template must be bounded non-blank text.", field="template")
    return normalized


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{field_name} must be a positive integer.", field=field_name)
    return value


def _page_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PAGE_SIZE:
        raise ValidationError(
            f"page_size must be between 1 and {_MAX_PAGE_SIZE}.", field="page_size"
        )
    return value


def _uuid(value: UUID | str, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a UUID.", field=field_name)
    try:
        return UUID(value)
    except ValueError as error:
        raise ValidationError(f"{field_name} must be a valid UUID.", field=field_name) from error


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware.", field=field_name)
    return value.astimezone(UTC)


def _request_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > _REQUEST_ID_MAX_LENGTH
    ):
        raise ValidationError("request_id must be bounded non-blank text.", field="request_id")
    return value.strip()


InAppNotificationCenter = InAppNotificationService
InAppStore = InAppNotificationService
InAppDeliveryNotifier = InAppNotifier


__all__ = [
    "InAppDeliveryNotifier",
    "InAppNotificationCenter",
    "InAppNotificationPage",
    "InAppNotificationService",
    "InAppNotificationView",
    "InAppNotifier",
    "InAppStore",
]
