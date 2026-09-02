"""Fail-closed construction of prompts sent to a language-model provider.

The model boundary is intentionally narrower than the rest of the
application.  :func:`build_outbound` accepts only derived values and the
small amount of clause text required by stage 1.  It recursively flattens
containers before validating their leaves, masks known names and common
official identifiers, and redacts configured secrets before a provider-ready
prompt is returned.

The returned :class:`MaskedPrompt` contains the masked messages and a
host-only token map.  The token map is excluded from the representation and
is never part of the messages consumed by a provider.  The client boundary
also checks the marker, so callers cannot replace this object with an
unmarked prompt accidentally.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final, cast

from covenant_radar.ports.llm import MessageInput, MessageRole, PromptMessage

MASKING_MARKER: Final[str] = "covenant-radar/masked/v1"
"""Stable marker verified by :mod:`covenant_radar.ai.client`."""

REDACTION_TOKEN: Final[str] = "[REDACTED]"
"""Replacement used for configured secrets."""

_SECRET_ENVIRONMENT_VARIABLE: Final[str] = "COVENANT_RADAR_AI_API_KEY"
_MAX_FIELDS: Final[int] = 256
_MAX_FIELD_PATH_LENGTH: Final[int] = 256
_MAX_NESTING_DEPTH: Final[int] = 32
_MAX_REQUEST_CONTENT_LENGTH: Final[int] = 4 * 1_048_576
_MAX_TEXT_LENGTH: Final[int] = 1_048_576
_MAX_NAMES: Final[int] = 256
_MAX_NAME_PATTERN_LENGTH: Final[int] = 8_192
_MAX_SECRETS: Final[int] = 32
_MAX_SECRET_LENGTH: Final[int] = 16_384
_MAX_TOKEN_MAP_ENTRIES: Final[int] = 512
_FIELD_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")


class FieldNotWhitelisted(ValueError):
    """Raised when an outbound field is not explicitly admitted."""

    def __init__(self, key: object) -> None:
        self.key = str(key)
        super().__init__(f"Outbound field {self.key!r} is not whitelisted.")


class FieldTypeNotAllowed(TypeError):
    """Raised when an admitted outbound field carries an unsafe type."""

    def __init__(self, key: str, expected: str, actual: object) -> None:
        self.key = key
        self.expected = expected
        self.actual = type(actual).__name__
        super().__init__(f"Outbound field {key!r} must contain {expected}; received {self.actual}.")


@dataclass(frozen=True, slots=True, init=False)
class MaskedPrompt:
    """Immutable provider-ready prompt with host-only masking metadata.

    ``content`` and ``messages`` are alternate constructors for compatibility
    with the client boundary.  ``fields`` is the masked, flattened field set;
    ``token_map`` is deliberately kept out of ``repr`` and out of the
    provider-facing message tuple.
    """

    messages: tuple[PromptMessage, ...]
    version: str | None
    marker: str
    _fields: Mapping[str, object] = field(repr=False, compare=False)
    _token_map: Mapping[str, str] = field(repr=False, compare=False)

    def __init__(
        self,
        content: str | Sequence[MessageInput] | None = None,
        *,
        messages: Sequence[MessageInput] | None = None,
        version: str | None = None,
        prompt_version: str | None = None,
        marker: str = MASKING_MARKER,
        fields: Mapping[str, object] | None = None,
        token_map: Mapping[str, str] | None = None,
    ) -> None:
        if content is not None and messages is not None:
            raise TypeError("Provide either content or messages, not both.")
        supplied = messages if messages is not None else content
        if isinstance(supplied, str):
            supplied_messages: Sequence[MessageInput] = (
                PromptMessage(role="user", content=supplied),
            )
        elif supplied is not None:
            supplied_messages = supplied
        else:
            raise ValueError("A masked prompt requires content or messages.")

        if not isinstance(marker, str) or marker != MASKING_MARKER:
            raise ValueError("A masked prompt must carry the Covenant Radar masking marker.")
        if version is not None and not _valid_version(version):
            raise ValueError("A masked prompt version must be non-empty text.")
        if prompt_version is not None and not _valid_version(prompt_version):
            raise ValueError("A masked prompt version must be non-empty text.")
        if version is not None and prompt_version is not None and version != prompt_version:
            raise ValueError("version and prompt_version must agree.")

        normalized_fields = _immutable_mapping(fields)
        normalized_tokens = _immutable_string_mapping(token_map)
        object.__setattr__(self, "messages", _coerce_messages(supplied_messages))
        object.__setattr__(self, "version", version if version is not None else prompt_version)
        object.__setattr__(self, "marker", marker)
        object.__setattr__(self, "_fields", normalized_fields)
        object.__setattr__(self, "_token_map", normalized_tokens)

    @property
    def content(self) -> str:
        """Return all message text for diagnostics and version checks."""

        return "\n".join(message.content for message in self.messages)

    @property
    def prompt_version(self) -> str | None:
        """Compatibility spelling used by prompt loaders and the client."""

        return self.version

    @property
    def masking_marker(self) -> str:
        """Compatibility spelling accepted by older call-site integrations."""

        return self.marker

    @property
    def fields(self) -> Mapping[str, object]:
        """Return the flattened, masked fields; original values are absent."""

        return self._fields

    @property
    def masked_fields(self) -> Mapping[str, object]:
        """Alias for callers that want to make the masking explicit."""

        return self._fields

    @property
    def token_map(self) -> Mapping[str, str]:
        """Return host-only original-to-token mappings.

        The mapping is immutable and never included in :attr:`messages`.
        Callers should use it only for local trace reconstruction.
        """

        return self._token_map


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Declared type contract for one admitted leaf field."""

    kind: str
    description: str


