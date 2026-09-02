"""UUIDv7 primary keys and the stable, human-facing reference format.

Every entity gets a UUIDv7 (RFC 9562) as its primary key: sortable by
creation order, so an index on it is not a write hot-spot the way a random
UUIDv4 would be. Where a person needs to read, say or type the identifier,
a separate short reference (``B-000123``, ``CV-000456``) is used instead;
it is minted from a database sequence by the caller and is never reused.
"""

from __future__ import annotations

import secrets
import threading
import time
from uuid import UUID

_VERSION = 0x7
_VARIANT = 0b10

_SEQUENCE_BITS = 12
_SEQUENCE_MASK = (1 << _SEQUENCE_BITS) - 1
_RANDOM_BITS = 62

_lock = threading.Lock()
_last_timestamp_ms = 0
_sequence = 0


def new_id() -> UUID:
    """Return a new UUIDv7, strictly increasing even within one millisecond.

    Thread-safe: a monotonic counter breaks ties when two identifiers are
    minted in the same millisecond, and the counter's overflow forces the
    timestamp field forward rather than ever going backwards or repeating.
    """
    global _last_timestamp_ms, _sequence

    with _lock:
        timestamp_ms = time.time_ns() // 1_000_000
        if timestamp_ms > _last_timestamp_ms:
            _last_timestamp_ms = timestamp_ms
            _sequence = secrets.randbits(_SEQUENCE_BITS)
        else:
            _sequence += 1
            if _sequence > _SEQUENCE_MASK:
                _last_timestamp_ms += 1
                _sequence = secrets.randbits(_SEQUENCE_BITS)
        timestamp_ms = _last_timestamp_ms
        sequence = _sequence

    random_bits = secrets.randbits(_RANDOM_BITS)
    value = timestamp_ms << 80
    value |= _VERSION << 76
    value |= (sequence & _SEQUENCE_MASK) << 64
    value |= _VARIANT << 62
    value |= random_bits
    return UUID(int=value)


def human_reference(prefix: str, sequence: int) -> str:
    """Format a stable, never-reused reference such as ``B-000123``.

    `sequence` is expected to come from a database sequence or an
    equivalent monotonic source; this function only formats it.
    """
    if not prefix:
        raise ValueError("human_reference requires a non-empty prefix.")
    if sequence < 1:
        raise ValueError(f"human_reference requires a positive sequence, got {sequence}.")
    return f"{prefix}-{sequence:06d}"
