"""The provider-neutral language-model port.

The port deliberately contains no HTTP, configuration or provider-specific
knowledge.  Callers can construct one immutable request and receive one
immutable response regardless of which adapter is selected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast, runtime_checkable

MessageRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """One bounded chat message sent to a provider."""

    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Prompt message content must not be empty.")
        if len(self.content) > 1_048_576:
            raise ValueError("Prompt message content exceeds the 1 MiB limit.")


MessageInput = PromptMessage | Mapping[str, str]


@dataclass(frozen=True, slots=True, init=False)
class CompletionRequest:
    """Provider-neutral completion input.

    ``prompt_version`` and ``cassette_key`` are local routing metadata.  Live
    adapters never transmit them; the recorded adapter uses them to make
    replay identity explicit when the call site has already computed a key.
    """

    messages: tuple[PromptMessage, ...]
    model: str
    max_tokens: int = 2048
    temperature: float | None = 0.0
    timeout_seconds: float | None = None
    prompt_version: str | None = None
    cassette_key: str | None = None

    def __init__(
        self,
        messages: Sequence[MessageInput],
        model: str,
        max_tokens: int = 2048,
        temperature: float | None = 0.0,
        timeout_seconds: float | None = None,
        prompt_version: str | None = None,
        cassette_key: str | None = None,
    ) -> None:
        object.__setattr__(
            self, "messages", tuple(_coerce_message(message) for message in messages)
        )
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "max_tokens", max_tokens)
        object.__setattr__(self, "temperature", temperature)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "prompt_version", prompt_version)
        object.__setattr__(self, "cassette_key", cassette_key)
        self._validate()

    def _validate(self) -> None:
        if not self.model or len(self.model) > 256:
            raise ValueError("Completion model must be between 1 and 256 characters.")
        if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool):
            raise TypeError("Completion max_tokens must be an integer.")
        if not 1 <= self.max_tokens <= 1_000_000:
            raise ValueError("Completion max_tokens must be between 1 and 1000000.")
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not 0 <= self.temperature <= 2:
                raise ValueError("Completion temperature must be between 0 and 2.")
        if self.timeout_seconds is not None:
            if isinstance(self.timeout_seconds, bool) or not 0 < self.timeout_seconds <= 300:
                raise ValueError("Completion timeout_seconds must be between 0 and 300.")

        if not self.messages:
            raise ValueError("Completion request requires at least one message.")
        if sum(len(message.content) for message in self.messages) > 4 * 1_048_576:
            raise ValueError("Completion request content exceeds the 4 MiB limit.")


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    """Normalised provider output, including the unmodified provider payload."""

    text: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    raw_payload: object
    normalization_note: str | None = None
    from_cassette: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or self.latency_ms < 0
        ):
            raise ValueError("Completion latency_ms must be a non-negative integer.")
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"Completion {name} must be a non-negative integer or None.")

    @property
    def model_id(self) -> str | None:
        """Compatibility name for the provider-returned model identifier."""

        return self.model

    @property
    def tokens_in(self) -> int | None:
        """Compatibility name used by the model-call persistence layer."""

        return self.input_tokens

    @property
    def tokens_out(self) -> int | None:
        """Compatibility name used by the model-call persistence layer."""

        return self.output_tokens


@runtime_checkable
class LLMProvider(Protocol):
    """The only protocol a completion provider must implement."""

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Complete one request without retrying or interpreting the result."""


def _coerce_message(message: MessageInput) -> PromptMessage:
    if isinstance(message, PromptMessage):
        return message
    if not isinstance(message, Mapping):
        raise TypeError("Completion messages must be PromptMessage values or mappings.")

    role = message.get("role")
    content = message.get("content")
    if role not in {"system", "user", "assistant"}:
        raise ValueError("Completion message role must be system, user or assistant.")
    if not isinstance(content, str):
        raise TypeError("Completion message content must be text.")
    return PromptMessage(role=cast(MessageRole, role), content=content)


__all__ = [
    "CompletionRequest",
    "CompletionResponse",
    "LLMProvider",
    "MessageInput",
    "MessageRole",
    "PromptMessage",
]
