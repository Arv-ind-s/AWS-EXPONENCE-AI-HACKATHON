"""Bundled email digests for the notification queue.

The notification service deliberately queues one durable row per disclosure.
This module is the delivery-side projection of those rows: it groups eligible
email rows by recipient and time window, renders each row through the same
versioned notification template used by the other channels, and produces one
immutable digest per recipient.  It does not merge or rewrite notification
rows, so every item remains independently reconstructable.

``DigestDeliveryService`` is an optional database-backed dispatcher for
deployments that want to send a whole window in one operation.  It applies the
same explicit retry/dead-letter state transitions as the notification service
after the bundled adapter has reported an outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from email.utils import parseaddr
from threading import RLock
from typing import Final, Protocol
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.errors import ExternalServiceError, ValidationError
from covenant_radar.db.models import AppUser, Notification
from covenant_radar.db.session import is_database_session
from covenant_radar.notifications.model import (
    NotificationState,
    NotificationTemplate,
    TemplateRegistry,
    TemplateRenderError,
)
from covenant_radar.notifications.templates import DEFAULT_TEMPLATE_REGISTRY
from covenant_radar.ports.notifier import DeliveryResult, DeliveryStatus

_MAX_BASE_URL_LENGTH: Final[int] = 2_048
_MAX_EMAIL_LENGTH: Final[int] = 320
_MAX_ENTRIES: Final[int] = 10_000
_MAX_RETRY_SECONDS: Final[int] = 3_600
_DEFAULT_RETRY_BASE_SECONDS: Final[int] = 30
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_EMAIL_CHANNEL: Final[str] = "email"
_PENDING_STATE: Final[str] = NotificationState.PENDING.value
_SMTP_FALLBACK_TEMPLATE: Final[str] = "morning_queue"


class DigestAssemblyError(ValidationError):
    """A queued notification cannot be safely included in a digest."""


class DigestSender(Protocol):
    """The portion of the email adapter required by the dispatcher."""

    def send_digest(self, digest: EmailDigest) -> DeliveryResult:
        """Send one already-assembled recipient digest."""
        ...


@dataclass(frozen=True, slots=True)
class DigestWindow:
    """A half-open, timezone-aware UTC interval used for bundling."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = _utc(self.start, "window.start")
        end = _utc(self.end, "window.end")
        if end <= start:
            raise ValidationError("Digest window end must be after its start.", field="window")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @classmethod
    def for_day(
        cls,
        day: date,
        *,
        delivery_time: time = time(9, 0),
        timezone: tzinfo | None = None,
    ) -> DigestWindow:
        """Build the morning window ending at *delivery_time* on *day*.

        The window begins at the previous occurrence of the same local
        delivery time.  A timezone must be supplied explicitly for a local
        morning window; this avoids silently interpreting a deployment's
        calendar in the host timezone.
        """

        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValidationError("day must be a calendar date.", field="day")
        if not isinstance(delivery_time, time) or delivery_time.tzinfo is not None:
            raise ValidationError(
                "delivery_time must be a naive time of day.", field="delivery_time"
            )
        if timezone is None:
            raise ValidationError(
                "timezone is required for a local digest window.", field="timezone"
            )
        end_local = datetime.combine(day, delivery_time, tzinfo=timezone)
        return cls(end_local - timedelta(days=1), end_local)


@dataclass(frozen=True, slots=True)
class DigestEntry:
    """One independently traceable item inside a bundled email."""

    notification_id: UUID
    template: str
    title: str
    summary: str
    deep_link: str
    occurred_at: datetime
    subject_type: str | None = None
    subject_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.notification_id, UUID):
            raise TypeError("DigestEntry.notification_id must be a UUID.")
        if not isinstance(self.template, str) or not self.template.strip():
            raise ValidationError("DigestEntry.template must be non-blank text.", field="template")
        for field_name, value in (("title", self.title), ("summary", self.summary)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"DigestEntry.{field_name} must be non-blank text.", field=field_name
                )
        if not isinstance(self.deep_link, str) or not self.deep_link.strip():
            raise ValidationError(
                "DigestEntry.deep_link must be non-blank text.", field="deep_link"
            )
        occurred_at = _utc(self.occurred_at, "occurred_at")
        if (self.subject_type is None) != (self.subject_id is None):
            raise ValidationError(
                "DigestEntry subject_type and subject_id must be supplied together.",
                field="subject",
            )
        object.__setattr__(self, "template", self.template.strip())
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "deep_link", self.deep_link.strip())
        object.__setattr__(self, "occurred_at", occurred_at)
        if self.subject_type is not None:
            if not isinstance(self.subject_type, str) or not self.subject_type.strip():
                raise ValidationError("DigestEntry.subject_type must be non-blank text.")
            object.__setattr__(self, "subject_type", self.subject_type.strip().lower())


