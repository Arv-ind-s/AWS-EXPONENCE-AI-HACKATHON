"""Canonical audit payloads and hash-chain integrity checks.

The audit table is deliberately a very small storage format.  This module
keeps the parts that must be identical in every adapter independent of
SQLAlchemy: JSON validation, canonical encoding, digest construction, and
the description of the first integrity failure.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID


class AuditPayloadError(ValueError):
    """The payload violates the audit trail's safe, JSON-only shape."""


class PersonalDataRefused(AuditPayloadError):
    """A personal-class value was supplied instead of a reference."""


@dataclass(frozen=True, slots=True)
class PersonalReference:
    """A safe reference that may stand in for a personal-class value.

    The value itself is intentionally not retained.  The resulting JSON is
    only ``{"reference": ...}``, which keeps the audit trail useful without
    duplicating personal data from the source record.
    """

    reference: str


@dataclass(frozen=True, slots=True)
class PersonalValue:
    """Marker for a value that must never be copied into an audit payload."""

    value: object


_PERSONAL_FIELD_NAMES = frozenset(
    {
        "aadhaar",
        "address",
        "cin",
        "contact_email",
        "contact_name",
        "contact",
        "date_of_birth",
        "director_name",
        "email",
        "email_address",
        "first_name",
        "guarantor_name",
        "last_name",
        "mobile",
        "mobile_number",
        "passport",
        "pan",
        "person_name",
        "person",
        "personal",
        "personal_data",
        "personal_value",
        "phone",
        "phone_number",
        "promoter_name",
        "signatory_name",
        "staff_name",
        "tax_id",
    }
)
_REFERENCE_KEY = "reference"
_FIELD_ROOT = "payload"
_HASH_SEPARATOR = "\x1f"