# The field names are deliberately explicit.  In particular, borrower,
# facility, contact, account and free-form reason fields are not aliases here:
# adding one without changing this table must fail closed.
OUTBOUND_FIELD_SPECS: Final[Mapping[str, FieldSpec]] = MappingProxyType(
    {
        "ratio_name": FieldSpec("text", "non-empty text"),
        "value": FieldSpec("number", "a finite number"),
        "threshold": FieldSpec("number", "a finite number"),
        "headroom": FieldSpec("number", "a finite number"),
        "evidence_type": FieldSpec("text", "non-empty text"),
        "evidence_count": FieldSpec("count", "a non-negative integer"),
        "evidence_counts": FieldSpec("count", "a non-negative integer"),
        "count": FieldSpec("count", "a non-negative integer"),
        "counts": FieldSpec("count", "a non-negative integer"),
        "materiality": FieldSpec("number", "a finite number"),
        "probability": FieldSpec("number", "a finite number"),
        "confidence": FieldSpec("number", "a finite number"),
        "crossing_date": FieldSpec("date", "an ISO date or date object"),
        "driver_name": FieldSpec("name", "non-empty name text"),
        "driver_names": FieldSpec("names", "a sequence of non-empty name text"),
        "drivers": FieldSpec("names", "a sequence of non-empty name text"),
        # Stage 7's fixed template carries these explicitly named, record-
        # backed values.  They are text summaries rather than arbitrary JSON
        # so adding a new memo field still requires an explicit whitelist
        # entry and a corresponding prompt-version change.
        "situation": FieldSpec("text", "non-empty situation text"),
        "evidence_counts_text": FieldSpec("text", "serialized evidence counts"),
        "simulation_options_text": FieldSpec("text", "serialized simulation options"),
        "recommended_interventions_text": FieldSpec("text", "serialized recommended interventions"),
        "action_ids": FieldSpec("names", "a sequence of permitted action ids"),
        "action_roles": FieldSpec("names", "a sequence of permitted action role tags"),
        "intervention_text": FieldSpec("text", "non-empty text"),
        "clause_text": FieldSpec("text", "non-empty text"),
    }
)

# Friendly aliases for inspection and for code that used the shorter name in
# early prototypes.  Both objects are immutable and point to the same schema.
FIELD_WHITELIST: Final[Mapping[str, FieldSpec]] = OUTBOUND_FIELD_SPECS
WHITELIST: Final[Mapping[str, FieldSpec]] = OUTBOUND_FIELD_SPECS

_EVIDENCE_LEAF_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {"type": "evidence_type", "count": "evidence_count", "counts": "evidence_counts"}
)

