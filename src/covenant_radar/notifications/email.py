"""SMTP email delivery for notification digests.

The adapter accepts only fully rendered messages and never loads application
records.  SMTP is deliberately created per send, which avoids sharing a
mutable connection across scheduler workers and ensures a broken connection
cannot poison the next delivery.  All failures become explicit
``DeliveryResult`` values for the durable retry/dead-letter policy.
"""

from __future__ import annotations

import html
import smtplib
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from importlib import resources
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

from jinja2 import DictLoader, Environment, StrictUndefined, select_autoescape

from covenant_radar.config.settings import NotificationsSettings
from covenant_radar.notifications.digest import (
    EmailDigest,
    normalize_email_address,
)
from covenant_radar.ports.notifier import (
    DeliveryResult,
    DeliveryStatus,
    NotificationChannel,
    Notifier,
    OutboundMessage,
)

_MAX_HOST_LENGTH: Final[int] = 255
_MAX_TIMEOUT_SECONDS: Final[float] = 120.0
_MAX_USERNAME_LENGTH: Final[int] = 320
_MAX_PROVIDER_ERROR_LENGTH: Final[int] = 2_000
_SMTP_UNCONFIGURED: Final[str] = (
    "SMTP is not configured; email remains queued for in-app delivery. "
    "Configure notifications.smtp_host and notifications.smtp_sender."
)
_SMTP_AUTH_FAILED: Final[str] = "SMTP authentication failed; inspect the configured credentials."
_SMTP_PERMANENT_FAILURE: Final[str] = "SMTP rejected the message permanently."
_SMTP_FALLBACK_TEMPLATE: Final[str] = "morning_queue"


class SmtpClient(Protocol):
    """The subset of ``smtplib`` used by :class:`EmailNotifier`."""

    def ehlo(self) -> object:
        """Negotiate ESMTP capabilities."""
        ...

    def starttls(self) -> object:
        """Upgrade a plain connection to TLS."""
        ...

    def login(self, username: str, password: str) -> object:
        """Authenticate to the relay."""
        ...

    def send_message(self, message: EmailMessage) -> Mapping[str, tuple[int, bytes]]:
        """Submit one message and return refused recipients, if any."""
        ...

    def quit(self) -> object:
        """Close the connection."""
        ...


SmtpFactory = Callable[..., SmtpClient]
RecipientResolver = Mapping[UUID, str] | Callable[[UUID], str | None]


@dataclass(frozen=True, slots=True)
class EmailCapability:
    """Administrator-facing configuration status for SMTP."""

    configured: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """The two MIME alternatives produced for one digest."""

    recipient_email: str
    subject: str
    text_body: str
    html_body: str | None
    html_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "recipient_email", normalize_email_address(self.recipient_email))
        for field_name, value in (("subject", self.subject), ("text_body", self.text_body)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"RenderedEmail.{field_name} must be non-blank text.")
        if self.html_body is not None and (
            not isinstance(self.html_body, str) or not self.html_body.strip()
        ):
            raise ValueError("RenderedEmail.html_body must be non-blank text when supplied.")


