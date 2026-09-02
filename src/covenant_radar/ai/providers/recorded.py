"""Recorded language-model responses for offline and air-gapped operation.

The recorded adapter is deliberately a provider, rather than a test helper.
It has the same port as a live provider and therefore can be selected by the
application without changing a caller. Cassettes are keyed by the exact
provider-facing messages and prompt version. A recording contains only that
already-masked request and the normalised provider response; the masking token
map is never serialised.

The directory format is one JSON envelope per response. Keeping entries in
separate files means one damaged response cannot make an otherwise usable
cassette set unavailable. Writes are atomic and duplicate keys are rejected
unless the existing entry is equivalent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from covenant_radar.ai.errors import ProviderConfigurationError, ProviderUnavailable
from covenant_radar.ai.masking import MASKING_MARKER, MaskedPrompt
from covenant_radar.ai.providers.base import normalise_openai_payload
from covenant_radar.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMProvider,
    MessageRole,
    PromptMessage,
)

logger = logging.getLogger(__name__)

CASSETTE_FORMAT: Final[str] = "covenant-radar-cassette"
CASSETTE_SCHEMA_VERSION: Final[int] = 1
MAX_CASSETTE_BYTES: Final[int] = 16 * 1024 * 1024
MAX_CASSETTE_FILES: Final[int] = 10_000
MAX_CASSETTE_ENTRIES: Final[int] = 100_000

_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")

# These are defence-in-depth checks for the recording seam. Normal calls have
# already passed through ``ai.masking``; rejecting recognisable official
# identifiers here prevents direct misuse from writing obvious raw values.
_PERSONAL_DATA_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\w)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\w)"),
    re.compile(r"(?<!\w)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\w)"),
    re.compile(r"(?<!\w)[A-Z]{5}\d{4}[A-Z](?!\w)", re.IGNORECASE),
    re.compile(r"(?i)\b(?:account|acct|a/c)(?:\s*(?:number|no\.?|#))?\s*[:#-]?\s*\d{8,20}\b"),
)


class CassetteError(RuntimeError):
    """Base class for safe cassette read and write failures."""


class CassetteWriteError(CassetteError):
    """Raised when a response cannot be persisted without weakening safety."""


class CassetteMiss(ProviderUnavailable):
    """A provider-unavailable result caused by an absent recorded response."""

    def __init__(self) -> None:
        super().__init__("recorded", reason="cassette miss")
        self.args = ("No recorded response matches the masked prompt.",)


class CassetteLoadWarning(UserWarning):
    """Warning emitted when one cassette file is ignored during loading."""


@dataclass(frozen=True, slots=True)
class CassetteEntry:
    """Validated, persistence-neutral representation of one cassette entry."""

    key: str
    prompt_version: str | None
    messages: tuple[PromptMessage, ...] | None
    response: Mapping[str, object]
    request_key: str | None = None

    def as_json(self) -> dict[str, object]:
        """Return the JSON-safe entry representation."""

        entry: dict[str, object] = {"response": dict(self.response)}
        if self.messages is not None:
            request: dict[str, object] = {
                "masking_marker": MASKING_MARKER,
                "prompt_version": self.prompt_version,
                "messages": [
                    {"role": message.role, "content": message.content} for message in self.messages
                ],
            }
            if self.request_key is not None:
                request["cassette_key"] = self.request_key
            entry["request"] = request
        return entry


class CassetteStore:
    """Load and atomically update a cassette file or cassette directory.

    ``create`` is intended for record mode. Replay mode leaves a missing path
    as a configuration error, while an existing but empty directory is a valid
    store whose individual lookups miss explicitly.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        create: bool = False,
        warning_sink: object | None = None,
    ) -> None:
        self.path = Path(path)
        self._warning_sink = warning_sink
        self._is_directory = self._resolve_kind()
        if self._is_directory and create:
            try:
                self.path.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise CassetteWriteError(
                    "Cassette directory could not be created safely."
                ) from error
        elif not self.path.exists() and not create:
            raise ProviderConfigurationError(
                f"Recorded provider response path not found: {self.path}.",
                provider="recorded",
            )
        elif not self._is_directory and create:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise CassetteWriteError(
                    "Cassette directory could not be created safely."
                ) from error

        self._entries: dict[str, CassetteEntry] = {}
        self.reload()

    def _resolve_kind(self) -> bool:
        if self.path.is_symlink():
            raise ProviderConfigurationError(
                "Recorded provider cassette paths may not be symbolic links.",
                provider="recorded",
            )
        if self.path.exists():
            if self.path.is_dir():
                return True
            if self.path.is_file():
                return False
            raise ProviderConfigurationError(
                "Recorded provider cassette path must be a regular file or directory.",
                provider="recorded",
            )
        return self.path.suffix.casefold() != ".json"

    def reload(self) -> tuple[CassetteEntry, ...]:
        """Reload all usable entries, warning and skipping damaged files."""

        self._entries = {}
        if not self.path.exists():
            return ()
        if self._is_directory:
            if not self.path.is_dir():
                self._warn(self.path, "cassette path changed from a directory")
                return ()
            try:
                candidates = sorted(self.path.glob("*.json"), key=lambda item: item.name)
            except OSError as error:
                self._warn(self.path, "cassette directory could not be enumerated")
                logger.warning(
                    "cassette_directory_enumeration_failed",
                    extra={"error": type(error).__name__},
                )
                return ()
            if len(candidates) > MAX_CASSETTE_FILES:
                self._warn(self.path, "cassette directory contains too many JSON files")
                candidates = candidates[:MAX_CASSETTE_FILES]
            for candidate in candidates:
                self._load_file(candidate)
        else:
            self._load_file(self.path)
        return tuple(self._entries.values())

    @property
    def entries(self) -> tuple[CassetteEntry, ...]:
        """Return entries in deterministic key order."""

        return tuple(self._entries[key] for key in sorted(self._entries))

    @property
    def size(self) -> int:
        """Return the number of usable entries currently loaded."""

        return len(self._entries)

    def get(self, key: str) -> CassetteEntry | None:
        """Return an entry by key, or ``None`` for an explicit miss."""

        return self._entries.get(key)

    def record(
        self,
        request: CompletionRequest,
        response: CompletionResponse,
        *,
        masked_prompt: MaskedPrompt | None = None,
    ) -> CassetteEntry:
        """Persist one successful response and return its validated entry.

        ``request`` is the provider-facing request and is expected to have
        already passed the masking boundary. Supplying ``masked_prompt``
        additionally proves that the request came from the typed masking
        result; this is the preferred API for direct recording.
        """

        if not isinstance(request, CompletionRequest):
            raise TypeError("Cassette recording requires a CompletionRequest.")
        if not isinstance(response, CompletionResponse):
            raise TypeError("Cassette recording requires a CompletionResponse.")
        stored_messages: tuple[PromptMessage, ...] | None = None
        if masked_prompt is not None:
            _verify_masked_prompt(masked_prompt, request)
            stored_messages = masked_prompt.messages
        # A provider-facing request is already masked by the normal call site,
        # but this seam must remain safe when called directly. Store only the
        # digest in that case; a request body requires a typed MaskedPrompt.
        entry = CassetteEntry(
            key=request.cassette_key or cassette_key(request),
            prompt_version=request.prompt_version,
            messages=stored_messages,
            response=_response_to_json(response),
            request_key=request.cassette_key,
        )
        self._write_entry(entry)
        self._entries[entry.key] = entry
        return entry

    def record_masked(
        self,
        masked_prompt: MaskedPrompt,
        response: CompletionResponse,
        *,
        model: str = "recorded",
        max_tokens: int = 2048,
        temperature: float | None = 0.0,
    ) -> CassetteEntry:
        """Record a response directly from a verified :class:`MaskedPrompt`."""

        if not isinstance(masked_prompt, MaskedPrompt):
            raise TypeError("record_masked requires a MaskedPrompt.")
        request = CompletionRequest(
            messages=masked_prompt.messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            prompt_version=masked_prompt.version,
        )
        return self.record(request, response, masked_prompt=masked_prompt)

    def _write_entry(self, entry: CassetteEntry) -> None:
        existing = self._entries.get(entry.key)
        if existing is not None:
            if existing.as_json() == entry.as_json():
                return
            raise CassetteWriteError(
                f"Cassette key {entry.key} already exists with a different response."
            )

        envelope = {
            "format": CASSETTE_FORMAT,
            "schema_version": CASSETTE_SCHEMA_VERSION,
            "entries": {entry.key: entry.as_json()},
        }
        if self._is_directory:
            target = self.path / f"{entry.key}.json"
            self._atomic_write(target, envelope)
            return

        combined = dict(self._entries)
        combined[entry.key] = entry
        self._atomic_write(
            self.path,
            {
                "format": CASSETTE_FORMAT,
                "schema_version": CASSETTE_SCHEMA_VERSION,
                "entries": {key: combined[key].as_json() for key in sorted(combined)},
            },
        )

    def _atomic_write(self, target: Path, value: Mapping[str, object]) -> None:
        parent = target.parent
        temporary_name: str | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
                allow_nan=False,
            )
            if len(encoded.encode("utf-8")) > MAX_CASSETTE_BYTES:
                raise CassetteWriteError("Cassette file exceeds the 16 MiB limit.")
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, target)
        except CassetteWriteError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise CassetteWriteError("Cassette response could not be written safely.") from error
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                try:
                    os.unlink(temporary_name)
                except OSError:
                    logger.warning("cassette_temporary_cleanup_failed")

    def _load_file(self, path: Path) -> None:
        try:
            if path.is_symlink():
                self._warn(path, "symbolic-link cassette files are not accepted")
                return
            if not path.is_file():
                self._warn(path, "cassette path is not a regular file")
                return
            if path.stat().st_size > MAX_CASSETTE_BYTES:
                self._warn(path, "cassette file exceeds the 16 MiB limit")
                return
            content = path.read_text(encoding="utf-8")
            payload = json.loads(content)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            self._warn(path, f"corrupt cassette file ({type(error).__name__})")
            return

        if not isinstance(payload, Mapping):
            self._warn(path, "cassette root must be a JSON object")
            return
        candidates = _entry_candidates(payload)
        if candidates is None:
            self._warn(path, "cassette format is not recognised")
            return
        for key, value in candidates:
            if len(self._entries) >= MAX_CASSETTE_ENTRIES:
                self._warn(path, "cassette entry limit reached")
                return
            try:
                entry = _parse_entry(key, value)
            except (CassetteError, TypeError, ValueError, KeyError, AttributeError) as error:
                self._warn(path, f"corrupt cassette entry skipped ({type(error).__name__})")
                continue
            if entry.key in self._entries:
                self._warn(path, f"duplicate cassette key {entry.key} skipped")
                continue
            self._entries[entry.key] = entry

    def _warn(self, path: Path, reason: str) -> None:
        message = f"Skipping cassette {path.name}: {reason}."
        warnings.warn(message, CassetteLoadWarning, stacklevel=3)
        sink = self._warning_sink
        if sink is not None:
            warning_method = getattr(sink, "warning", None)
            if callable(warning_method):
                cast(Callable[[str], object], warning_method)(message)
            elif callable(sink):
                sink(message)
        logger.warning("cassette_skipped", extra={"path": path.name, "reason": reason})