# Patterns are intentionally limited to identifiers, credentials and contact
# values.  Financial numbers and ISO dates must remain readable as derived
# values, so there is no generic "long number" rule.
_IDENTIFIER_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\w)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\w)"),
    re.compile(r"(?<!\w)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\w)"),
    re.compile(r"(?<!\w)\d{2}[A-Z]{5}\d{4}[A-Z]\dZ[A-Z0-9](?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)[A-Z]{5}\d{4}[A-Z](?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)[UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)[A-Z]{4}0[A-Z0-9]{6}(?!\w)", re.IGNORECASE),
    re.compile(
        r"(?<!\w)[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(r"(?i)\b(?:account|acct|a/c)(?:\s*(?:number|no\.?|#))?\s*[:#-]?\s*\d{8,20}\b"),
)


def build_outbound(
    fields: Mapping[str, object],
    secret: object | None = None,
    *,
    configured_secret: object | None = None,
    secret_value: object | None = None,
    secrets: Sequence[object] | None = None,
    prompt_version: str | None = None,
) -> MaskedPrompt:
    """Validate, mask and serialize one provider-bound field mapping.

    ``secret`` is injectable for tests and for application composition.  When
    no explicit secret is supplied, the configured model API key is read from
    ``COVENANT_RADAR_AI_API_KEY`` without importing settings or forcing model
    configuration during offline startup.  ``configured_secret`` and
    ``secret_value`` are accepted as descriptive aliases; supplying more than
    one singular value is an error.

    The function performs all validation before constructing the prompt.  A
    caller therefore never receives a partially masked or partially accepted
    result after a whitelist or type failure.
    """

    if not isinstance(fields, Mapping):
        raise TypeError("Outbound fields must be a mapping.")
    singular_values = [
        value for value in (secret, configured_secret, secret_value) if value is not None
    ]
    if len(singular_values) > 1:
        raise TypeError("Pass only one of secret, configured_secret or secret_value.")

    flattened = _flatten_fields(fields)
    if not flattened:
        raise ValueError("Outbound fields must contain at least one leaf field.")
    if len(flattened) > _MAX_FIELDS:
        raise ValueError(f"Outbound fields exceed the {_MAX_FIELDS}-field limit.")

    validated: dict[str, object] = {}
    for path, value in flattened:
        field_name = _canonical_field_name(path)
        spec = OUTBOUND_FIELD_SPECS.get(field_name)
        if spec is None:
            raise FieldNotWhitelisted(path)
        validated[path] = _validate_value(path, field_name, spec, value)

    secret_values = _resolve_secrets(
        singular_values[0] if singular_values else None,
        secrets,
    )
    names = _collect_names(validated)
    masked_fields: dict[str, object] = {}
    token_map: dict[str, str] = {}
    for name in names:
        if any(secret.casefold() in name.casefold() for secret in secret_values):
            continue
        token_map[name] = _new_token("ROLE_DRIVER", token_map)
    for path in sorted(validated):
        masked_fields[path] = _mask_value(
            validated[path],
            names=names,
            secret_values=secret_values,
            token_map=token_map,
        )

    content = json.dumps(
        {"fields": masked_fields},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return MaskedPrompt(
        content=content,
        version=prompt_version,
        fields=masked_fields,
        token_map=token_map,
    )


def _flatten_fields(fields: Mapping[str, object]) -> list[tuple[str, object]]:
    flattened: list[tuple[str, object]] = []
    for key, value in fields.items():
        if not isinstance(key, str) or _FIELD_NAME_RE.fullmatch(key) is None:
            raise FieldNotWhitelisted(key)
        _flatten_value(value, key, flattened, depth=0)
    return flattened


def _flatten_value(
    value: object,
    path: str,
    output: list[tuple[str, object]],
    *,
    depth: int,
) -> None:
    if len(path) > _MAX_FIELD_PATH_LENGTH:
        raise ValueError(f"Outbound field path {path!r} exceeds the length limit.")
    if isinstance(value, Mapping):
        if depth >= _MAX_NESTING_DEPTH:
            raise ValueError(f"Outbound field {path!r} exceeds the nesting limit.")
        field_name = _canonical_field_name(path)
        spec = OUTBOUND_FIELD_SPECS.get(field_name)
        if spec is not None:
            raise FieldTypeNotAllowed(path, spec.description, value)
        if not value:
            raise ValueError(f"Outbound field {path!r} contains an empty mapping.")
        for key, child in value.items():
            if not isinstance(key, str) or _FIELD_NAME_RE.fullmatch(key) is None:
                raise FieldNotWhitelisted(f"{path}.{key}")
            _flatten_value(child, f"{path}.{key}", output, depth=depth + 1)
        return
    if len(output) >= _MAX_FIELDS:
        raise ValueError(f"Outbound fields exceed the {_MAX_FIELDS}-field limit.")
    output.append((path, value))


def _canonical_field_name(path: str) -> str:
    leaf = path.rsplit(".", 1)[-1]
    parent = path.rsplit(".", 2)[-2] if "." in path else ""
    if parent.casefold() == "evidence" and leaf.casefold() in _EVIDENCE_LEAF_ALIASES:
        return _EVIDENCE_LEAF_ALIASES[leaf.casefold()]
    return leaf


def _validate_value(path: str, field_name: str, spec: FieldSpec, value: object) -> object:
    if value is None:
        return None
    kind = spec.kind
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise FieldTypeNotAllowed(path, spec.description, value)
        if len(value) > _MAX_TEXT_LENGTH:
            raise ValueError(f"Outbound field {path!r} exceeds the text length limit.")
        return value
    if kind == "number":
        if not _is_finite_number(value):
            raise FieldTypeNotAllowed(path, spec.description, value)
        return _number_text(cast(Decimal | int | float, value))
    if kind == "count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise FieldTypeNotAllowed(path, spec.description, value)
        return value
    if kind == "date":
        if isinstance(value, datetime) or not isinstance(value, date | str):
            raise FieldTypeNotAllowed(path, spec.description, value)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError as error:
                raise FieldTypeNotAllowed(path, spec.description, value) from error
        return value.isoformat()
    if kind == "name":
        if not isinstance(value, str) or not value.strip():
            raise FieldTypeNotAllowed(path, spec.description, value)
        if len(value) > _MAX_TEXT_LENGTH:
            raise ValueError(f"Outbound field {path!r} exceeds the text length limit.")
        return value
    if kind == "names":
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise FieldTypeNotAllowed(path, spec.description, value)
        if len(value) > _MAX_NAMES:
            raise ValueError(f"Outbound field {path!r} contains too many names.")
        names: list[str] = []
        for index, name in enumerate(value):
            if not isinstance(name, str) or not name.strip():
                raise FieldTypeNotAllowed(f"{path}[{index}]", "non-empty name text", name)
            if len(name) > _MAX_TEXT_LENGTH:
                raise ValueError(f"Outbound field {path}[{index}] exceeds the text length limit.")
            names.append(name)
        return names
    raise RuntimeError(f"Outbound field schema contains unsupported kind {kind!r}.")


def _resolve_secrets(
    singular: object | None,
    additional: Sequence[object] | None,
) -> tuple[str, ...]:
    raw_values: list[object] = []
    if singular is not None:
        raw_values.append(singular)
    elif additional is None:
        environment_secret = os.environ.get(_SECRET_ENVIRONMENT_VARIABLE)
        if environment_secret:
            raw_values.append(environment_secret)
    if additional is not None:
        if isinstance(additional, str | bytes | bytearray):
            raise TypeError("secrets must be a sequence of secret values, not text.")
        raw_values.extend(additional)
    if len(raw_values) > _MAX_SECRETS:
        raise ValueError(f"At most {_MAX_SECRETS} outbound secrets may be configured.")

    normalized: list[str] = []
    for value in raw_values:
        candidate = _secret_text(value)
        if candidate is None:
            continue
        if len(candidate) > _MAX_SECRET_LENGTH:
            raise ValueError("Configured outbound secret exceeds the length limit.")
        if candidate not in normalized:
            normalized.append(candidate)
    return tuple(sorted(normalized, key=lambda item: (-len(item), item)))


def _secret_text(value: object) -> str | None:
    getter = getattr(value, "get_secret_value", None)
    candidate = getter() if callable(getter) else value
    if not isinstance(candidate, str):
        raise TypeError("Configured outbound secret must be text.")
    if not candidate:
        raise ValueError("Configured outbound secret must not be empty.")
    return candidate


def _collect_names(fields: Mapping[str, object]) -> tuple[str, ...]:
    collected: list[str] = []
    for path, value in fields.items():
        field_name = _canonical_field_name(path)
        if field_name not in {"driver_name", "driver_names", "drivers"}:
            continue
        values = [value] if field_name == "driver_name" else value
        if not isinstance(values, Sequence) or isinstance(values, str):
            continue
        for name in values:
            if isinstance(name, str) and name not in collected:
                collected.append(name)
    if len(collected) > _MAX_NAMES:
        raise ValueError(f"Outbound fields contain more than {_MAX_NAMES} names.")
    if sum(len(name) for name in collected) > _MAX_NAME_PATTERN_LENGTH:
        raise ValueError(
            f"Outbound driver names exceed the {_MAX_NAME_PATTERN_LENGTH}-character limit."
        )
    return tuple(sorted(collected, key=lambda item: (-len(item), item.casefold(), item)))


def _mask_value(
    value: object,
    *,
    names: Sequence[str],
    secret_values: Sequence[str],
    token_map: dict[str, str],
) -> object:
    if isinstance(value, str):
        return _mask_text(value, names, secret_values, token_map)
    if isinstance(value, list):
        return [
            _mask_value(item, names=names, secret_values=secret_values, token_map=token_map)
            for item in value
        ]
    return value


def _mask_text(
    value: str,
    names: Sequence[str],
    secret_values: Sequence[str],
    token_map: dict[str, str],
) -> str:
    replacements: dict[str, str] = {}
    masked = value

    # Sentinel values prevent a name such as "ID" or a secret that happens to
    # contain a token-looking word from modifying a replacement made earlier.
    for secret in secret_values:
        pattern = re.compile(re.escape(secret), re.IGNORECASE)
        masked = pattern.sub(lambda match: _sentinel(replacements, REDACTION_TOKEN), masked)

    for pattern in _IDENTIFIER_PATTERNS:
        masked = pattern.sub(
            lambda match: _identifier_replacement(match.group(0), replacements, token_map),
            masked,
        )

    if names:
        name_pattern = re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(name) for name in names) + r")(?!\w)",
            re.IGNORECASE,
        )

        def replace_name(match: re.Match[str]) -> str:
            original = match.group(0)
            token = _lookup_token(token_map, original)
            if token is None:
                token = _new_token("ROLE_DRIVER", token_map)
                token_map[original] = token
            return _sentinel(replacements, token)

        masked = name_pattern.sub(replace_name, masked)

    for sentinel, replacement in replacements.items():
        masked = masked.replace(sentinel, replacement)
    return masked


def _identifier_replacement(
    original: str,
    replacements: dict[str, str],
    token_map: dict[str, str],
) -> str:
    token = _lookup_token(token_map, original)
    if token is None:
        token = _new_token("OPAQUE_ID", token_map)
        token_map[original] = token
    return _sentinel(replacements, token)


def _sentinel(replacements: dict[str, str], replacement: str) -> str:
    sentinel = f"\ue000CR{len(replacements):04d}\ue001"
    replacements[sentinel] = replacement
    return sentinel


def _new_token(prefix: str, token_map: Mapping[str, str]) -> str:
    if len(token_map) >= _MAX_TOKEN_MAP_ENTRIES:
        raise ValueError(f"Outbound token map exceeds the {_MAX_TOKEN_MAP_ENTRIES}-entry limit.")
    number = 1
    existing = set(token_map.values())
    while (candidate := f"{prefix}_{number}") in existing:
        number += 1
    return candidate


def _lookup_token(token_map: Mapping[str, str], original: str) -> str | None:
    direct = token_map.get(original)
    if direct is not None:
        return direct
    folded = original.casefold()
    for existing, token in token_map.items():
        if existing.casefold() == folded:
            return token
    return None


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
        return False
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, float):
        return math.isfinite(value)
    return True