class DigestTemplateRenderer:
    """Render the checked-in plain-text and HTML digest templates."""

    def __init__(self, *, templates: Mapping[str, str] | None = None) -> None:
        source = dict(templates) if templates is not None else _load_templates()
        if set(source) != {"digest.txt", "digest.html"}:
            raise ValueError("Digest templates must define digest.txt and digest.html.")
        self._environment = Environment(
            loader=DictLoader(source),
            autoescape=select_autoescape(["html"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render_text(self, digest: EmailDigest) -> str:
        """Render the always-required plain-text alternative."""

        if not isinstance(digest, EmailDigest):
            raise TypeError("digest must be an EmailDigest.")
        return self._environment.get_template("digest.txt").render(**_context(digest)).strip()

    def render_html(self, digest: EmailDigest) -> str:
        """Render the richer alternative; callers may safely fall back."""

        if not isinstance(digest, EmailDigest):
            raise TypeError("digest must be an EmailDigest.")
        return self._environment.get_template("digest.html").render(**_context(digest)).strip()

    def render(self, digest: EmailDigest) -> RenderedEmail:
        """Render text and HTML, retaining text if HTML rendering degrades."""

        text_body = self.render_text(digest)
        html_body: str | None = None
        html_error: str | None = None
        try:
            html_body = self.render_html(digest)
        except Exception as error:  # template rendering must not suppress text delivery
            html_error = _safe_error(error)
        return RenderedEmail(
            recipient_email=digest.recipient_email,
            subject=digest.subject,
            text_body=text_body,
            html_body=html_body,
            html_error=html_error,
        )


class EmailNotifier:
    """C-54 email adapter with explicit configuration and failure states."""

    def __init__(
        self,
        settings: NotificationsSettings | None = None,
        *,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_sender: str | None = None,
        smtp_username: str | None = None,
        smtp_password: str | object | None = None,
        timeout_seconds: float = 30.0,
        use_tls: bool = True,
        use_ssl: bool = False,
        recipient_resolver: RecipientResolver | None = None,
        recipient_email: str | None = None,
        fallback_notifier: Notifier | None = None,
        renderer: DigestTemplateRenderer | None = None,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        if settings is not None and not isinstance(settings, NotificationsSettings):
            raise TypeError("settings must be NotificationsSettings.")
        source = settings
        host = smtp_host if smtp_host is not None else (source.smtp_host if source else None)
        port = smtp_port if smtp_port is not None else (source.smtp_port if source else 587)
        sender = (
            smtp_sender if smtp_sender is not None else (source.smtp_sender if source else None)
        )
        username = (
            smtp_username
            if smtp_username is not None
            else (source.smtp_username if source else None)
        )
        password = (
            smtp_password
            if smtp_password is not None
            else (source.smtp_password if source else None)
        )
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
            raise ValueError("smtp_port must be between 1 and 65535.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be greater than 0 and at most 120.")
        if not isinstance(use_tls, bool) or not isinstance(use_ssl, bool):
            raise TypeError("use_tls and use_ssl must be booleans.")
        if use_tls and use_ssl:
            raise ValueError("use_tls and use_ssl are mutually exclusive.")
        if recipient_resolver is not None and not isinstance(recipient_resolver, Mapping):
            if not callable(recipient_resolver):
                raise TypeError("recipient_resolver must be a mapping or callable.")
        if recipient_email is not None:
            recipient_email = normalize_email_address(recipient_email)
        if fallback_notifier is not None and not callable(getattr(fallback_notifier, "send", None)):
            raise TypeError("fallback_notifier must expose send(message).")
        if renderer is not None and not isinstance(renderer, DigestTemplateRenderer):
            raise TypeError("renderer must be a DigestTemplateRenderer.")
        if smtp_factory is not None and not callable(smtp_factory):
            raise TypeError("smtp_factory must be callable.")

        self.smtp_host = _host(host)
        self.smtp_port = port
        self.smtp_sender = None if sender is None else normalize_email_address(sender)
        self.smtp_username = _optional_text(username, "smtp_username", _MAX_USERNAME_LENGTH)
        self.smtp_password = _secret_text(password)
        self.timeout_seconds = float(timeout_seconds)
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.recipient_resolver = recipient_resolver
        self.recipient_email = recipient_email
        self.fallback_notifier = fallback_notifier
        self.renderer = renderer or DigestTemplateRenderer()
        if smtp_factory is not None:
            self._smtp_factory = smtp_factory
        elif use_ssl:
            self._smtp_factory = smtplib.SMTP_SSL
        else:
            self._smtp_factory = smtplib.SMTP

    @property
    def capability(self) -> EmailCapability:
        """Return the status an administrator can display without connecting."""

        if self.smtp_host is None:
            return EmailCapability(False, _SMTP_UNCONFIGURED)
        if self.smtp_sender is None:
            return EmailCapability(False, "SMTP sender is not configured.")
        if self.smtp_username is not None and self.smtp_password is None:
            return EmailCapability(False, "SMTP password is missing for the configured username.")
        if self.smtp_username is not None and not (self.use_tls or self.use_ssl):
            return EmailCapability(False, "SMTP authentication requires TLS.")
        return EmailCapability(True, self.smtp_host)

    @property
    def configuration(self) -> EmailCapability:
        """Compatibility alias for the administrator-facing capability."""

        return self.capability

    @property
    def is_configured(self) -> bool:
        """Whether a send can be attempted safely."""

        return self.capability.configured

    @property
    def configuration_notice(self) -> str:
        """Explain the degraded path without exposing a credential."""

        return self.capability.detail

    def send(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver one already-rendered notification through SMTP."""

        if not isinstance(message, OutboundMessage):
            raise TypeError("EmailNotifier.send requires an OutboundMessage.")
        if message.channel is not NotificationChannel.EMAIL:
            raise ValueError("EmailNotifier can send only email-channel messages.")
        if not self.capability.configured:
            return self._fallback(message, message.subject, message.body, self.capability.detail)
        try:
            recipient = self._recipient(message.recipient_id)
        except (TypeError, ValueError) as error:
            return DeliveryResult(
                DeliveryStatus.DEAD_LETTERED,
                error=f"email recipient configuration failed: {_safe_error(error)}",
            )
        html_body = _plain_text_as_html(message.body)
        return self._deliver_or_fallback(
            message,
            recipient,
            subject=message.subject,
            text_body=message.body,
            html_body=html_body,
        )

    def send_digest(self, digest: EmailDigest) -> DeliveryResult:
        """Render and deliver one bundled digest as a multipart email."""

        if not isinstance(digest, EmailDigest):
            raise TypeError("digest must be an EmailDigest.")
        try:
            rendered = self.renderer.render(digest)
        except Exception as error:
            return DeliveryResult(
                DeliveryStatus.RETRY, error=f"email text rendering failed: {_safe_error(error)}"
            )
        message = OutboundMessage(
            recipient_id=digest.recipient_id,
            channel=NotificationChannel.EMAIL,
            template="morning_queue",
            subject=rendered.subject,
            body=rendered.text_body,
            payload=MappingProxyType(
                {"notification_ids": tuple(str(item) for item in digest.notification_ids)}
            ),
            scheduled_for=digest.window.end,
        )
        return self._deliver_or_fallback(
            message,
            digest.recipient_email,
            subject=rendered.subject,
            text_body=rendered.text_body,
            html_body=rendered.html_body,
        )

    deliver = send
    send_email = send

    def _deliver_or_fallback(
        self,
        message: OutboundMessage,
        recipient: str,
        *,
        subject: str,
        text_body: str,
        html_body: str | None,
    ) -> DeliveryResult:
        capability = self.capability
        if not capability.configured:
            return self._fallback(message, subject, text_body, capability.detail)
        email_message = _mime_message(
            sender=self.smtp_sender,
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        return self._send_smtp(email_message)

    def _fallback(
        self,
        source: OutboundMessage,
        subject: str,
        text_body: str,
        reason: str,
    ) -> DeliveryResult:
        if self.fallback_notifier is None:
            return DeliveryResult(DeliveryStatus.DEAD_LETTERED, error=reason)
        fallback_message = OutboundMessage(
            recipient_id=source.recipient_id,
            channel=NotificationChannel.IN_APP,
            template=_SMTP_FALLBACK_TEMPLATE,
            subject=subject,
            body=text_body,
            payload={"email_fallback": True, "source_template": source.template},
            subject_type=source.subject_type,
            subject_id=source.subject_id,
            scheduled_for=source.scheduled_for,
            attempt=source.attempt,
        )
        try:
            result = self.fallback_notifier.send(fallback_message)
            if not isinstance(result, DeliveryResult):
                raise TypeError("fallback notifier must return DeliveryResult.")
        except Exception as error:
            return DeliveryResult(
                DeliveryStatus.RETRY, error=f"in-app fallback failed: {_safe_error(error)}"
            )
        if result.status is DeliveryStatus.SENT:
            return DeliveryResult(
                DeliveryStatus.SENT,
                provider_message_id=f"inapp-fallback:{source.recipient_id}",
            )
        return result

    def _recipient(self, recipient_id: UUID) -> str:
        if self.recipient_email is not None:
            return self.recipient_email
        resolver = self.recipient_resolver
        if resolver is None:
            raise ValueError(
                "An email recipient resolver is required for OutboundMessage delivery."
            )
        try:
            raw = (
                resolver[recipient_id] if isinstance(resolver, Mapping) else resolver(recipient_id)
            )
        except KeyError as error:
            raise ValueError(
                f"No email address is available for recipient {recipient_id}."
            ) from error
        if raw is None:
            raise ValueError(f"No email address is available for recipient {recipient_id}.")
        return normalize_email_address(raw)

    def _send_smtp(self, message: EmailMessage) -> DeliveryResult:
        client: SmtpClient | None = None
        try:
            client = self._smtp_factory(
                self.smtp_host, self.smtp_port, timeout=self.timeout_seconds
            )
            if self.use_tls:
                client.ehlo()
                client.starttls()
                client.ehlo()
            if self.smtp_username is not None and self.smtp_password is not None:
                client.login(self.smtp_username, self.smtp_password)
            refused = client.send_message(message)
            if refused:
                return _refused_result(refused)
            return DeliveryResult(
                DeliveryStatus.SENT,
                provider_message_id=message.get("Message-ID") or make_msgid(),
            )
        except smtplib.SMTPAuthenticationError:
            return DeliveryResult(DeliveryStatus.DEAD_LETTERED, error=_SMTP_AUTH_FAILED)
        except smtplib.SMTPRecipientsRefused as error:
            return _refused_result(error.recipients)
        except smtplib.SMTPConnectError as error:
            return DeliveryResult(
                DeliveryStatus.RETRY,
                error=f"SMTP connection failed: {_safe_error(error)}",
            )
        except (smtplib.SMTPDataError, smtplib.SMTPResponseException) as error:
            code = getattr(error, "smtp_code", 0)
            return _smtp_code_result(code, _safe_error(error))
        except (
            smtplib.SMTPServerDisconnected,
            TimeoutError,
            OSError,
        ) as error:
            return DeliveryResult(
                DeliveryStatus.RETRY, error=f"SMTP transport failed: {_safe_error(error)}"
            )
        except smtplib.SMTPException as error:
            return DeliveryResult(DeliveryStatus.RETRY, error=f"SMTP failed: {_safe_error(error)}")
        except Exception as error:
            return DeliveryResult(
                DeliveryStatus.RETRY,
                error=f"SMTP adapter failed: {_safe_error(error)}",
            )
        finally:
            if client is not None:
                with suppress(Exception):
                    client.quit()


def _load_templates() -> dict[str, str]:
    package = resources.files("covenant_radar.notifications.templates.email")
    return {
        "digest.txt": package.joinpath("digest.txt").read_text(encoding="utf-8"),
        "digest.html": package.joinpath("digest.html").read_text(encoding="utf-8"),
    }


def _context(digest: EmailDigest) -> dict[str, object]:
    return {
        "subject": digest.subject,
        "window_start": digest.window.start.isoformat(),
        "window_end": digest.window.end.isoformat(),
        "entries": tuple(
            {
                "title": entry.title,
                "summary": entry.summary,
                "deep_link": entry.deep_link,
            }
            for entry in digest.entries
        ),
    }


def _mime_message(
    *,
    sender: str | None,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str | None,
) -> EmailMessage:
    if sender is None:
        raise ValueError("SMTP sender is required for MIME message construction.")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = normalize_email_address(recipient)
    message["Subject"] = _header_text(subject, "subject")
    message["Message-ID"] = make_msgid(domain="covenant-radar")
    message.set_content(_body_text(text_body, "text_body"))
    if html_body is not None:
        message.add_alternative(_body_text(html_body, "html_body"), subtype="html")
    return message


def _plain_text_as_html(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return "<html><body>" + "<p>" + escaped.replace("\n", "<br>\n") + "</p></body></html>"


def _refused_result(refused: Mapping[str, tuple[int, bytes]]) -> DeliveryResult:
    codes = [value[0] for value in refused.values() if isinstance(value, tuple) and value]
    detail = _safe_error("SMTP refused one or more recipients")
    if codes and all(400 <= code < 500 for code in codes):
        return DeliveryResult(DeliveryStatus.RETRY, error=detail)
    return DeliveryResult(DeliveryStatus.DEAD_LETTERED, error=detail)


def _smtp_code_result(code: object, detail: str) -> DeliveryResult:
    if isinstance(code, int) and 400 <= code < 500:
        return DeliveryResult(DeliveryStatus.RETRY, error=f"SMTP transient failure: {detail}")
    return DeliveryResult(DeliveryStatus.DEAD_LETTERED, error=_SMTP_PERMANENT_FAILURE)


def _host(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_HOST_LENGTH:
        raise ValueError("smtp_host must be a bounded non-blank hostname.")
    normalized = value.strip()
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("smtp_host contains invalid characters.")
    return normalized


def _optional_text(value: object, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{field_name} must be bounded non-blank text.")
    normalized = value.strip()
    if any(char in normalized for char in "\r\n"):
        raise ValueError(f"{field_name} contains a header-injection character.")
    return normalized


def _secret_text(value: object) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if callable(getter) else value
    if not isinstance(raw, str) or not raw:
        raise ValueError("smtp_password must be non-empty text when supplied.")
    return raw


def _header_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\r\n"):
        raise ValueError(f"{field_name} must be non-blank text without newlines.")
    return value.strip()


def _body_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")
    return value


def _safe_error(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:_MAX_PROVIDER_ERROR_LENGTH] or "SMTP delivery failed"


SMTPNotifier = EmailNotifier
SmtpNotifier = EmailNotifier
DigestRenderer = DigestTemplateRenderer


__all__ = [
    "DigestRenderer",
    "DigestTemplateRenderer",
    "EmailCapability",
    "EmailNotifier",
    "RenderedEmail",
    "SMTPNotifier",
    "SmtpNotifier",
]
