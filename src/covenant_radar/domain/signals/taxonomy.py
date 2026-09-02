"""Closed taxonomy and immutable value object for behavioural signals.

``signal_event.content_hash`` is based on the event's natural key, not on the
source that delivered it.  This is the important idempotence property: two
independent adapters describing the same event produce the same identity.

The payload remains extensible for source-specific context, but the fields
that downstream scoring needs are closed and typed here.  Unknown families,
event types, units, missing required fields, non-JSON values, and non-finite
numeric values fail before persistence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast
from uuid import UUID

from covenant_radar.core.errors import ValidationError


class SignalFamily(StrEnum):
    """The seven signal families accepted by the product."""

    ACCOUNT_ACTIVITY = "account_activity"
    PAYMENT = "payment"
    UTILISATION = "utilisation"
    TREASURY = "treasury"
    CONCENTRATION = "concentration"
    INDUSTRY = "industry"
    NEWS = "news"


FAMILIES: Final[tuple[str, ...]] = tuple(family.value for family in SignalFamily)
# Public vocabulary alias matching the reference-data and connector naming.
SIGNAL_FAMILIES: Final[tuple[str, ...]] = FAMILIES

# The reference generator and all first-party sources use one stable type per
# family.  Adding a type is an intentional taxonomy change, not an accidental
# acceptance of an arbitrary string from a source.
FAMILY_EVENT_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "account_activity": "account_activity_change",
        "payment": "payment_delay",
        "utilisation": "facility_utilisation",
        "treasury": "treasury_outflow",
        "concentration": "concentration_exposure",
        "industry": "industry_indicator",
        "news": "news_event",
    }
)

FAMILY_UNITS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "account_activity": "%",
        "payment": "days",
        "utilisation": "%",
        "treasury": "ratio",
        "concentration": "%",
        "industry": "score",
        "news": "score",
    }
)

_FAMILY_VALUE_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "account_activity": "activity_change_pct",
        "payment": "days_past_due",
        "utilisation": "utilisation_pct",
        "treasury": "cash_outflow_ratio",
        "concentration": "top_group_exposure_pct",
        "industry": "industry_stress_score",
        "news": "news_risk_score",
    }
)

REQUIRED_PAYLOAD_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {family: (value_field, "is_adverse") for family, value_field in _FAMILY_VALUE_FIELDS.items()}
)

EVENT_TYPES: Final[frozenset[str]] = frozenset(FAMILY_EVENT_TYPES.values())

_MAX_PAYLOAD_KEYS = 100
_MAX_PAYLOAD_DEPTH = 10
_MAX_CANONICAL_PAYLOAD_BYTES = 256_000
_MAX_TEXT_LENGTH = 100_000


class SignalTaxonomyError(ValidationError):
    """A signal violates the closed domain contract."""

    code = "signal_taxonomy_error"


@dataclass(frozen=True, slots=True)
class SignalTypeDefinition:
    """Schema metadata for one accepted event type."""

    family: str
    event_type: str
    unit: str
    required_payload_fields: tuple[str, ...]
    value_field: str


_EVENT_DEFINITIONS: Final[Mapping[str, SignalTypeDefinition]] = MappingProxyType(
    {
        event_type: SignalTypeDefinition(
            family=family,
            event_type=event_type,
            unit=FAMILY_UNITS[family],
            required_payload_fields=REQUIRED_PAYLOAD_FIELDS[family],
            value_field=_FAMILY_VALUE_FIELDS[family],
        )
        for family, event_type in FAMILY_EVENT_TYPES.items()
    }
)


def definition_for(family: str, event_type: str | None = None) -> SignalTypeDefinition:
    """Return the definition for ``family`` and optionally ``event_type``.

    Keeping this lookup strict means callers cannot accidentally validate a
    known event type under a different family.
    """

    if not isinstance(family, str) or family not in FAMILIES:
        raise SignalTaxonomyError(f"Unknown signal family {family!r}.", field="signal_event.family")
    expected_type = FAMILY_EVENT_TYPES[family]
    if event_type is not None and event_type != expected_type:
        raise SignalTaxonomyError(
            f"Signal family {family!r} requires event type {expected_type!r}; "
            f"received {event_type!r}.",
            field="signal_event.event_type",
        )
    return _EVENT_DEFINITIONS[expected_type]


def required_payload_fields(family_or_event_type: str) -> tuple[str, ...]:
    """Return the required payload fields for a family or event type."""

    if family_or_event_type in REQUIRED_PAYLOAD_FIELDS:
        return REQUIRED_PAYLOAD_FIELDS[family_or_event_type]
    definition = _EVENT_DEFINITIONS.get(family_or_event_type)
    if definition is None:
        raise SignalTaxonomyError(
            f"Unknown signal family or event type {family_or_event_type!r}.",
            field="signal_event.event_type",
        )
    return definition.required_payload_fields


def _canonical_value(value: object, *, depth: int = 0) -> object:
    """Convert supported values into deterministic JSON-compatible values."""

    if depth > _MAX_PAYLOAD_DEPTH:
        raise SignalTaxonomyError(
            "Signal payload nesting is too deep.", field="signal_event.payload"
        )
    if value is None or isinstance(value, bool | int | str):
        if isinstance(value, str) and len(value) > _MAX_TEXT_LENGTH:
            raise SignalTaxonomyError(
                "Signal payload text values are too long.", field="signal_event.payload"
            )
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise SignalTaxonomyError(
                "Signal payload floats must be finite.", field="signal_event.payload"
            )
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SignalTaxonomyError(
                "Signal payload decimals must be finite.", field="signal_event.payload"
            )
        return format(value, "f")
    if isinstance(value, UUID | date) and not isinstance(value, datetime):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, Mapping):
        if len(value) > _MAX_PAYLOAD_KEYS:
            raise SignalTaxonomyError(
                f"Signal payload cannot contain more than {_MAX_PAYLOAD_KEYS} keys.",
                field="signal_event.payload",
            )
        result: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or not key.strip():
                raise SignalTaxonomyError(
                    "Signal payload keys must be non-empty strings.",
                    field="signal_event.payload",
                )
            if any(ord(character) < 32 or ord(character) == 127 for character in key):
                raise SignalTaxonomyError(
                    "Signal payload keys cannot contain control characters.",
                    field="signal_event.payload",
                )
            result[key] = _canonical_value(nested, depth=depth + 1)
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, list | tuple):
        if len(value) > _MAX_PAYLOAD_KEYS:
            raise SignalTaxonomyError(
                f"Signal payload arrays cannot contain more than {_MAX_PAYLOAD_KEYS} values.",
                field="signal_event.payload",
            )
        return [_canonical_value(item, depth=depth + 1) for item in value]
    raise SignalTaxonomyError(
        f"Signal payload contains unsupported value type {type(value).__name__}.",
        field="signal_event.payload",
    )


def canonical_json(value: object) -> str:
    """Return the canonical JSON representation used for signal identity."""

    try:
        encoded = json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        if isinstance(error, SignalTaxonomyError):
            raise
        raise SignalTaxonomyError(
            "Signal natural key cannot be represented as canonical JSON.",
            field="signal_event.payload",
        ) from error
    if len(encoded.encode("utf-8")) > _MAX_CANONICAL_PAYLOAD_BYTES:
        raise SignalTaxonomyError("Signal payload is too large.", field="signal_event.payload")
    return encoded


def compute_content_hash(
    *,
    borrower_id: UUID,
    facility_id: UUID | None,
    event_date: date,
    family: str,
    event_type: str,
    magnitude: Decimal,
    unit: str,
    payload: Mapping[str, object],
) -> str:
    """Compute the source-independent SHA-256 natural-key hash."""

    natural_key = {
        "borrower_id": borrower_id,
        "facility_id": facility_id,
        "event_date": event_date,
        "family": family,
        "event_type": event_type,
        "magnitude": magnitude,
        "unit": unit,
        "payload": payload,
    }
    return hashlib.sha256(canonical_json(natural_key).encode("utf-8")).hexdigest()


signal_content_hash = compute_content_hash


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool | float):
        raise SignalTaxonomyError(f"{field} must be an exact decimal value.", field=field)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | str):
        try:
            result = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise SignalTaxonomyError(
                f"{field} must be a valid decimal value.", field=field
            ) from error
    else:
        raise SignalTaxonomyError(f"{field} must be a decimal value.", field=field)
    if not result.is_finite():
        raise SignalTaxonomyError(f"{field} must be finite.", field=field)
    return result


def _uuid(value: object, field: str, *, nullable: bool = False) -> UUID | None:
    if value is None and nullable:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise SignalTaxonomyError(f"{field} must be a UUID.", field=field) from error
    raise SignalTaxonomyError(f"{field} must be a UUID.", field=field)


def _event_date(value: object) -> date:
    if isinstance(value, datetime):
        raise SignalTaxonomyError(
            "event_date must be a calendar date, not a datetime.",
            field="signal_event.event_date",
        )
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise SignalTaxonomyError(
                "event_date must be an ISO calendar date.",
                field="signal_event.event_date",
            ) from error
    raise SignalTaxonomyError(
        "event_date must be a calendar date.", field="signal_event.event_date"
    )


def _payload(definition: SignalTypeDefinition, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SignalTaxonomyError("payload must be an object.", field="signal_event.payload")
    canonical = cast(dict[str, object], _canonical_value(value))
    missing = tuple(field for field in definition.required_payload_fields if field not in canonical)
    if missing:
        missing_text = ", ".join(missing)
        raise SignalTaxonomyError(
            f"payload is missing required field(s): {missing_text}.",
            field=f"signal_event.payload.{missing[0]}",
        )
    if not isinstance(canonical["is_adverse"], bool):
        raise SignalTaxonomyError(
            "payload.is_adverse must be a boolean.", field="signal_event.payload.is_adverse"
        )
    value_field = definition.value_field
    if definition.family == SignalFamily.PAYMENT.value:
        days = canonical[value_field]
        if isinstance(days, bool) or not isinstance(days, int) or days < 0:
            raise SignalTaxonomyError(
                "payload.days_past_due must be a non-negative integer.",
                field="signal_event.payload.days_past_due",
            )
    else:
        _decimal(canonical[value_field], f"signal_event.payload.{value_field}")
    return MappingProxyType(canonical)


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """An immutable, validated event at the ingestion boundary."""

    borrower_id: UUID
    facility_id: UUID | None
    event_date: date
    family: str
    event_type: str
    magnitude: Decimal
    unit: str
    payload: Mapping[str, object]
    source_id: UUID | None = None
    is_late: bool = False
    content_hash: str | None = None

    def __post_init__(self) -> None:
        borrower_id = _uuid(self.borrower_id, "signal_event.borrower_id")
        facility_id = _uuid(self.facility_id, "signal_event.facility_id", nullable=True)
        assert borrower_id is not None
        event_date = _event_date(self.event_date)
        definition = definition_for(self.family, self.event_type)
        magnitude = _decimal(self.magnitude, "signal_event.magnitude")
        unit = self.unit
        if unit != definition.unit:
            raise SignalTaxonomyError(
                f"Signal family {definition.family!r} requires unit {definition.unit!r}.",
                field="signal_event.unit",
            )
        source_id = _uuid(self.source_id, "signal_event.source_id", nullable=True)
        if not isinstance(self.is_late, bool):
            raise SignalTaxonomyError("is_late must be a boolean.", field="signal_event.is_late")
        payload = _payload(definition, self.payload)
        expected_hash = compute_content_hash(
            borrower_id=borrower_id,
            facility_id=facility_id,
            event_date=event_date,
            family=definition.family,
            event_type=definition.event_type,
            magnitude=magnitude,
            unit=unit,
            payload=payload,
        )
        if self.content_hash is not None:
            if not isinstance(self.content_hash, str) or self.content_hash != expected_hash:
                raise SignalTaxonomyError(
                    "content_hash does not match the signal natural key.",
                    field="signal_event.content_hash",
                )
        object.__setattr__(self, "borrower_id", borrower_id)
        object.__setattr__(self, "facility_id", facility_id)
        object.__setattr__(self, "event_date", event_date)
        object.__setattr__(self, "family", definition.family)
        object.__setattr__(self, "event_type", definition.event_type)
        object.__setattr__(self, "magnitude", magnitude)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "content_hash", expected_hash)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        source_id: UUID | None = None,
    ) -> SignalEvent:
        """Build an event from a source-neutral mapping with strict fields."""

        if not isinstance(value, Mapping):
            raise SignalTaxonomyError("A signal row must be an object.", field="signal_event")
        required = (
            "borrower_id",
            "event_date",
            "family",
            "event_type",
            "magnitude",
            "unit",
            "payload",
        )
        for field in required:
            if field not in value:
                raise SignalTaxonomyError(f"{field} is required.", field=f"signal_event.{field}")
        allowed = frozenset((*required, "facility_id", "source_id", "is_late", "content_hash"))
        unknown = sorted(str(item) for item in set(value) - allowed)
        if unknown:
            raise SignalTaxonomyError(
                f"Unknown signal field(s): {', '.join(str(item) for item in unknown)}.",
                field="signal_event",
            )
        row_source_id = value.get("source_id", source_id)
        if row_source_id is None:
            row_source_id = source_id
        return cls(
            borrower_id=cast(UUID, value["borrower_id"]),
            facility_id=cast(UUID | None, value.get("facility_id")),
            event_date=cast(date, value["event_date"]),
            family=cast(str, value["family"]),
            event_type=cast(str, value["event_type"]),
            magnitude=cast(Decimal, value["magnitude"]),
            unit=cast(str, value["unit"]),
            payload=cast(Mapping[str, object], value["payload"]),
            source_id=cast(UUID | None, row_source_id),
            is_late=cast(bool, value.get("is_late", False)),
            content_hash=cast(str | None, value.get("content_hash")),
        )

    @property
    def natural_key(self) -> tuple[object, ...]:
        """The source-independent identity used for de-duplication."""

        return (
            self.borrower_id,
            self.facility_id,
            self.event_date,
            self.family,
            self.event_type,
            self.magnitude,
            self.unit,
            self.payload,
        )

    @property
    def hash(self) -> str:
        """A readable alias for the computed content hash."""

        assert self.content_hash is not None
        return self.content_hash


def validate_event(value: SignalEvent | Mapping[str, object]) -> SignalEvent:
    """Validate one domain event, returning the canonical immutable value."""

    if isinstance(value, SignalEvent):
        return value
    if isinstance(value, Mapping):
        return SignalEvent.from_mapping(value)
    raise SignalTaxonomyError("A signal row must be an object.", field="signal_event")


__all__ = [
    "EVENT_TYPES",
    "FAMILIES",
    "FAMILY_EVENT_TYPES",
    "FAMILY_UNITS",
    "REQUIRED_PAYLOAD_FIELDS",
    "SignalEvent",
    "SignalFamily",
    "SIGNAL_FAMILIES",
    "SignalTaxonomyError",
    "SignalTypeDefinition",
    "canonical_json",
    "compute_content_hash",
    "definition_for",
    "required_payload_fields",
    "signal_content_hash",
    "validate_event",
]