@dataclass(frozen=True, slots=True)
class EmailDigest:
    """The complete, immutable input to one bundled email."""

    recipient_id: UUID
    recipient_email: str
    window: DigestWindow
    entries: tuple[DigestEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.recipient_id, UUID):
            raise TypeError("EmailDigest.recipient_id must be a UUID.")
        object.__setattr__(self, "recipient_email", normalize_email_address(self.recipient_email))
        if not isinstance(self.window, DigestWindow):
            raise TypeError("EmailDigest.window must be a DigestWindow.")
        entries = tuple(self.entries)
        if not entries:
            raise ValidationError(
                "An email digest must contain at least one entry.", field="entries"
            )
        if len(entries) > _MAX_ENTRIES:
            raise ValidationError(
                f"An email digest cannot contain more than {_MAX_ENTRIES} entries.",
                field="entries",
            )
        if any(not isinstance(entry, DigestEntry) for entry in entries):
            raise TypeError("EmailDigest.entries must contain DigestEntry values.")
        ids = tuple(entry.notification_id for entry in entries)
        if len(ids) != len(set(ids)):
            raise ValidationError(
                "A digest cannot contain a notification more than once.", field="entries"
            )
        object.__setattr__(self, "entries", entries)

    @property
    def subject(self) -> str:
        """Return a stable subject that avoids recipient-specific disclosure."""

        return f"Covenant Radar morning queue — {len(self.entries)} item(s)"

    @property
    def notification_ids(self) -> tuple[UUID, ...]:
        """Return the durable rows represented by this email."""

        return tuple(entry.notification_id for entry in self.entries)


RecipientLookup = Mapping[UUID, str] | Callable[[UUID], str | None]


