"""Cursor pagination shared by every C-21 list resource.

The signed-seek-cursor shape here generalises two independent, nearly
identical implementations already in the codebase —
`web/view_models/audit.py`'s `AuditCursor` and
`db/repositories/triage.py`'s `QueueCursor` — into one reusable module so a
third near-duplicate is not written for this task's six new resources. A
cursor is opaque, HMAC-signed so a client cannot forge a seek position, and
bound to a digest of the request's filters so paging with a different filter
set is refused rather than silently reinterpreted (`C-21`: "a cursor from a
different filter set → refused").

Ordering is two-column keyset ("seek") pagination — a primary sort column
plus the row id as a tiebreaker — which every supported database (SQLite and
PostgreSQL) executes as a plain indexed range scan. Composite row-value
comparison (`(a, b) > (c, d)`) is deliberately avoided: it is not portable
across both engines this product runs on, so the seek predicate is instead
written out as the standard `col > val OR (col = val AND id > val)` form.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, TypeVar
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, or_
from sqlalchemy.orm import Session

from covenant_radar.core.errors import ValidationError

DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_SIZE: Final[int] = 200
_CURSOR_VERSION: Final[int] = 1
_CURSOR_SECRET_ENV: Final[str] = "COVENANT_RADAR_API_CURSOR_SECRET"
_PROCESS_CURSOR_SECRET: Final[bytes] = secrets.token_bytes(32)
_CURSOR_MAX_LENGTH: Final[int] = 512
_BASE64_ALPHABET: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

ModelT = TypeVar("ModelT")


class InvalidCursor(ValueError):
    """A cursor is malformed, unauthenticated, or carries invalid fields."""


@dataclass(frozen=True, slots=True)
class Cursor:
    """An authenticated seek position bound to one resource's filter set.

    ``primary`` is the last returned row's primary sort value, pre-serialised
    to text by the caller (``date``/``datetime`` as ISO-8601, an integer as
    its decimal text); ``id`` is that row's id, the tiebreaker for rows that
    share a primary value.
    """

    primary: str
    id: UUID
    filters_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.primary, str) or not 1 <= len(self.primary) <= 64:
            raise ValueError("Cursor primary must be text of 1 to 64 characters.")
        if not isinstance(self.id, UUID):
            raise TypeError("Cursor id must be a UUID.")
        if (
            not isinstance(self.filters_digest, str)
            or len(self.filters_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.filters_digest)
        ):
            raise ValueError("Cursor filters_digest must be a lowercase SHA-256 digest.")

    def encode(self, secret: bytes | str | None = None) -> str:
        """Return an opaque, tamper-evident URL-safe token."""
        payload = {
            "v": _CURSOR_VERSION,
            "primary": self.primary,
            "id": str(self.id),
            "filters_digest": self.filters_digest,
        }
        body = _urlsafe(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(_cursor_secret(secret), body, hashlib.sha256).digest()
        return f"{body.decode('ascii')}.{_urlsafe(signature).decode('ascii')}"

    @classmethod
    def decode(cls, token: str, secret: bytes | str | None = None) -> Cursor:
        """Verify and decode a cursor without trusting any client-supplied field."""
        if not isinstance(token, str) or not 1 <= len(token) <= _CURSOR_MAX_LENGTH:
            raise InvalidCursor("Cursor is malformed.")
        parts = token.split(".")
        if len(parts) != 2:
            raise InvalidCursor("Cursor is malformed.")
        try:
            encoded_body = parts[0].encode("ascii")
            body = _urlsafe_decode(parts[0])
            supplied_signature = _urlsafe_decode(parts[1])
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise InvalidCursor("Cursor is malformed.") from error
        expected_signature = hmac.new(_cursor_secret(secret), encoded_body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidCursor("Cursor authentication failed.")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidCursor("Cursor payload is malformed.") from error
        expected_keys = {"v", "primary", "id", "filters_digest"}
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise InvalidCursor("Cursor payload is malformed.")
        if payload.get("v") != _CURSOR_VERSION:
            raise InvalidCursor("Cursor version is unsupported.")
        try:
            row_id = UUID(payload["id"])
            return cls(
                primary=payload["primary"],
                id=row_id,
                filters_digest=payload["filters_digest"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidCursor("Cursor fields are malformed.") from error


def digest_filters(filters: Mapping[str, object]) -> str:
    """Return the stable SHA-256 binding used to reject a stale-filter cursor."""
    canonical = json.dumps(_json_safe(dict(filters)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def serialize_primary(value: date | datetime | int) -> str:
    """Serialise a row's primary sort value to the cursor's stable text form."""
    if isinstance(value, bool):
        raise TypeError("A boolean is not a valid cursor primary value.")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        return str(value)
    raise TypeError(f"Unsupported cursor primary type: {type(value).__name__}.")


@dataclass(frozen=True, slots=True)
class Page:
    """One page of scoped, filtered, cursor-paginated rows."""

    items: tuple[Any, ...]
    next_cursor: str | None


def paginate(
    session: Session,
    statement: Select[Any],
    *,
    # `InstrumentedAttribute[T]`, what a mapped model's column actually is at
    # every call site, is not a subtype of `ColumnElement[Any]` under
    # SQLAlchemy's stubs even though it behaves like one at runtime;
    # `db/scoping.py::Scope.predicate` accepts the same kind of value the
    # same way, as `Any`, rather than fighting that invariance here.
    primary_column: Any,
    id_column: Any,
    primary_of: Callable[[Any], date | datetime | int],
    primary_parse: Callable[[str], Any],
    cursor: str | None,
    filters_digest: str,
    page_size: int,
    descending: bool = True,
    secret: bytes | str | None = None,
) -> Page:
    """Apply a validated seek cursor to ``statement``, execute it, and page it.

    ``statement`` must already carry every scope and filter predicate; this
    function only adds the seek predicate, the deterministic order, and the
    limit, then decides whether another page follows.
    """
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}.")
    position = _decode(cursor, secret)
    if position is not None:
        if position.filters_digest != filters_digest:
            raise ValidationError(
                "The cursor does not match the current filters; request a fresh page "
                "without a cursor.",
                field="cursor",
            )
        value = primary_parse(position.primary)
        if descending:
            seek: ColumnElement[bool] = or_(
                primary_column < value,
                and_(primary_column == value, id_column < position.id),
            )
        else:
            seek = or_(
                primary_column > value,
                and_(primary_column == value, id_column > position.id),
            )
        statement = statement.where(seek)

    order = (
        (primary_column.desc(), id_column.desc())
        if descending
        else (primary_column.asc(), id_column.asc())
    )
    statement = statement.order_by(*order).limit(page_size + 1)
    rows = tuple(session.execute(statement).scalars().all())
    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = Cursor(
            primary=serialize_primary(primary_of(last)),
            id=last.id,
            filters_digest=filters_digest,
        ).encode(secret)
    return Page(items=page_rows, next_cursor=next_cursor)


def clamp_page_size(value: int | None, *, default: int = DEFAULT_PAGE_SIZE) -> int:
    """Return a validated page size, defaulting when the caller omits one."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE_SIZE:
        raise ValidationError(
            f"page_size must be between 1 and {MAX_PAGE_SIZE}.", field="page_size"
        )
    return value


def _decode(token: str | None, secret: bytes | str | None) -> Cursor | None:
    if token is None:
        return None
    try:
        return Cursor.decode(token, secret)
    except InvalidCursor as error:
        raise ValidationError(str(error), field="cursor") from error


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _cursor_secret(value: bytes | str | None) -> bytes:
    if value is None:
        configured = os.environ.get(_CURSOR_SECRET_ENV)
        return _cursor_secret(configured) if configured else _PROCESS_CURSOR_SECRET
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("API cursor secret must contain at least 32 bytes.")
    return value


def _urlsafe(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _urlsafe_decode(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or any(character not in _BASE64_ALPHABET for character in value)
    ):
        raise ValueError("Invalid base64 value.")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Cursor",
    "InvalidCursor",
    "Page",
    "clamp_page_size",
    "digest_filters",
    "paginate",
    "serialize_primary",
]
