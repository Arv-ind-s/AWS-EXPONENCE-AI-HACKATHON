"""Explicit availability records derived from immutable application settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from covenant_radar.config.settings import Settings


@dataclass(frozen=True)
class Capability:
    """A configured feature dependency and its explanatory state."""

    configured: bool
    detail: str


@dataclass(frozen=True)
class Capabilities:
    """Configured external capabilities; consumers must inspect these before use."""

    model_provider: Capability
    sso: Capability
    ocr: Capability
    smtp: Capability
    webhooks: Capability
    document_store: Capability

    @classmethod
    def from_settings(cls, settings: Settings) -> Capabilities:
        """Derive availability without connecting to or probing any external service."""
        model_provider = settings.ai.provider != "none"
        sso = settings.security.sso_provider != "none"
        ocr = settings.documents.ocr_enabled
        smtp = settings.notifications.smtp_host is not None
        webhooks = settings.notifications.webhooks_enabled
        document_store = settings.documents.store != "none"

        return cls(
            model_provider=Capability(model_provider, settings.ai.provider),
            sso=Capability(sso, settings.security.sso_provider),
            ocr=Capability(ocr, settings.documents.ocr_command or "not configured"),
            smtp=Capability(smtp, settings.notifications.smtp_host or "not configured"),
            webhooks=Capability(webhooks, "configured" if webhooks else "not configured"),
            document_store=Capability(document_store, settings.documents.store),
        )