class DigestAssembler:
    """Group pending email notifications into one digest per recipient."""

    def __init__(
        self,
        session: Session | None = None,
        *,
        template_registry: TemplateRegistry | None = None,
        base_url: str = "",
        recipient_resolver: RecipientLookup | None = None,
        max_entries: int = _MAX_ENTRIES,
    ) -> None:
        if session is not None and not is_database_session(session):
            raise TypeError("DigestAssembler session must be a SQLAlchemy Session.")
        if template_registry is not None and not isinstance(template_registry, TemplateRegistry):
            raise TypeError("template_registry must be a TemplateRegistry.")
        if recipient_resolver is not None and not isinstance(recipient_resolver, Mapping):
            if not callable(recipient_resolver):
                raise TypeError("recipient_resolver must be a mapping or callable.")
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or not 1 <= max_entries <= _MAX_ENTRIES
        ):
            raise ValidationError(
                f"max_entries must be between 1 and {_MAX_ENTRIES}.", field="max_entries"
            )
        self.session = session
        self.template_registry = template_registry or DEFAULT_TEMPLATE_REGISTRY
        self.base_url = _base_url(base_url)
        self.recipient_resolver = recipient_resolver
        self.max_entries = max_entries

    def assemble(
        self,
        notifications: Iterable[object] | DigestWindow | None = None,
        *,
        window: DigestWindow | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        recipients: RecipientLookup | None = None,
        recipient_emails: RecipientLookup | None = None,
        lock_for_update: bool = False,
    ) -> tuple[EmailDigest, ...]:
        """Assemble the supplied rows, or query the configured session.

        ``window_start`` is inclusive and ``window_end`` is exclusive.  A
        digest never includes a row outside that interval, which makes a
        rerun with the same window deterministic and prevents boundary rows
        from being sent twice by adjacent windows.
        """

        if not isinstance(lock_for_update, bool):
            raise TypeError("lock_for_update must be a boolean.")
        if isinstance(notifications, DigestWindow):
            if window is not None:
                raise ValidationError("Specify a digest window only once.", field="window")
            window = notifications
            notifications = None
        resolved_window = _resolve_window(window, window_start, window_end)
        lookup = (
            recipient_emails
            if recipient_emails is not None
            else recipients
            if recipients is not None
            else self.recipient_resolver
        )
        if notifications is None:
            notifications = self._query_notifications(
                resolved_window, lock_for_update=lock_for_update
            )
        rows = tuple(notifications)
        grouped: dict[UUID, list[DigestEntry]] = {}
        eligible_count = 0
        for row in rows:
            if not _eligible(row, resolved_window):
                continue
            eligible_count += 1
            if eligible_count > self.max_entries:
                raise DigestAssemblyError(
                    f"Digest assembly is limited to {self.max_entries} notification entries.",
                    field="notifications",
                )
            entry = self._entry(row)
            grouped.setdefault(_row_uuid(row, "recipient_id"), []).append(entry)

        if not grouped:
            return ()
        resolved_emails = self._recipient_emails(tuple(grouped), lookup)
        digests: list[EmailDigest] = []
        for recipient_id in sorted(grouped, key=str):
            entries = tuple(
                sorted(
                    grouped[recipient_id],
                    key=lambda item: (item.occurred_at, str(item.notification_id)),
                )
            )
            digests.append(
                EmailDigest(
                    recipient_id=recipient_id,
                    recipient_email=resolved_emails[recipient_id],
                    window=resolved_window,
                    entries=entries,
                )
            )
        return tuple(digests)

    build = assemble
    assemble_digests = assemble

    def _query_notifications(
        self, window: DigestWindow, *, lock_for_update: bool = False
    ) -> tuple[Notification, ...]:
        if self.session is None:
            raise ValidationError(
                "notifications are required when DigestAssembler has no session.",
                field="notifications",
            )
        statement: Select[tuple[Notification]] = (
            select(Notification)
            .where(
                Notification.channel == _EMAIL_CHANNEL,
                Notification.state == _PENDING_STATE,
                Notification.scheduled_for.is_not(None),
                Notification.scheduled_for >= window.start,
                Notification.scheduled_for < window.end,
            )
            .order_by(Notification.scheduled_for, Notification.id)
            .limit(self.max_entries + 1)
        )
        if lock_for_update:
            statement = statement.with_for_update()
        return tuple(self.session.execute(statement).scalars().all())

    def _recipient_emails(
        self,
        recipient_ids: tuple[UUID, ...],
        lookup: RecipientLookup | None,
    ) -> dict[UUID, str]:
        if lookup is None and self.session is None:
            raise DigestAssemblyError(
                "Recipient email addresses are required to assemble an email digest.",
                field="recipients",
            )
        if lookup is None:
            session = self.session
            if session is None:
                raise DigestAssemblyError(
                    "Recipient email addresses are required to assemble an email digest.",
                    field="recipients",
                )
            rows = session.execute(
                select(AppUser.id, AppUser.email).where(AppUser.id.in_(recipient_ids))
            ).all()
            lookup = {recipient_id: email for recipient_id, email in rows}
        resolved: dict[UUID, str] = {}
        for recipient_id in recipient_ids:
            try:
                raw = lookup[recipient_id] if isinstance(lookup, Mapping) else lookup(recipient_id)
            except KeyError as error:
                raise DigestAssemblyError(
                    f"No email address is available for recipient {recipient_id}.",
                    field="recipients",
                ) from error
            if raw is None:
                raise DigestAssemblyError(
                    f"No email address is available for recipient {recipient_id}.",
                    field="recipients",
                )
            resolved[recipient_id] = normalize_email_address(raw)
        return resolved

    def _entry(self, row: object) -> DigestEntry:
        notification_id = _row_uuid(row, "id")
        template_name = _row_text(row, "template")
        try:
            template: NotificationTemplate = self.template_registry.get(template_name)
            rendered = template.render(_row_payload(row))
        except (TemplateRenderError, TypeError, ValueError) as error:
            raise DigestAssemblyError(
                f"Notification {notification_id} cannot be rendered for email delivery.",
                field="payload",
            ) from error
        subject_type = _row_optional_text(row, "subject_type")
        subject_id = _row_optional_uuid(row, "subject_id")
        if (subject_type is None) != (subject_id is None):
            raise DigestAssemblyError(
                f"Notification {notification_id} has an incomplete subject reference.",
                field="subject",
            )
        occurred_at = _row_datetime(row, "scheduled_for") or _row_datetime(row, "created_at")
        if occurred_at is None:
            raise DigestAssemblyError(
                f"Notification {notification_id} has no scheduling timestamp.",
                field="scheduled_for",
            )
        return DigestEntry(
            notification_id=notification_id,
            template=template.name,
            title=rendered.subject,
            summary=rendered.body,
            deep_link=deep_link(
                subject_type,
                subject_id,
                rendered.payload,
                base_url=self.base_url,
            ),
            occurred_at=occurred_at,
            subject_type=subject_type,
            subject_id=subject_id,
        )


