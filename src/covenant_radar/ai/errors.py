"""Safe, provider-specific failures for the language-model adapter layer."""

from __future__ import annotations

from typing import Final


class ProviderError(Exception):
    """Base class for deliberate provider failures.

    Error text is intentionally generic.  Provider responses, URLs, request
    bodies and credentials are never copied into an exception message.
    """

    def __init__(self, provider: str, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.reason = reason


class ProviderUnavailable(ProviderError):
    """The provider could not be reached or is temporarily unavailable."""

    def __init__(self, provider: str, *, reason: str = "transport failure") -> None:
        super().__init__(
            provider,
            f"LLM provider '{provider}' is unavailable.",
            reason=reason,
        )


class ProviderAuthError(ProviderError):
    """The provider rejected authentication; credentials are never retried."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            provider,
            f"LLM provider '{provider}' rejected authentication.",
            reason="authentication failure",
        )


class ProviderRequestRejected(ProviderError):
    """The provider rejected a non-authentication request."""

    def __init__(self, provider: str, *, status_code: int) -> None:
        super().__init__(
            provider,
            f"LLM provider '{provider}' rejected the request.",
            reason=f"http status {status_code}",
        )
        self.status_code = status_code


class ProviderConfigurationError(ProviderError):
    """The selected adapter cannot be constructed safely from configuration."""

    def __init__(self, message: str, *, provider: str = "configuration") -> None:
        super().__init__(provider, message, reason="invalid configuration")


class ModelGovernanceBlocked(RuntimeError):
    """A model call was refused by the production model-register guard."""


VALID_PROVIDER_NAMES: Final[tuple[str, ...]] = (
    "tcs",
    "azure_openai",
    "anthropic",
    "recorded",
)


__all__ = [
    "ModelGovernanceBlocked",
    "ProviderAuthError",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderRequestRejected",
    "ProviderUnavailable",
    "VALID_PROVIDER_NAMES",
]
