"""Durable notification queueing and policy enforcement.

This service is the one place where a notification becomes a disclosure.  It
resolves active recipients, applies portfolio and permission checks to every
scope-bearing value, evaluates preferences, validates the complete template,
and only then writes a pending notification row.  Channel adapters receive a
rendered :class:`~covenant_radar.ports.notifier.OutboundMessage`; they never
load records or make authorization decisions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from threading import RLock
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import ExternalServiceError, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models import (
    AppUser,
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
    Role,
    RolePermission,
    TriageEntry,
    UserRole,
)
from covenant_radar.db.models import (
    NotificationPreference as NotificationPreferenceRow,
)
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.notifications.model import (
    NotificationState,
    NotificationTemplate,
    ScopedValue,
    TemplateRegistry,
    TemplateRenderError,
)
from covenant_radar.notifications.preferences import (
    DigestFrequency,
    NotificationPreference,
    default_preference,
)
from covenant_radar.notifications.templates import DEFAULT_TEMPLATE_REGISTRY
from covenant_radar.ports.notifier import (
    DeliveryResult,
    DeliveryStatus,
    NotificationChannel,
    Notifier,
    OutboundMessage,
)
from covenant_radar.security.permissions import Permission, coerce_permission
from covenant_radar.security.rbac import Principal

_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_LAST_ERROR_MAX_LENGTH: Final[int] = 2_000
_MAX_RECIPIENTS: Final[int] = 10_000
_MAX_BATCH: Final[int] = 1_000
_DEFAULT_RETRY_BASE_SECONDS: Final[int] = 30
_MAX_RETRY_SECONDS: Final[int] = 3_600
_WEBHOOK_CHANNEL: Final[str] = NotificationChannel.WEBHOOK.value

_SUBJECT_MODELS: Final[Mapping[str, Any]] = {
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

_SUPPRESSION_INACTIVE = "recipient is inactive at delivery time"
_SUPPRESSION_OUT_OF_SCOPE = "recipient no longer has access to the notification subject"
_SUPPRESSION_EMPTY = "all notification content was removed by recipient scope"
_SUPPRESSION_DISABLED = "notification disabled by recipient preference"
_QUIET_HOURS_DEFERRED = "notification deferred by recipient quiet hours"


class AuditWriter(Protocol):
    """The optional append-only audit boundary for notification decisions."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one audit event."""
        ...


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """One attempted or suppressed durable notification."""

    notification: Notification
    result: DeliveryResult | None
    state: NotificationState
    reason: str | None = None