@dataclass(frozen=True, slots=True)
class DigestDeliveryOutcome:
    """The result applied to every row represented by one digest."""

    digest: EmailDigest
    result: DeliveryResult
    state: NotificationState
    reason: str | None = None


class DigestDeliveryService:
    """Send a window and persist one explicit outcome for each source row."""

    def __init__(
        self,
        session: Session,
        sender: DigestSender,
        *,
        assembler: DigestAssembler | None = None,
        clock: Clock | None = None,
        retry_base_seconds: int = _DEFAULT_RETRY_BASE_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        dead_letter_alert: Callable[[Notification, str], object] | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("DigestDeliveryService requires a SQLAlchemy Session.")
        if not callable(getattr(sender, "send_digest", None)):
            raise TypeError("sender must expose send_digest(digest).")
        if assembler is not None and assembler.session is not session:
            raise ValueError("assembler and delivery service must use the same session.")
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
        self.sender = sender
        self.assembler = assembler or DigestAssembler(session)
        self.clock = clock or SystemClock()
        self.retry_base_seconds = retry_base_seconds
        self.max_attempts = max_attempts
        self.dead_letter_alert = dead_letter_alert
        self._lock = RLock()

    def send_window(
        self,
        window: DigestWindow,
        *,
        recipients: RecipientLookup | None = None,
        recipient_emails: RecipientLookup | None = None,
    ) -> tuple[DigestDeliveryOutcome, ...]:
        """Bundle and send one window, leaving empty windows untouched."""

        if not isinstance(window, DigestWindow):
            raise TypeError("window must be a DigestWindow.")
        now = _utc(self.clock.now(), "clock.now()")
        with self._lock:
            digests = self.assembler.assemble(
                window=window,
                recipients=recipients,
                recipient_emails=recipient_emails,
                lock_for_update=True,
            )
            outcomes: list[DigestDeliveryOutcome] = []
            for digest in digests:
                try:
                    result = self.sender.send_digest(digest)
                    if not isinstance(result, DeliveryResult):
                        raise TypeError("sender.send_digest must return DeliveryResult.")
                except Exception as error:
                    result = DeliveryResult(DeliveryStatus.RETRY, error=_error_text(error))
                outcomes.append(self._persist_outcome(digest, result, now))
            self.session.flush()
            return tuple(outcomes)

    dispatch = send_window
    deliver = send_window

    def _persist_outcome(
        self,
        digest: EmailDigest,
        result: DeliveryResult,
        now: datetime,
    ) -> DigestDeliveryOutcome:
        rows_list: list[Notification] = []
        notification_ids = digest.notification_ids
        for offset in range(0, len(notification_ids), 500):
            chunk = notification_ids[offset : offset + 500]
            rows_list.extend(
                self.session.execute(
                    select(Notification).where(Notification.id.in_(chunk)).with_for_update()
                )
                .scalars()
                .all()
            )
        rows = tuple(rows_list)
        by_id = {row.id: row for row in rows}
        missing = [str(row_id) for row_id in digest.notification_ids if row_id not in by_id]
        if missing:
            raise ExternalServiceError(
                f"Digest source notification disappeared during delivery: {missing[0]}"
            )
        not_pending = [str(row.id) for row in rows if row.state != NotificationState.PENDING.value]
        if not_pending:
            raise ExternalServiceError(
                f"Digest source notification is no longer pending: {not_pending[0]}"
            )
        attempt = max(by_id[row_id].attempts for row_id in digest.notification_ids) + 1
        reason = (
            None
            if result.status is DeliveryStatus.SENT
            else _error_text(result.error or "email digest delivery failed")
        )
        if result.status is DeliveryStatus.SENT:
            for row in by_id.values():
                row.attempts = attempt
                row.state = NotificationState.SENT.value
                row.sent_at = now
                row.last_error = None
                row.updated_at = now
            return DigestDeliveryOutcome(digest, result, NotificationState.SENT)

        if result.status is DeliveryStatus.DEAD_LETTERED or attempt >= self.max_attempts:
            failure_reason = reason or "email digest delivery failed"
            for row in by_id.values():
                row.attempts = attempt
                row.state = NotificationState.DEAD_LETTERED.value
                row.dead_lettered_at = now
                row.last_error = failure_reason
                row.updated_at = now
                if self.dead_letter_alert is not None:
                    self.dead_letter_alert(row, failure_reason)
            return DigestDeliveryOutcome(
                digest,
                result,
                NotificationState.DEAD_LETTERED,
                failure_reason,
            )

        delay = result.retry_after_seconds
        if delay is None:
            delay = min(self.retry_base_seconds * (2 ** (attempt - 1)), _MAX_RETRY_SECONDS)
        for row in by_id.values():
            row.attempts = attempt
            row.state = NotificationState.PENDING.value
            row.scheduled_for = now + timedelta(seconds=delay)
            row.last_error = reason or "email digest delivery failed"
            row.updated_at = now
        return DigestDeliveryOutcome(
            digest,
            result,
            NotificationState.PENDING,
            reason or "email digest delivery failed",
        )


def normalize_email_address(value: object) -> str:
    """Validate one mailbox address before it can enter an SMTP header."""

    if not isinstance(value, str):
        raise DigestAssemblyError("Recipient email must be text.", field="email")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_EMAIL_LENGTH
        or "\r" in normalized
        or "\n" in normalized
    ):
        raise DigestAssemblyError("Recipient email is invalid.", field="email")
    display_name, address = parseaddr(normalized)
    if display_name or address != normalized or "@" not in address:
        raise DigestAssemblyError("Recipient email must be a bare mailbox address.", field="email")
    local, domain = address.rsplit("@", 1)
    if (
        not local
        or not domain
        or local.endswith(".")
        or domain.startswith(".")
        or domain.endswith(".")
    ):
        raise DigestAssemblyError("Recipient email is invalid.", field="email")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in address
    ):
        raise DigestAssemblyError("Recipient email contains invalid characters.", field="email")
    return normalized