def normalise_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate and copy one audit payload into JSON-compatible values.

    A mapping under a field classified as personal is accepted only when it
    is exactly a reference object.  This is intentionally stricter than a
    general JSON encoder: an audit record must not become a second store of
    names, contact details, or official identity values by accident.
    """

    if not isinstance(payload, Mapping):
        raise TypeError(
            f"Audit payload must be a mapping; field {_FIELD_ROOT!r} received "
            f"{type(payload).__name__}."
        )
    value = _normalise_value(payload, _FIELD_ROOT)
    if not isinstance(value, dict):
        raise TypeError("Audit payload root must be a JSON object at field 'payload'.")
    return value


def canonical_payload(payload: Mapping[str, object]) -> str:
    """Return the stable compact JSON representation used by the digest."""

    value = normalise_payload(payload)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_event_hash(
    sequence: int,
    occurred_at: datetime,
    actor: UUID | str | None,
    event_type: str,
    subject_type: str,
    subject_id: UUID | str,
    payload: Mapping[str, object],
    prev_hash: str | None,
) -> str:
    """Calculate the SHA-256 digest for one complete audit row.

    The unit-separator framing makes field boundaries unambiguous even when
    an event type or reference contains punctuation.  Datetimes are always
    represented as aware UTC instants with microsecond precision.
    """

    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError(f"Audit sequence must be a positive integer, got {sequence!r}.")
    timestamp = _utc_timestamp(occurred_at)
    canonical = canonical_payload(payload)
    material = _HASH_SEPARATOR.join(
        (
            str(sequence),
            timestamp,
            _actor_token(actor),
            _text_token(event_type, "event_type"),
            _text_token(subject_type, "subject_type"),
            _text_token(subject_id, "subject_id"),
            canonical,
            prev_hash if prev_hash is not None else "<null>",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# These aliases make the hashing primitive discoverable without creating a
# second implementation or a second write path.
hash_event = compute_event_hash
canonicalise_payload = canonical_payload
canonicalize_payload = canonical_payload
calculate_event_hash = compute_event_hash


class AuditChainRow(Protocol):
    """The row attributes required by :func:`verify_chain`."""

    sequence: int
    occurred_at: datetime
    actor_id: UUID | None
    actor_label: str | None
    event_type: str
    subject_type: str
    subject_id: UUID
    payload: Mapping[str, object]
    prev_hash: str | None
    hash: str


@dataclass(frozen=True, slots=True)
class AuditChainBreak:
    """The first broken link or digest, including both row sequences."""

    sequence: int
    previous_sequence: int | None
    reason: str
    expected_prev_hash: str | None
    actual_prev_hash: str | None
    expected_hash: str | None
    actual_hash: str | None

    @property
    def row_name(self) -> str:
        """Human-readable name of the row that failed verification."""

        return f"sequence {self.sequence}"

    @property
    def previous_row_name(self) -> str:
        """Human-readable name of the preceding row, when one exists."""

        if self.previous_sequence is None:
            return "no previous row"
        return f"sequence {self.previous_sequence}"

    @property
    def message(self) -> str:
        """Stable diagnostic suitable for an integrity report."""

        return (
            f"Audit chain break at {self.row_name} after {self.previous_row_name}: {self.reason}."
        )

    def __str__(self) -> str:
        return self.message


def verify_chain(
    rows: Sequence[AuditChainRow],
    from_sequence: int | None = None,
    to_sequence: int | None = None,
) -> AuditChainBreak | None:
    """Return the first chain failure, or ``None`` when all rows verify.

    ``rows`` must be ordered by sequence.  A caller checking a bounded range
    should include the row immediately before ``from_sequence`` when it is
    available; that lets the report name both sides of a broken link while
    still avoiding a false genesis failure for a partial range.
    """

    _validate_range(from_sequence, to_sequence)
    ordered_rows = tuple(rows)
    if from_sequence is None:
        selected = tuple(
            row for row in ordered_rows if to_sequence is None or row.sequence <= to_sequence
        )
    else:
        # Retain the latest row before the requested range as context.  It is
        # what lets a bounded verification identify both sides of a broken
        # link without incorrectly treating the range's first row as genesis.
        preceding = tuple(row for row in ordered_rows if row.sequence < from_sequence)
        in_range = tuple(
            row
            for row in ordered_rows
            if row.sequence >= from_sequence
            and (to_sequence is None or row.sequence <= to_sequence)
        )
        selected = ((max(preceding, key=lambda row: row.sequence),) if preceding else ()) + in_range
    if not selected:
        return None

    previous: AuditChainRow | None = None
    for row in selected:
        row_sequence = _row_sequence(row)
        if previous is None:
            if from_sequence is None and row.prev_hash is not None:
                return AuditChainBreak(
                    sequence=row_sequence,
                    previous_sequence=None,
                    reason="the first row has a previous hash",
                    expected_prev_hash=None,
                    actual_prev_hash=row.prev_hash,
                    expected_hash=None,
                    actual_hash=row.hash,
                )
        else:
            previous_sequence = _row_sequence(previous)
            if row_sequence <= previous_sequence:
                return AuditChainBreak(
                    sequence=row_sequence,
                    previous_sequence=previous_sequence,
                    reason="sequence is not strictly increasing",
                    expected_prev_hash=previous.hash,
                    actual_prev_hash=row.prev_hash,
                    expected_hash=None,
                    actual_hash=row.hash,
                )
            if row.prev_hash != previous.hash:
                return AuditChainBreak(
                    sequence=row_sequence,
                    previous_sequence=previous_sequence,
                    reason="previous hash does not match the preceding row",
                    expected_prev_hash=previous.hash,
                    actual_prev_hash=row.prev_hash,
                    expected_hash=None,
                    actual_hash=row.hash,
                )

        try:
            expected_hash = compute_event_hash(
                row_sequence,
                row.occurred_at,
                row.actor_id if row.actor_id is not None else row.actor_label,
                row.event_type,
                row.subject_type,
                row.subject_id,
                row.payload,
                row.prev_hash,
            )
        except (TypeError, ValueError, UnicodeError) as error:
            return AuditChainBreak(
                sequence=row_sequence,
                previous_sequence=_row_sequence(previous) if previous is not None else None,
                reason=f"row content is not hashable ({error})",
                expected_prev_hash=previous.hash if previous is not None else None,
                actual_prev_hash=row.prev_hash,
                expected_hash=None,
                actual_hash=row.hash,
            )
        if row.hash != expected_hash:
            return AuditChainBreak(
                sequence=row_sequence,
                previous_sequence=_row_sequence(previous) if previous is not None else None,
                reason="stored hash does not cover the stored row content",
                expected_prev_hash=previous.hash if previous is not None else None,
                actual_prev_hash=row.prev_hash,
                expected_hash=expected_hash,
                actual_hash=row.hash,
            )
        previous = row
    return None


def _normalise_value(value: object, path: str) -> object:
    if isinstance(value, PersonalValue):
        raise PersonalDataRefused(
            f"Audit payload field {path!r} contains personal-class data; pass a reference instead."
        )
    if isinstance(value, PersonalReference):
        return {"reference": _reference(value.reference, path)}
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"Audit payload field {path!r} has a non-string key {key!r}; "
                    "JSON object keys must be text."
                )
            child_path = f"{path}.{key}"
            if _is_personal_field(key):
                if not _is_reference_object(child):
                    raise PersonalDataRefused(
                        f"Audit payload field {child_path!r} contains a direct "
                        "personal-class value; pass a reference instead."
                    )
                result[key] = _reference_object(child, child_path)
            else:
                result[key] = _normalise_value(child, child_path)
        return result
    if isinstance(value, list | tuple):
        return [_normalise_value(child, f"{path}[{index}]") for index, child in enumerate(value)]
    if value is None or isinstance(value, bool | int | str):
        if isinstance(value, str):
            return _normalise_text(value, path)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(
                f"Audit payload field {path!r} is not JSON-serialisable: non-finite float."
            )
        return value
    raise TypeError(
        f"Audit payload field {path!r} is not JSON-serialisable: {type(value).__name__}."
    )


def _is_personal_field(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in _PERSONAL_FIELD_NAMES


def _is_reference_object(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {_REFERENCE_KEY}


def _reference_object(value: object, path: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PersonalDataRefused(f"Audit payload field {path!r} requires a reference object.")
    return {"reference": _reference(value.get(_REFERENCE_KEY), path)}


def _reference(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersonalDataRefused(f"Audit payload field {path!r} requires a non-empty reference.")
    return _normalise_text(value.strip(), f"{path}.reference")


def _normalise_text(value: str, path: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise TypeError(f"Audit payload field {path!r} contains an invalid Unicode surrogate.")
    return unicodedata.normalize("NFC", value)


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Audit occurred_at must be a timezone-aware datetime.")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _text_token(value: object, field: str) -> str:
    if not isinstance(value, str | UUID):
        raise TypeError(f"Audit {field} must be text or UUID, got {type(value).__name__}.")
    return _normalise_text(str(value), field)


def _actor_token(value: UUID | str | None) -> str:
    if value is None:
        return ""
    return _text_token(value, "actor")


def _row_sequence(row: AuditChainRow) -> int:
    sequence = row.sequence
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError(f"Audit row sequence must be a positive integer, got {sequence!r}.")
    return sequence


def _validate_range(from_sequence: int | None, to_sequence: int | None) -> None:
    for name, value in (("from_sequence", from_sequence), ("to_sequence", to_sequence)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{name} must be a positive integer or None.")
    if from_sequence is not None and to_sequence is not None and from_sequence > to_sequence:
        raise ValueError("from_sequence cannot be greater than to_sequence.")


__all__ = [
    "AuditChainBreak",
    "AuditChainRow",
    "AuditPayloadError",
    "PersonalDataRefused",
    "PersonalReference",
    "PersonalValue",
    "canonical_payload",
    "canonicalize_payload",
    "canonicalise_payload",
    "calculate_event_hash",
    "compute_event_hash",
    "hash_event",
    "normalise_payload",
    "verify_chain",
]
