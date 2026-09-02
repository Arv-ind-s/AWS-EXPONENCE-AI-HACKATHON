"""Portable custom column types.

Every type here is written once and behaves identically whether the bound
engine is PostgreSQL or SQLite, so a declarative model never branches on
dialect and the same schema exercised in development and in the offline
evaluation harness is the schema that runs in production (`spec §11.1`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.engine import Dialect
from sqlalchemy.types import DateTime, Numeric, TypeDecorator, TypeEngine

from covenant_radar.security.crypto import FieldEncryptor, HMACFingerprinter

_MONEY_PRECISION = 18
_MONEY_SCALE = 4
_MONEY_QUANTUM = Decimal("1").scaleb(-_MONEY_SCALE)


class GUID(TypeDecorator[UUID]):
    """A UUID key: native ``uuid`` on PostgreSQL, ``char(36)`` text on
    SQLite, always handed back to Python as a `uuid.UUID`."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PostgresUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: UUID | str | None, dialect: Dialect) -> UUID | str | None:
        if value is None:
            return None
        as_uuid = value if isinstance(value, UUID) else UUID(str(value))
        return as_uuid if dialect.name == "postgresql" else str(as_uuid)

    def process_result_value(self, value: Any, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value if isinstance(value, UUID) else UUID(str(value))


class AwareDateTime(TypeDecorator[datetime]):
    """A timezone-aware instant, always stored and returned as UTC.

    Refuses a naive `datetime` outright at bind time rather than guessing
    its zone — the caller must go through the injected `Clock`
    (`core/clock.py`), which only ever produces aware instants. SQLite has
    no timezone-aware storage of its own, so the offset is normalised to
    UTC before the naive-looking string is written, and reattached to the
    naive value SQLite hands back on read.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"AwareDateTime refuses a naive datetime: {value!r}.")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class MoneyAmount(TypeDecorator[Decimal]):
    """An exact ``numeric(18,4)`` amount.

    PostgreSQL stores it natively at that precision and scale. SQLite has
    no fixed-point decimal storage class — declaring the column ``NUMERIC``
    there would let SQLite's own type-affinity rules silently convert the
    value to an IEEE-754 float on write — so this type instead stores the
    amount as fixed-point text on SQLite and parses it back through
    `Decimal`, never through `float`, on the way out. Refuses a non-Decimal
    value outright, for the same reason `core.money.Money` does.
    """

    impl = Numeric(_MONEY_PRECISION, _MONEY_SCALE, asdecimal=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(Numeric(_MONEY_PRECISION, _MONEY_SCALE, asdecimal=True))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Decimal | str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(
                f"MoneyAmount requires a Decimal, not {type(value).__name__} ({value!r})."
            )
        quantized = value.quantize(_MONEY_QUANTUM)
        return format(quantized, "f") if dialect.name == "sqlite" else quantized

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class PortableJSON(TypeDecorator[Any]):
    """A JSON payload: ``jsonb`` on PostgreSQL, ``text`` on SQLite.

    Encoding and decoding always go through Python's own `json` module, so
    what is written is syntactically valid JSON by construction. SQLite
    cannot add a ``CHECK`` constraint to a table after it already exists,
    so the ``json_valid`` guard against a row written by something other
    than SQLAlchemy is added by the migration that creates each JSON
    column (`T-010`), not by this type.
    """

    impl = Text()
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.loads(value)


class EncryptedText(TypeDecorator[str]):
    """Text whose database representation is an authenticated ciphertext.

    The encryptor is supplied explicitly so application startup can load and
    validate keys once, then share the immutable service with all mapped
    columns.  A raw SQL query bypasses this result processor and therefore
    sees only the envelope, never the plaintext.
    """

    impl = Text
    cache_ok = True

    def __init__(self, encryptor: FieldEncryptor, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._encryptor = encryptor

    @property
    def encryptor(self) -> FieldEncryptor:
        """The validated cipher used by this column."""
        return self._encryptor

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is not None and not isinstance(value, str):
            raise TypeError(f"EncryptedText requires text, not {type(value).__name__}.")
        return self._encryptor.encrypt(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(
                f"EncryptedText received non-text database data: {type(value).__name__}."
            )
        return self._encryptor.decrypt(value)

    def process_literal_param(self, value: str | None, dialect: Dialect) -> str:
        """Ensure SQL literal compilation cannot place plaintext in a query."""
        encrypted = self.process_bind_param(value, dialect)
        if encrypted is None:
            raise TypeError("EncryptedText does not compile a NULL literal.")
        return encrypted

    def copy(self, **kwargs: Any) -> EncryptedText:
        return type(self)(self._encryptor, **kwargs)


class FingerprintType(TypeDecorator[str]):
    """A deterministic HMAC-backed text type for equality and uniqueness."""

    impl = String(64)
    cache_ok = True

    def __init__(self, fingerprinter: HMACFingerprinter, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fingerprinter = fingerprinter

    @property
    def fingerprinter(self) -> HMACFingerprinter:
        """The HMAC service used by this column."""
        return self._fingerprinter

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is not None and not isinstance(value, str):
            raise TypeError(f"FingerprintType requires text, not {type(value).__name__}.")
        return self._fingerprinter.fingerprint(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(
                f"FingerprintType received non-text database data: {type(value).__name__}."
            )
        return value

    def copy(self, **kwargs: Any) -> FingerprintType:
        return type(self)(self._fingerprinter, **kwargs)


# Explicit aliases keep the type names readable at call sites while allowing
# migrations and adapters to use the more descriptive canonical names above.
EncryptedString = EncryptedText
HMACFingerprint = FingerprintType
Fingerprint = FingerprintType


__all__ = [
    "AwareDateTime",
    "EncryptedString",
    "EncryptedText",
    "Fingerprint",
    "FingerprintType",
    "GUID",
    "HMACFingerprint",
    "MoneyAmount",
    "PortableJSON",
]