def deep_link(
    subject_type: str | None,
    subject_id: UUID | None,
    payload: Mapping[str, object],
    *,
    base_url: str = "",
) -> str:
    """Build a same-product link from a subject reference.

    References supplied by a notification payload are used only as path
    components and are always quoted.  If a subject has no route-specific
    human reference, the queue route remains a valid, non-disclosing landing
    page carrying the opaque subject identifiers as query parameters.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping.")
    if (subject_type is None) != (subject_id is None):
        raise ValidationError(
            "subject_type and subject_id must be supplied together.", field="subject"
        )
    if subject_type is not None and not isinstance(subject_type, str):
        raise ValidationError("subject_type must be text.", field="subject_type")
    if subject_id is not None and not isinstance(subject_id, UUID):
        raise ValidationError("subject_id must be a UUID.", field="subject_id")
    prefix = _base_url(base_url)
    if subject_type is None or subject_id is None:
        path = "/queue"
    else:
        normalized_type = subject_type.strip().lower() if isinstance(subject_type, str) else ""
        reference = _payload_reference(payload, normalized_type)
        if normalized_type == "borrower":
            path = f"/borrowers/{quote(reference or str(subject_id), safe='')}"
        elif normalized_type == "case":
            path = f"/cases/{quote(reference or str(subject_id), safe='')}"
        elif normalized_type in {"covenant", "covenant_version"}:
            path = f"/covenants/{quote(reference or str(subject_id), safe='')}"
        elif normalized_type == "job":
            path = "/admin/jobs"
        else:
            query = urlencode(
                {"subject_type": normalized_type, "subject_id": str(subject_id)},
                quote_via=quote,
            )
            path = f"/queue?{query}"
    if not prefix:
        return path
    parsed = urlsplit(prefix)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + path, "", ""))


def _payload_reference(payload: Mapping[str, object], subject_type: str) -> str | None:
    keys = {
        "borrower": ("borrower_reference", "reference"),
        "case": ("case_reference", "reference"),
        "covenant": ("covenant_reference", "reference"),
        "covenant_version": ("covenant_reference", "reference"),
    }.get(subject_type, ("reference",))
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_window(
    window: DigestWindow | None,
    start: datetime | None,
    end: datetime | None,
) -> DigestWindow:
    if window is not None and (start is not None or end is not None):
        raise ValidationError(
            "Specify a digest window either as an object or as bounds.", field="window"
        )
    if window is not None:
        if not isinstance(window, DigestWindow):
            raise TypeError("window must be a DigestWindow.")
        return window
    if start is None or end is None:
        raise ValidationError("window_start and window_end are required.", field="window")
    return DigestWindow(start, end)


def _eligible(row: object, window: DigestWindow) -> bool:
    if _row_text(row, "channel").strip().lower() != _EMAIL_CHANNEL:
        return False
    if _row_text(row, "state").strip().lower() != _PENDING_STATE:
        return False
    scheduled = _row_datetime(row, "scheduled_for") or _row_datetime(row, "created_at")
    return scheduled is not None and window.start <= scheduled < window.end


def _row_payload(row: object) -> Mapping[str, object]:
    payload = getattr(row, "payload", None)
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise DigestAssemblyError("Notification payload must be a mapping.", field="payload")
    return payload


def _row_uuid(row: object, field_name: str) -> UUID:
    value = getattr(row, field_name, None)
    if isinstance(value, UUID):
        return value
    raise DigestAssemblyError(f"Notification {field_name} must be a UUID.", field=field_name)


def _row_optional_uuid(row: object, field_name: str) -> UUID | None:
    value = getattr(row, field_name, None)
    if value is None:
        return None
    return _row_uuid(row, field_name)


def _row_text(row: object, field_name: str) -> str:
    value = getattr(row, field_name, None)
    if not isinstance(value, str) or not value.strip():
        raise DigestAssemblyError(
            f"Notification {field_name} must be non-blank text.", field=field_name
        )
    return value


def _row_optional_text(row: object, field_name: str) -> str | None:
    value = getattr(row, field_name, None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DigestAssemblyError(
            f"Notification {field_name} must be non-blank text.", field=field_name
        )
    return value.strip().lower()


def _row_datetime(row: object, field_name: str) -> datetime | None:
    value = getattr(row, field_name, None)
    if value is None:
        return None
    return _utc(value, field_name)


def _row_error(value: object) -> str:
    return (
        str(value).replace("\r", " ").replace("\n", " ").strip()[:2_000]
        or "email digest delivery failed"
    )


def _error_text(value: object) -> str:
    return _row_error(value)


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware.", field=field_name)
    return value.astimezone(UTC)


def _base_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("base_url must be text.", field="base_url")
    normalized = value.strip()
    if not normalized:
        return ""
    if len(normalized) > _MAX_BASE_URL_LENGTH or any(char in normalized for char in "\r\n"):
        raise ValidationError("base_url is invalid.", field="base_url")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(
            "base_url must be an HTTP(S) origin without credentials.", field="base_url"
        )
    return normalized.rstrip("/")


__all__ = [
    "DigestAssembler",
    "DigestAssemblyError",
    "DigestDeliveryOutcome",
    "DigestDeliveryService",
    "DigestEntry",
    "DigestSender",
    "DigestWindow",
    "EmailDigest",
    "deep_link",
    "normalize_email_address",
]