def _number_text(value: Decimal | int | float) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    return str(value)


def _valid_version(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 128


def _coerce_messages(messages: Sequence[MessageInput]) -> tuple[PromptMessage, ...]:
    if isinstance(messages, str | bytes | bytearray):
        raise TypeError("Masked prompt messages must be a sequence of messages.")
    normalized: list[PromptMessage] = []
    for message in messages:
        if isinstance(message, PromptMessage):
            normalized.append(message)
            continue
        if not isinstance(message, Mapping):
            raise TypeError("Masked prompt messages must be PromptMessage values or mappings.")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"}:
            raise ValueError("Masked prompt message role must be system, user or assistant.")
        if not isinstance(content, str):
            raise TypeError("Masked prompt message content must be text.")
        normalized.append(PromptMessage(role=cast(MessageRole, role), content=content))
    if not normalized:
        raise ValueError("A masked prompt requires at least one message.")
    if sum(len(message.content) for message in normalized) > _MAX_REQUEST_CONTENT_LENGTH:
        raise ValueError("Masked prompt content exceeds the 4 MiB limit.")
    return tuple(normalized)


def _immutable_mapping(values: Mapping[str, object] | None) -> Mapping[str, object]:
    if values is None:
        return MappingProxyType({})
    if not isinstance(values, Mapping):
        raise TypeError("Masked prompt fields must be a mapping.")
    return MappingProxyType({key: _freeze_value(value) for key, value in values.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


def _immutable_string_mapping(values: Mapping[str, str] | None) -> Mapping[str, str]:
    if values is None:
        return MappingProxyType({})
    if not isinstance(values, Mapping):
        raise TypeError("Masked prompt token_map must be a mapping.")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("Masked prompt token_map keys and values must be text.")
        normalized[key] = value
    return MappingProxyType(normalized)


__all__ = [
    "FIELD_WHITELIST",
    "FieldNotWhitelisted",
    "FieldSpec",
    "FieldTypeNotAllowed",
    "MASKING_MARKER",
    "MaskedPrompt",
    "OUTBOUND_FIELD_SPECS",
    "REDACTION_TOKEN",
    "WHITELIST",
    "build_outbound",
]