class NotificationService:
    """Queue and deliver policy-checked notifications."""

    def __init__(
        self,
        session: Session,
        *,
        notifier: Notifier | None = None,
        audit: AuditWriter | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
        template_registry: TemplateRegistry | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        permission_resolver: Callable[[UUID], Iterable[Permission | str]] | None = None,
        retry_base_seconds: int = _DEFAULT_RETRY_BASE_SECONDS,
        max_attempts: int = 3,
        dead_letter_alert: Callable[[Notification, str], object] | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("NotificationService requires a SQLAlchemy Session.")
        if notifier is not None and not callable(getattr(notifier, "send", None)):
            raise TypeError("NotificationService notifier must expose send(message).")
        if audit is not None and not callable(getattr(audit, "record", None)):
            raise TypeError("NotificationService audit must expose record(...).")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("NotificationService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("NotificationService scope_resolver must be callable.")
        if permission_resolver is not None and not callable(permission_resolver):
            raise TypeError("NotificationService permission_resolver must be callable.")
        if template_registry is not None and not isinstance(template_registry, TemplateRegistry):
            raise TypeError("NotificationService template_registry must be a TemplateRegistry.")
        if (
            isinstance(retry_base_seconds, bool)
            or not isinstance(retry_base_seconds, int)
            or not 1 <= retry_base_seconds <= _MAX_RETRY_SECONDS
        ):
            raise ValidationError("retry_base_seconds must be between 1 and 3600.")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 20
        ):
            raise ValidationError("max_attempts must be between 1 and 20.")
        if dead_letter_alert is not None and not callable(dead_letter_alert):
            raise TypeError("dead_letter_alert must be callable.")

        self.session = session
        self.notifier = notifier
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())
        self.template_registry = template_registry or DEFAULT_TEMPLATE_REGISTRY
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )
        self.permission_resolver = permission_resolver or self._database_permissions
        self.retry_base_seconds = retry_base_seconds
        self.max_attempts = max_attempts
        self.dead_letter_alert = dead_letter_alert
        self._delivery_lock = RLock()

    def queue(
        self,
        template: str | NotificationTemplate,
        payload: Mapping[str, object],
        *,
        recipients: UUID | str | Iterable[UUID | str] | None = None,
        recipient_ids: UUID | str | Iterable[UUID | str] | None = None,
        recipient_id: UUID | str | None = None,
        recipient_roles: Iterable[str] = (),
        channel: NotificationChannel | str = NotificationChannel.IN_APP,
        subject_type: str | None = None,
        subject_id: UUID | str | None = None,
        scheduled_for: datetime | None = None,
        actor_id: UUID | None = None,
        request_id: str | None = None,
    ) -> tuple[Notification, ...]:
        """Queue one message per resolved recipient in one savepoint.

        The method never commits the surrounding unit of work.  A rendering,
        authorization or validation failure rolls back the complete queueing
        operation, so a caller cannot accidentally commit a partial fan-out.
        """

        resolved_template = self._template(template)
        if not isinstance(payload, Mapping):
            raise ValidationError("Notification payload must be a mapping.", field="payload")
        resolved_channel = _channel(channel)
        normalized_subject = _subject(subject_type, subject_id)
        resolved_request_id = _request_id(request_id or self.request_id)
        actor = _uuid(actor_id, "actor_id") if actor_id is not None else None
        recipient_values = self._resolve_recipient_ids(
            recipients=recipients,
            recipient_ids=recipient_ids,
            recipient_id=recipient_id,
            recipient_roles=recipient_roles,
        )
        if len(recipient_values) > _MAX_RECIPIENTS:
            raise ValidationError(
                f"A notification fan-out is limited to {_MAX_RECIPIENTS} recipients."
            )
        now = self._now()
        requested_schedule = (
            None if scheduled_for is None else _aware_utc(scheduled_for, "scheduled_for")
        )
        rows: list[Notification] = []

        with self.session.begin_nested():
            for recipient in recipient_values:
                user = self.session.scalar(select(AppUser).where(AppUser.id == recipient).limit(1))
                if user is None:
                    raise NotFound(f"Notification recipient {recipient} was not found.")
                if not user.is_active:
                    row = self._new_row(
                        recipient,
                        resolved_channel,
                        resolved_template,
                        normalized_subject,
                        {},
                        now,
                        state=NotificationState.SUPPRESSED,
                        scheduled_for=now,
                        last_error=_SUPPRESSION_INACTIVE,
                        actor_id=actor,
                        request_id=resolved_request_id,
                    )
                    self.session.add(row)
                    rows.append(row)
                    self._audit_suppression(row, _SUPPRESSION_INACTIVE)
                    continue

                permissions = self._permissions_for(recipient)
                principal = Principal.user(recipient, permissions)
                scope = self._recipient_scope(principal)
                if normalized_subject[0] is not None and not self._subject_visible(
                    normalized_subject[0], normalized_subject[1], scope
                ):
                    row = self._new_row(
                        recipient,
                        resolved_channel,
                        resolved_template,
                        normalized_subject,
                        {},
                        now,
                        state=NotificationState.SUPPRESSED,
                        scheduled_for=now,
                        last_error=_SUPPRESSION_OUT_OF_SCOPE,
                        actor_id=actor,
                        request_id=resolved_request_id,
                    )
                    self.session.add(row)
                    rows.append(row)
                    self._audit_suppression(row, _SUPPRESSION_OUT_OF_SCOPE)
                    continue

                def can_disclose(
                    value: ScopedValue,
                    resolved_scope: Scope = scope,
                    resolved_permissions: frozenset[Permission] = permissions,
                ) -> bool:
                    return self._can_disclose(value, resolved_scope, resolved_permissions)

                filtered = resolved_template.filter_payload(
                    payload,
                    can_disclose=can_disclose,
                    permissions=permissions,
                )
                if not filtered.has_visible_content:
                    row = self._new_row(
                        recipient,
                        resolved_channel,
                        resolved_template,
                        normalized_subject,
                        {},
                        now,
                        state=NotificationState.SUPPRESSED,
                        scheduled_for=now,
                        last_error=_SUPPRESSION_EMPTY,
                        actor_id=actor,
                        request_id=resolved_request_id,
                    )
                    self.session.add(row)
                    rows.append(row)
                    self._audit_suppression(row, _SUPPRESSION_EMPTY)
                    continue

                resolved_template.render(
                    filtered,
                    allow_removed_slots=filtered.removed_slots,
                )
                storage_payload = dict(filtered.values)
                # Preserve the fact that a declared slot was intentionally
                # removed without persisting the denied value.  An empty
                # value remains type-valid for the second render at delivery
                # time and cannot disclose what policy removed.
                storage_payload.update({name: "" for name in filtered.removed_slots})
                preference = self._preference(recipient, resolved_template.name, resolved_channel)
                if not preference.allows(
                    template_non_suppressible=resolved_template.non_suppressible
                ):
                    row = self._new_row(
                        recipient,
                        resolved_channel,
                        resolved_template,
                        normalized_subject,
                        {},
                        now,
                        state=NotificationState.SUPPRESSED,
                        scheduled_for=now,
                        last_error=_SUPPRESSION_DISABLED,
                        actor_id=actor,
                        request_id=resolved_request_id,
                    )
                    self.session.add(row)
                    rows.append(row)
                    self._audit_suppression(row, _SUPPRESSION_DISABLED)
                    continue

                base_schedule = requested_schedule or now
                schedule = max(base_schedule, preference.schedule_for(base_schedule))
                row = self._new_row(
                    recipient,
                    resolved_channel,
                    resolved_template,
                    normalized_subject,
                    storage_payload,
                    now,
                    state=NotificationState.PENDING,
                    scheduled_for=schedule,
                    actor_id=actor,
                    request_id=resolved_request_id,
                )
                self.session.add(row)
                rows.append(row)
            self.session.flush()
        return tuple(rows)

    enqueue = queue
    queue_notification = queue

    def send_due(
        self, *, limit: int = _MAX_BATCH, now: datetime | None = None
    ) -> tuple[DeliveryOutcome, ...]:
        """Attempt due rows and persist every outcome explicitly."""

        notifier = self.notifier
        if notifier is None:
            raise ExternalServiceError("Notification delivery is not configured.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_BATCH:
            raise ValidationError(f"limit must be between 1 and {_MAX_BATCH}.", field="limit")
        current = self._now() if now is None else _aware_utc(now, "now")
        assert current is not None
        with self._delivery_lock:
            statement: Select[tuple[Notification]] = (
                select(Notification)
                .where(
                    Notification.state == NotificationState.PENDING.value,
                    Notification.scheduled_for.is_not(None),
                    Notification.scheduled_for <= current,
                )
                .order_by(Notification.scheduled_for, Notification.id)
                .limit(limit)
                .with_for_update()
            )
            rows = tuple(self.session.execute(statement).scalars().all())
            outcomes: list[DeliveryOutcome] = []
            for row in rows:
                outcome = self._send_one(row, current, notifier)
                self.session.flush()
                outcomes.append(outcome)
            return tuple(outcomes)

    deliver_due = send_due
    dispatch = send_due

    def list_dead_letters(self, *, limit: int = _MAX_BATCH) -> tuple[Notification, ...]:
        """Return webhook dead letters in deterministic delivery order.

        The notification row is the delivery record.  Reading it through this
        method gives an administrator a durable queue view without exposing a
        separate, process-local dead-letter store.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_BATCH:
            raise ValidationError(f"limit must be between 1 and {_MAX_BATCH}.", field="limit")
        statement: Select[tuple[Notification]] = (
            select(Notification)
            .where(
                Notification.channel == _WEBHOOK_CHANNEL,
                Notification.state == NotificationState.DEAD_LETTERED.value,
            )
            .order_by(Notification.dead_lettered_at, Notification.id)
            .limit(limit)
        )
        return tuple(self.session.execute(statement).scalars().all())

    dead_letters = list_dead_letters

    def list_delivery_status(
        self,
        *,
        recipient_id: UUID | str | None = None,
        state: NotificationState | str | None = None,
        limit: int = _MAX_BATCH,
    ) -> tuple[Notification, ...]:
        """Return durable webhook delivery rows for administrator views."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_BATCH:
            raise ValidationError(f"limit must be between 1 and {_MAX_BATCH}.", field="limit")
        conditions: list[ColumnElement[bool]] = [Notification.channel == _WEBHOOK_CHANNEL]
        if recipient_id is not None:
            conditions.append(Notification.recipient_id == _uuid(recipient_id, "recipient_id"))
        if state is not None:
            try:
                normalized_state = (
                    state if isinstance(state, NotificationState) else NotificationState(state)
                )
            except ValueError as error:
                allowed = ", ".join(item.value for item in NotificationState)
                raise ValidationError(f"state must be one of: {allowed}.", field="state") from error
            conditions.append(Notification.state == normalized_state.value)
        statement: Select[tuple[Notification]] = (
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at, Notification.id)
            .limit(limit)
        )
        return tuple(self.session.execute(statement).scalars().all())

    webhook_delivery_status = list_delivery_status

    def replay_dead_letter(
        self,
        notification_id: UUID | str,
        *,
        now: datetime | None = None,
    ) -> DeliveryOutcome:
        """Replay one dead letter exactly once, with a fresh retry window.

        A sent row is an idempotent no-op.  This is the duplicate guard for an
        operator who clicks replay after a delivery acknowledgement raced with
        the dead-letter view.  The row is locked before either decision and
        remains the sole durable record of the delivery state.
        """

        resolved_id = _uuid(notification_id, "notification_id")
        current = self._now() if now is None else _aware_utc(now, "now")
        assert current is not None
        with self._delivery_lock:
            row = self.session.scalar(
                select(Notification)
                .where(Notification.id == resolved_id)
                .with_for_update()
                .limit(1)
            )
            if row is None:
                raise NotFound(f"Notification {resolved_id} was not found.")
            if row.state == NotificationState.SENT.value:
                return DeliveryOutcome(
                    row,
                    DeliveryResult(
                        DeliveryStatus.SENT,
                        provider_message_id="already-delivered",
                    ),
                    NotificationState.SENT,
                )
            if row.state != NotificationState.DEAD_LETTERED.value:
                raise ValidationError(
                    f"Notification {resolved_id} is not a dead letter.",
                    field="notification_id",
                )
            if self.notifier is None:
                raise ExternalServiceError("Notification delivery is not configured.")

            # A manual replay is a new operator-authorised retry cycle, while
            # retaining the original row and its event identity.  The prior
            # failure remains available in the audit/dead-letter history; the
            # active row's attempt counter now controls this cycle.
            row.state = NotificationState.PENDING.value
            row.attempts = 0
            row.scheduled_for = current
            row.dead_lettered_at = None
            row.last_error = None
            row.updated_at = current
            outcome = self._send_one(row, current, self.notifier)
            self.session.flush()
            return outcome

    replay = replay_dead_letter

    def replay_dead_letters(
        self,
        *,
        limit: int = _MAX_BATCH,
        now: datetime | None = None,
    ) -> tuple[DeliveryOutcome, ...]:
        """Replay the oldest webhook dead letters without duplicating sent rows."""

        rows = self.list_dead_letters(limit=limit)
        return tuple(self.replay_dead_letter(row.id, now=now) for row in rows)

    def _send_one(
        self,
        row: Notification,
        now: datetime,
        notifier: Notifier,
    ) -> DeliveryOutcome:
        user = self.session.scalar(select(AppUser).where(AppUser.id == row.recipient_id).limit(1))
        if user is None or not user.is_active:
            self._suppress(row, _SUPPRESSION_INACTIVE, now)
            return DeliveryOutcome(row, None, NotificationState.SUPPRESSED, _SUPPRESSION_INACTIVE)

        try:
            template = self._template(row.template)
            channel = _channel(row.channel)
            preference = self._preference(row.recipient_id, template.name, channel)
        except ValidationError as error:
            reason = _error_text(error)
            self._suppress(row, reason, now)
            return DeliveryOutcome(row, None, NotificationState.SUPPRESSED, reason)
        if not preference.allows(template_non_suppressible=template.non_suppressible):
            self._suppress(row, _SUPPRESSION_DISABLED, now)
            return DeliveryOutcome(row, None, NotificationState.SUPPRESSED, _SUPPRESSION_DISABLED)
        deferred_until = preference.deferred_until(now)
        if deferred_until is not None:
            row.scheduled_for = deferred_until
            row.last_error = _QUIET_HOURS_DEFERRED
            row.updated_at = now
            return DeliveryOutcome(row, None, NotificationState.PENDING, _QUIET_HOURS_DEFERRED)

        subject_type = row.subject_type
        subject_id = row.subject_id
        if subject_type is not None:
            permissions = self._permissions_for(row.recipient_id)
            scope = self._recipient_scope(Principal.user(row.recipient_id, permissions))
            if not self._subject_visible(subject_type, subject_id, scope):
                self._suppress(row, _SUPPRESSION_OUT_OF_SCOPE, now)
                return DeliveryOutcome(
                    row, None, NotificationState.SUPPRESSED, _SUPPRESSION_OUT_OF_SCOPE
                )

        try:
            rendered = template.render(row.payload or {})
            message = OutboundMessage(
                recipient_id=row.recipient_id,
                channel=channel,
                template=template.name,
                subject=rendered.subject,
                body=rendered.body,
                payload=rendered.payload,
                subject_type=subject_type,
                subject_id=subject_id,
                scheduled_for=row.scheduled_for,
                attempt=row.attempts + 1,
            )
            result = notifier.send(message)
            if not isinstance(result, DeliveryResult):
                raise TypeError("Notifier.send must return DeliveryResult.")
        except TemplateRenderError as error:
            reason = _error_text(error)
            self._suppress(row, reason, now)
            return DeliveryOutcome(row, None, NotificationState.SUPPRESSED, reason)
        except Exception as delivery_error:
            result = DeliveryResult(DeliveryStatus.RETRY, error=_error_text(delivery_error))

        attempt = row.attempts + 1
        row.attempts = attempt
        if result.status is DeliveryStatus.SENT:
            row.state = NotificationState.SENT.value
            row.sent_at = now
            row.last_error = None
            row.updated_at = now
            return DeliveryOutcome(row, result, NotificationState.SENT)

        failure_reason = _error_text(result.error or "notification delivery failed")
        if result.status is DeliveryStatus.DEAD_LETTERED or attempt >= self.max_attempts:
            row.state = NotificationState.DEAD_LETTERED.value
            row.dead_lettered_at = now
            row.last_error = failure_reason
            row.updated_at = now
            self._alert_dead_letter(row, failure_reason)
            return DeliveryOutcome(
                row,
                result,
                NotificationState.DEAD_LETTERED,
                failure_reason,
            )

        delay = result.retry_after_seconds
        if delay is None:
            delay = min(self.retry_base_seconds * (2 ** (attempt - 1)), _MAX_RETRY_SECONDS)
        row.state = NotificationState.PENDING.value
        row.scheduled_for = now + timedelta(seconds=delay)
        row.last_error = failure_reason
        row.updated_at = now
        return DeliveryOutcome(row, result, NotificationState.PENDING, failure_reason)

    def _new_row(
        self,
        recipient_id: UUID,
        channel: NotificationChannel,
        template: NotificationTemplate,
        subject: tuple[str | None, UUID | None],
        payload: Mapping[str, object],
        now: datetime,
        *,
        state: NotificationState,
        scheduled_for: datetime,
        last_error: str | None = None,
        actor_id: UUID | None,
        request_id: str,
    ) -> Notification:
        subject_type, subject_id = subject
        return Notification(
            id=new_id(),
            recipient_id=recipient_id,
            channel=channel.value,
            template=template.name,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=dict(payload),
            state=state.value,
            scheduled_for=scheduled_for,
            attempts=0,
            last_error=None if last_error is None else _error_text(last_error),
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )

    def _resolve_recipient_ids(
        self,
        *,
        recipients: UUID | str | Iterable[UUID | str] | None,
        recipient_ids: UUID | str | Iterable[UUID | str] | None,
        recipient_id: UUID | str | None,
        recipient_roles: Iterable[str],
    ) -> tuple[UUID, ...]:
        resolved: list[UUID] = []
        seen: set[UUID] = set()
        for value in (
            *_as_uuid_values(recipients, "recipients"),
            *_as_uuid_values(recipient_ids, "recipient_ids"),
        ):
            if value not in seen:
                resolved.append(value)
                seen.add(value)
        if recipient_id is not None:
            value = _uuid(recipient_id, "recipient_id")
            if value not in seen:
                resolved.append(value)
                seen.add(value)
        if isinstance(recipient_roles, str):
            raise ValidationError("recipient_roles must be an iterable of role names.")
        roles = tuple(_role(value) for value in recipient_roles)
        if roles:
            role_statement = (
                select(AppUser.id)
                .join(UserRole, UserRole.user_id == AppUser.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(AppUser.is_active.is_(True), Role.code.in_(roles))
                .order_by(AppUser.id)
            )
            for value in self.session.execute(role_statement).scalars().all():
                if value not in seen:
                    resolved.append(value)
                    seen.add(value)
        if not resolved:
            raise ValidationError(
                "At least one notification recipient is required.", field="recipients"
            )
        return tuple(resolved)

    def _preference(
        self,
        user_id: UUID,
        template: str,
        channel: NotificationChannel,
    ) -> NotificationPreference:
        row = self.session.scalar(
            select(NotificationPreferenceRow)
            .where(
                NotificationPreferenceRow.user_id == user_id,
                NotificationPreferenceRow.template == template,
                NotificationPreferenceRow.channel == channel.value,
            )
            .limit(1)
        )
        if row is None:
            return default_preference(user_id, template, channel)
        return NotificationPreference(
            user_id=row.user_id,
            template=row.template,
            channel=row.channel,
            enabled=row.enabled,
            quiet_hours_start=row.quiet_hours_start,
            quiet_hours_end=row.quiet_hours_end,
            digest_frequency=row.digest_frequency or "immediate",
        )

    def set_preference(
        self,
        user_id: UUID,
        template: str,
        channel: NotificationChannel | str,
        *,
        enabled: bool = True,
        quiet_hours_start: time | None = None,
        quiet_hours_end: time | None = None,
        digest_frequency: str = "immediate",
        actor_id: UUID | None = None,
        request_id: str | None = None,
    ) -> NotificationPreference:
        """Create or replace one preference row with optimistic versioning."""

        normalized_user = _uuid(user_id, "user_id")
        if (
            self.session.scalar(select(AppUser.id).where(AppUser.id == normalized_user).limit(1))
            is None
        ):
            raise NotFound(f"Notification preference user {normalized_user} was not found.")
        normalized_channel = _channel(channel)
        preference = NotificationPreference(
            normalized_user,
            template,
            normalized_channel,
            enabled=enabled,
            quiet_hours_start=quiet_hours_start,
            quiet_hours_end=quiet_hours_end,
            digest_frequency=digest_frequency,
        )
        start = preference.quiet_hours_start
        end = preference.quiet_hours_end
        existing = self.session.scalar(
            select(NotificationPreferenceRow)
            .where(
                NotificationPreferenceRow.user_id == normalized_user,
                NotificationPreferenceRow.template == preference.template,
                NotificationPreferenceRow.channel == normalized_channel.value,
            )
            .with_for_update()
            .limit(1)
        )
        now = self._now()
        actor = _uuid(actor_id, "actor_id") if actor_id is not None else None
        if existing is None:
            row = NotificationPreferenceRow(
                id=new_id(),
                user_id=normalized_user,
                template=preference.template,
                channel=normalized_channel.value,
                enabled=preference.enabled,
                quiet_hours_start=start,
                quiet_hours_end=end,
                digest_frequency=_frequency_value(preference.digest_frequency),
                version=1,
                created_at=now,
                updated_at=now,
                created_by_id=actor,
                updated_by_id=actor,
                request_id=_request_id(request_id or self.request_id),
            )
            self.session.add(row)
        else:
            existing.enabled = preference.enabled
            existing.quiet_hours_start = start
            existing.quiet_hours_end = end
            existing.digest_frequency = _frequency_value(preference.digest_frequency)
            existing.version += 1
            existing.updated_at = now
            existing.updated_by_id = actor
            existing.request_id = _request_id(request_id or self.request_id)
        self.session.flush()
        return preference

    update_preference = set_preference

    def _recipient_scope(self, principal: Principal) -> Scope:
        scope = self.scope_resolver(principal)
        if not isinstance(scope, Scope) or scope.principal_id != principal.id:
            raise ValidationError("Recipient scope does not belong to the recipient.")
        return scope

    def _permissions_for(self, user_id: UUID) -> frozenset[Permission]:
        try:
            values = self.permission_resolver(user_id)
            return frozenset(coerce_permission(value) for value in values)
        except (TypeError, ValueError) as error:
            raise ValidationError(f"Recipient permissions are invalid: {error}.") from error

    def _database_permissions(self, user_id: UUID) -> frozenset[Permission]:
        from covenant_radar.db.models.identity import Permission as PermissionRow

        statement = (
            select(PermissionRow.code)
            .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
        )
        return frozenset(
            coerce_permission(value) for value in self.session.execute(statement).scalars()
        )

    def _can_disclose(
        self,
        value: ScopedValue,
        scope: Scope,
        permissions: frozenset[Permission],
    ) -> bool:
        if value.required_permission is not None and value.required_permission not in permissions:
            return False
        if value.subject_type is not None and value.subject_id is not None:
            if not self._subject_visible(value.subject_type, value.subject_id, scope):
                return False
        if value.portfolio_path is not None:
            path = value.portfolio_path
            if not path.endswith("/"):
                path += "/"
            if path not in scope.exact_paths and not any(
                path.startswith(prefix) for prefix in scope.descendant_paths
            ):
                return False
        return True

    def _subject_visible(self, subject_type: str, subject_id: UUID | None, scope: Scope) -> bool:
        if subject_id is None:
            return False
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

    def _audit_suppression(self, row: Notification, reason: str) -> None:
        if self.audit is None:
            return
        self.audit.record(
            "notification_suppressed",
            ("notification", row.id),
            {
                "notification_id": str(row.id),
                "recipient_id": str(row.recipient_id),
                "template": row.template,
                "channel": row.channel,
                "reason": reason,
            },
            actor=row.recipient_id,
            request_id=row.request_id,
        )

    def _suppress(self, row: Notification, reason: str, now: datetime) -> None:
        row.state = NotificationState.SUPPRESSED.value
        row.last_error = _error_text(reason)
        row.updated_at = now
        self._audit_suppression(row, reason)

    def _alert_dead_letter(self, row: Notification, reason: str) -> None:
        """Run the alert hook without making durable dead-lettering fragile."""

        if self.dead_letter_alert is None:
            return
        try:
            self.dead_letter_alert(row, reason)
        except Exception as error:
            # The row has already transitioned to dead-lettered.  An alerting
            # outage must not roll that state back or make the event disappear
            # from the administrator's delivery view.
            row.last_error = _error_text(
                f"{reason}; dead-letter alert failed: {type(error).__name__}"
            )

    def _template(self, value: str | NotificationTemplate) -> NotificationTemplate:
        if isinstance(value, NotificationTemplate):
            return value
        return self.template_registry.get(value)

    def _now(self) -> datetime:
        value = self.clock.now()
        current = _aware_utc(value, "clock.now()")
        if current is None:
            raise ValidationError("clock.now() must return a timezone-aware datetime.")
        return current


def _subject(
    subject_type: str | None, subject_id: UUID | str | None
) -> tuple[str | None, UUID | None]:
    if subject_type is None and subject_id is None:
        return None, None
    if subject_type is None or subject_id is None:
        raise ValidationError(
            "subject_type and subject_id must be supplied together.", field="subject"
        )
    if not isinstance(subject_type, str) or not subject_type.strip():
        raise ValidationError("subject_type must be non-blank text.", field="subject_type")
    return subject_type.strip().lower(), _uuid(subject_id, "subject_id")


def _as_uuid_values(
    value: UUID | str | Iterable[UUID | str] | None,
    field_name: str,
) -> tuple[UUID, ...]:
    if value is None:
        return ()
    if isinstance(value, UUID | str):
        return (_uuid(value, field_name),)
    if isinstance(value, bytes | bytearray) or not isinstance(value, Iterable):
        raise ValidationError(
            f"{field_name} must be a UUID or iterable of UUIDs.", field=field_name
        )
    result: list[UUID] = []
    for item in value:
        result.append(_uuid(item, field_name))
    return tuple(result)


def _uuid(value: UUID | str, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a UUID.", field=field_name)
    try:
        return UUID(value)
    except ValueError as error:
        raise ValidationError(f"{field_name} must be a valid UUID.", field=field_name) from error


def _role(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 50:
        raise ValidationError("recipient role must be non-blank text of at most 50 characters.")
    return value.strip()


def _channel(value: NotificationChannel | str) -> NotificationChannel:
    if isinstance(value, NotificationChannel):
        return value
    if not isinstance(value, str):
        raise ValidationError("channel must be text.", field="channel")
    try:
        return NotificationChannel(value.strip().lower())
    except ValueError as error:
        allowed = ", ".join(item.value for item in NotificationChannel)
        raise ValidationError(f"channel must be one of: {allowed}.", field="channel") from error


def _aware_utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware.", field=field_name)
    return value.astimezone(UTC)


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _REQUEST_ID_MAX_LENGTH:
        raise ValidationError(
            f"request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters.",
            field="request_id",
        )
    if not value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValidationError(
            "request_id must be non-blank text without control characters.", field="request_id"
        )
    return value


def _error_text(value: object) -> str:
    text = str(value).strip() or "notification delivery failed"
    return text[:_LAST_ERROR_MAX_LENGTH]


def _frequency_value(value: DigestFrequency | str) -> str:
    return value.value if isinstance(value, DigestFrequency) else value


__all__ = ["AuditWriter", "DeliveryOutcome", "NotificationService"]