class RecordedProvider:
    """Replay provider backed by keyed in-memory responses or cassettes."""

    provider_name = "recorded"

    def __init__(
        self,
        *,
        responses: Mapping[str, object] | None = None,
        responses_path: Path | str | None = None,
        cassette_path: Path | str | None = None,
        path: Path | str | None = None,
    ) -> None:
        paths = [value for value in (responses_path, cassette_path, path) if value is not None]
        if responses is not None and paths:
            raise ValueError("Provide either responses or one cassette path, not both.")
        if len(paths) > 1:
            raise ValueError("Provide only one cassette path.")
        self._responses: dict[str, object] | None
        self._store: CassetteStore | None
        if responses is not None:
            self._responses = dict(responses)
            self._store = None
        elif paths:
            self._responses = None
            self._store = CassetteStore(paths[0])
        else:
            raise ProviderConfigurationError(
                "Recorded provider requires a cassette response mapping or path.",
                provider=self.provider_name,
            )

    @property
    def cassette_store(self) -> CassetteStore | None:
        """Return the backing store for diagnostics and cassette tooling."""

        return self._store

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Replay a matching response or raise an explicit cassette miss."""

        if not isinstance(request, CompletionRequest):
            raise TypeError("Recorded provider requires a CompletionRequest.")
        key = request.cassette_key or cassette_key(request)
        if self._store is not None:
            entry = self._store.get(key)
            if entry is None:
                raise _cassette_miss()
            if entry.messages is not None and not _request_matches_entry(request, entry):
                raise _cassette_miss()
            return _response_from_json(entry.response)
        if self._responses is None or key not in self._responses:
            raise _cassette_miss()
        return _normalise_recorded_value(self._responses[key])

    def replay(self, request: CompletionRequest) -> CompletionResponse:
        """Explicitly named replay operation for offline tooling."""

        return self.complete(request)

    def response_for_key(self, key: str) -> CompletionResponse:
        """Read a response by an explicit key for CLI diagnostics."""

        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("Cassette key must be bounded non-empty text.")
        if self._store is not None:
            entry = self._store.get(key)
            if entry is None:
                raise _cassette_miss()
            return _response_from_json(entry.response)
        if self._responses is None or key not in self._responses:
            raise _cassette_miss()
        return _normalise_recorded_value(self._responses[key])


class RecordingProvider:
    """Capture successful responses from a live provider into a cassette."""

    def __init__(
        self,
        provider: LLMProvider,
        cassette_path: Path | str | None = None,
        *,
        store: CassetteStore | None = None,
    ) -> None:
        if not isinstance(provider, LLMProvider):
            raise TypeError("RecordingProvider requires an LLMProvider.")
        if store is not None and cassette_path is not None:
            raise ValueError("Provide either cassette_path or store, not both.")
        if store is None and cassette_path is None:
            raise ProviderConfigurationError(
                "Record mode requires a cassette path.", provider="recorded"
            )
        self.provider = provider
        if store is not None:
            self.store = store
        else:
            assert cassette_path is not None
            self.store = CassetteStore(cassette_path, create=True)
        self.provider_name = str(getattr(provider, "provider_name", "recorded"))

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Call the live provider once, then persist its successful response."""

        response = self.provider.complete(request)
        self.store.record(request, response)
        return response

    def complete_masked(
        self,
        masked_prompt: MaskedPrompt,
        *,
        model: str,
        max_tokens: int = 2048,
        temperature: float | None = 0.0,
        timeout_seconds: float | None = None,
    ) -> CompletionResponse:
        """Complete and record a verified :class:`MaskedPrompt` in one call."""

        if not isinstance(masked_prompt, MaskedPrompt):
            raise TypeError("complete_masked requires a MaskedPrompt.")
        request = CompletionRequest(
            messages=masked_prompt.messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            prompt_version=masked_prompt.version,
        )
        response = self.provider.complete(request)
        self.store.record(request, response, masked_prompt=masked_prompt)
        return response

    def close(self) -> None:
        """Close the wrapped provider when it owns a closeable client."""

        close = getattr(self.provider, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> RecordingProvider:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def cassette_key(request: CompletionRequest) -> str:
    """Hash the exact masked messages and prompt version deterministically."""

    if not isinstance(request, CompletionRequest):
        raise TypeError("cassette_key requires a CompletionRequest.")
    canonical = json.dumps(
        {
            "messages": [
                {"content": message.content, "role": message.role} for message in request.messages
            ],
            "prompt_version": request.prompt_version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _cassette_miss() -> ProviderUnavailable:
    return CassetteMiss()


def _verify_masked_prompt(masked_prompt: MaskedPrompt, request: CompletionRequest) -> None:
    if masked_prompt.marker != MASKING_MARKER:
        raise CassetteWriteError("Cassette recording requires a valid masking marker.")
    if masked_prompt.messages != request.messages:
        raise CassetteWriteError("Masked prompt and completion request do not agree.")
    if masked_prompt.version != request.prompt_version:
        raise CassetteWriteError("Masked prompt and completion request versions do not agree.")


def _verify_recordable_messages(messages: Sequence[PromptMessage]) -> None:
    for message in messages:
        for pattern in _PERSONAL_DATA_PATTERNS:
            if pattern.search(message.content):
                raise CassetteWriteError("Cassette recording refused unmasked prompt content.")


def _response_to_json(response: CompletionResponse) -> Mapping[str, object]:
    value: dict[str, object] = {
        "text": response.text,
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": response.latency_ms,
        "normalization_note": response.normalization_note,
        "raw_payload": response.raw_payload,
    }
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise CassetteWriteError(
            "Provider response is not JSON-serialisable and cannot be recorded."
        ) from error
    return value


def _normalise_recorded_value(recorded: object) -> CompletionResponse:
    if isinstance(recorded, CompletionResponse):
        return CompletionResponse(
            text=recorded.text,
            model=recorded.model,
            input_tokens=recorded.input_tokens,
            output_tokens=recorded.output_tokens,
            latency_ms=recorded.latency_ms,
            raw_payload=recorded.raw_payload,
            normalization_note=recorded.normalization_note,
            from_cassette=True,
        )
    if isinstance(recorded, Mapping) and _looks_like_normalised_response(recorded):
        return _response_from_json(recorded)
    return normalise_openai_payload(recorded, latency_ms=0, from_cassette=True)


def _response_from_json(recorded: Mapping[str, object]) -> CompletionResponse:
    if not _looks_like_normalised_response(recorded):
        return normalise_openai_payload(recorded, latency_ms=0, from_cassette=True)
    text = recorded.get("text")
    model = recorded.get("model")
    input_tokens = recorded.get("input_tokens")
    output_tokens = recorded.get("output_tokens")
    latency_ms = recorded.get("latency_ms", 0)
    normalization_note = recorded.get("normalization_note")
    if text is not None and not isinstance(text, str):
        return normalise_openai_payload(recorded, latency_ms=0, from_cassette=True)
    if model is not None and not isinstance(model, str):
        return normalise_openai_payload(recorded, latency_ms=0, from_cassette=True)
    if (
        not _valid_count(input_tokens)
        or not _valid_count(output_tokens)
        or not _valid_count(latency_ms)
    ):
        return normalise_openai_payload(recorded, latency_ms=0, from_cassette=True)
    if normalization_note is not None and not isinstance(normalization_note, str):
        normalization_note = None
    return CompletionResponse(
        text=text,
        model=model,
        input_tokens=cast(int | None, input_tokens),
        output_tokens=cast(int | None, output_tokens),
        latency_ms=cast(int, latency_ms),
        raw_payload=recorded.get("raw_payload", recorded),
        normalization_note=normalization_note,
        from_cassette=True,
    )


def _looks_like_normalised_response(recorded: Mapping[str, object]) -> bool:
    return "text" in recorded and (
        "raw_payload" in recorded or "model" in recorded or "latency_ms" in recorded
    )


def _valid_count(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _entry_candidates(payload: Mapping[str, object]) -> list[tuple[str, object]] | None:
    if payload.get("format") == CASSETTE_FORMAT:
        if payload.get("schema_version") != CASSETTE_SCHEMA_VERSION:
            return None
        entries = payload.get("entries")
        if not isinstance(entries, Mapping):
            return None
        return [(str(key), value) for key, value in entries.items()]
    if "key" in payload and "response" in payload:
        key = payload.get("key")
        return [(key, payload)] if isinstance(key, str) else None
    # Compatibility with the original mapping-only adapter format.
    if payload and all(isinstance(key, str) for key in payload):
        return [(key, value) for key, value in payload.items()]
    return None


def _parse_entry(key: str, value: object) -> CassetteEntry:
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        raise ValueError("invalid cassette key")
    if not isinstance(value, Mapping):
        raise TypeError("cassette entry is not an object")

    request_value = value.get("request")
    messages: tuple[PromptMessage, ...] | None = None
    prompt_version: str | None = None
    request_key: str | None = None
    response_value: object = value.get("response", value)
    if request_value is not None:
        if not isinstance(request_value, Mapping):
            raise TypeError("cassette request is not an object")
        if request_value.get("masking_marker") != MASKING_MARKER:
            raise ValueError("cassette request is not marked as masked")
        version = request_value.get("prompt_version")
        if version is not None and (
            not isinstance(version, str) or not version or len(version) > 50
        ):
            raise ValueError("cassette prompt version is invalid")
        prompt_version = version
        raw_request_key = request_value.get("cassette_key")
        if raw_request_key is not None and (
            not isinstance(raw_request_key, str) or _KEY_RE.fullmatch(raw_request_key) is None
        ):
            raise ValueError("cassette key is invalid")
        request_key = raw_request_key
        raw_messages = request_value.get("messages")
        if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, str | bytes):
            raise TypeError("cassette messages are missing")
        parsed_messages: list[PromptMessage] = []
        for item in raw_messages:
            if not isinstance(item, Mapping):
                raise TypeError("cassette message is not an object")
            role = item.get("role")
            content = item.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(content, str):
                raise ValueError("cassette message shape is invalid")
            parsed_messages.append(PromptMessage(role=cast(MessageRole, role), content=content))
        messages = tuple(parsed_messages)
        derived_key = cassette_key(
            CompletionRequest(messages=messages, model="cassette", prompt_version=prompt_version)
        )
        if (request_key or derived_key) != key:
            raise ValueError("cassette key does not match its masked request")
        _verify_recordable_messages(messages)
    if not isinstance(response_value, Mapping):
        raise TypeError("cassette response is not an object")
    json.dumps(response_value, ensure_ascii=False, allow_nan=False)
    return CassetteEntry(
        key=key,
        prompt_version=prompt_version,
        messages=messages,
        response=cast(Mapping[str, object], response_value),
        request_key=request_key,
    )


def _request_matches_entry(request: CompletionRequest, entry: CassetteEntry) -> bool:
    if entry.messages is None:
        return True
    return (
        request.prompt_version == entry.prompt_version
        and request.messages == entry.messages
        and request.cassette_key == entry.request_key
    )


__all__ = [
    "CASSETTE_FORMAT",
    "CASSETTE_SCHEMA_VERSION",
    "CassetteEntry",
    "CassetteError",
    "CassetteLoadWarning",
    "CassetteMiss",
    "CassetteStore",
    "CassetteWriteError",
    "RecordedProvider",
    "RecordingProvider",
    "cassette_key",
]
