"""Contracts and registry for source-neutral signal sources.

Signal adapters are deliberately narrower than the ingestion service.  They
read one source, map its fields to the canonical signal shape, and attach a
stable source identifier.  They do not open a database transaction or write
anything.  The service can therefore consume one source or a registry's
combined iterator through the same atomic path.

The registry wraps failures raised while a source is being iterated with the
source reference.  This is important operationally: an iterator can fail
after it has yielded valid rows, but the service must still roll the complete
batch back and report which source failed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, TypeVar, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from covenant_radar.core.errors import ExternalServiceError, ValidationError
from covenant_radar.domain.signals import SignalEvent

SignalInput = SignalEvent | Mapping[str, object]

_MAX_SOURCE_REFERENCE_LENGTH = 200
_MAX_MAPPING_FIELD_LENGTH = 200
_MAX_PAYLOAD_FIELD_LENGTH = 100
_SOURCE_ID_NAMESPACE = "https://covenant-radar.invalid/signal-source/"
_MISSING = object()

_CANONICAL_FIELDS = frozenset(
    {
        "borrower_id",
        "facility_id",
        "event_date",
        "family",
        "event_type",
        "magnitude",
        "unit",
        "payload",
        "source_id",
        "is_late",
        "content_hash",
    }
)
_OPTIONAL_FIELDS = frozenset({"facility_id", "source_id", "is_late", "content_hash"})


class SignalSourceError(ExternalServiceError):
    """A source could not be read or its source-level shape was invalid."""

    code = "signal_source_error"


class SignalSourceConfigurationError(ValidationError):
    """A source configuration cannot safely be used for ingestion."""

    code = "signal_source_configuration_error"


class SignalSource(Protocol):
    """Read-only seam implemented by every signal source adapter."""

    @property
    def source_reference(self) -> str:
        """Stable operator-facing reference for the source."""

    @property
    def source_id(self) -> UUID:
        """Stable identifier attached to events from this source."""

    def iter_events(self) -> Iterator[object]:
        """Yield canonical events, or malformed rows for quarantine."""


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """The registry metadata exposed for diagnostics and health views."""

    source_reference: str
    source_id: UUID


def source_identity(source_reference: object, source_id: object = None) -> tuple[str, UUID]:
    """Validate source metadata and derive a stable id when none is supplied.

    The derived UUID is local identity metadata only.  It is not part of a
    signal's natural key, so independently configured sources still converge
    on the same content hash for the same event.
    """

    reference = _text(source_reference, "source_reference", _MAX_SOURCE_REFERENCE_LENGTH)
    if source_id is None:
        resolved_id = uuid5(NAMESPACE_URL, _SOURCE_ID_NAMESPACE + reference)
    elif isinstance(source_id, UUID):
        resolved_id = source_id
    elif isinstance(source_id, str):
        try:
            resolved_id = UUID(source_id)
        except ValueError as error:
            raise SignalSourceConfigurationError(
                "source_id must be a UUID.", field="source_id"
            ) from error
    else:
        raise SignalSourceConfigurationError("source_id must be a UUID.", field="source_id")
    return reference, resolved_id


def validate_mapping(raw: Mapping[str, object] | None) -> Mapping[str, object]:
    """Validate a canonical-field-to-source-column mapping.

    The top-level keys are canonical signal fields.  ``payload`` may either
    name one JSON object column or contain a nested mapping from payload field
    names to source columns.  A mapping is mandatory for file sources and is
    intentionally validated before a file is opened.
    """

    if raw is None or not isinstance(raw, Mapping) or not raw:
        raise SignalSourceConfigurationError(
            "A signal source mapping is required before reading the source.", field="mapping"
        )

    expanded: dict[str, object] = {}
    flattened_payload: dict[str, object] = {}
    for raw_field_name, source_column in raw.items():
        if isinstance(raw_field_name, str) and raw_field_name.startswith("payload."):
            payload_field = raw_field_name.removeprefix("payload.")
            if not payload_field:
                raise SignalSourceConfigurationError(
                    "A flattened payload mapping field must not be blank.",
                    field="mapping.payload",
                )
            if payload_field in flattened_payload:
                raise SignalSourceConfigurationError(
                    f"Payload field {payload_field!r} is mapped more than once.",
                    field="mapping.payload",
                )
            flattened_payload[payload_field] = source_column
        else:
            if not isinstance(raw_field_name, str):
                raise SignalSourceConfigurationError(
                    "Signal source mapping field names must be non-empty strings.",
                    field="mapping",
                )
            expanded[raw_field_name] = source_column
    if flattened_payload:
        if "payload" in expanded:
            raise SignalSourceConfigurationError(
                "Use either mapping.payload or flattened payload fields, not both.",
                field="mapping.payload",
            )
        expanded["payload"] = flattened_payload

    unknown = sorted(str(key) for key in set(expanded) - _CANONICAL_FIELDS)
    if unknown:
        raise SignalSourceConfigurationError(
            f"Signal source mapping contains unknown field(s): {', '.join(unknown)}.",
            field="mapping",
        )

    required = _CANONICAL_FIELDS - _OPTIONAL_FIELDS
    missing = sorted(required - set(expanded))
    if missing:
        raise SignalSourceConfigurationError(
            f"Signal source mapping is missing field(s): {', '.join(missing)}.",
            field="mapping",
        )

    normalised: dict[str, object] = {}
    used_columns: dict[str, str] = {}
    for field_name, source_column in expanded.items():
        if not isinstance(field_name, str) or not field_name.strip():
            raise SignalSourceConfigurationError(
                "Signal source mapping field names must be non-empty strings.", field="mapping"
            )
        if field_name == "payload" and isinstance(source_column, Mapping):
            payload_mapping: dict[str, str] = {}
            for payload_field, payload_column in source_column.items():
                if not isinstance(payload_field, str) or not payload_field.strip():
                    raise SignalSourceConfigurationError(
                        "Payload mapping field names must be non-empty strings.",
                        field="mapping.payload",
                    )
                if len(payload_field) > _MAX_PAYLOAD_FIELD_LENGTH:
                    raise SignalSourceConfigurationError(
                        "A payload mapping field name is too long.", field="mapping.payload"
                    )
                column = _mapping_column(payload_column, f"mapping.payload.{payload_field}")
                _claim_column(used_columns, column, f"payload.{payload_field}")
                payload_mapping[payload_field] = column
            if not payload_mapping:
                raise SignalSourceConfigurationError(
                    "mapping.payload must contain at least one field.", field="mapping.payload"
                )
            normalised[field_name] = MappingProxyType(payload_mapping)
            continue

        column = _mapping_column(source_column, f"mapping.{field_name}")
        _claim_column(used_columns, column, field_name)
        normalised[field_name] = column

    return MappingProxyType(normalised)


def map_signal_row(
    raw: object,
    mapping: Mapping[str, object],
    *,
    source_id: UUID,
) -> object:
    """Map one raw row to the canonical event mapping.

    Missing row values are intentionally left missing.  The domain validator
    then turns that row into a quarantine record with the precise missing
    canonical field, while the source can continue with later rows.
    """

    if isinstance(raw, SignalEvent):
        if raw.source_id == source_id:
            return raw
        return SignalEvent(
            borrower_id=raw.borrower_id,
            facility_id=raw.facility_id,
            event_date=raw.event_date,
            family=raw.family,
            event_type=raw.event_type,
            magnitude=raw.magnitude,
            unit=raw.unit,
            payload=raw.payload,
            source_id=source_id,
            is_late=raw.is_late,
        )
    if not isinstance(raw, Mapping):
        return raw

    mapped: dict[str, object] = {}
    for field_name, source_column in mapping.items():
        if field_name == "payload" and isinstance(source_column, Mapping):
            payload: dict[str, object] = {}
            for payload_field, payload_column in source_column.items():
                value = raw.get(payload_column, _MISSING)
                if value is not _MISSING:
                    payload[payload_field] = _coerce_payload_value(payload_field, value)
            mapped["payload"] = payload
            continue
        value = raw.get(source_column, _MISSING)
        if value is not _MISSING:
            mapped[field_name] = _coerce_field_value(field_name, value)

    # A configured source owns the source identity.  A row cannot spoof it by
    # carrying a different source_id column.
    mapped["source_id"] = source_id
    return mapped


def validate_source_row(raw: object, mapping: Mapping[str, object], *, source_id: UUID) -> object:
    """Return a canonical event or a bounded malformed row for quarantine."""

    mapped = map_signal_row(raw, mapping, source_id=source_id)
    if isinstance(mapped, SignalEvent):
        return mapped
    if not isinstance(mapped, Mapping):
        return mapped
    try:
        return SignalEvent.from_mapping(mapped)
    except (TypeError, ValueError, ValidationError):
        return mapped


class SignalSourceRegistry:
    """Ordered registry and composition point for signal sources."""

    def __init__(self, sources: Iterable[SignalSource] = ()) -> None:
        self._sources: dict[str, SignalSource] = {}
        for source in sources:
            self.register(source)

    def register(self, source: SignalSource, *, replace: bool = False) -> None:
        """Register one source without changing the ingestion pipeline."""

        reference, source_id, iterator = _source_metadata(source)
        if reference in self._sources and not replace:
            raise SignalSourceConfigurationError(
                f"Signal source {reference!r} is already registered.",
                field="source_reference",
            )
        if not callable(iterator):
            raise SignalSourceConfigurationError(
                f"Signal source {reference!r} must expose iter_events().",
                field="source",
            )
        if not isinstance(source_id, UUID):
            raise SignalSourceConfigurationError(
                f"Signal source {reference!r} must expose a UUID source_id.", field="source_id"
            )
        self._sources[reference] = source

    def get(self, source_reference: str) -> SignalSource:
        """Return a registered source by its stable reference."""

        reference = _text(source_reference, "source_reference", _MAX_SOURCE_REFERENCE_LENGTH)
        try:
            return self._sources[reference]
        except KeyError as error:
            raise SignalSourceConfigurationError(
                f"Signal source {reference!r} is not registered.", field="source_reference"
            ) from error

    def names(self) -> tuple[str, ...]:
        """Return references in deterministic registration order."""

        return tuple(self._sources)

    def listing(self) -> tuple[SourceDescriptor, ...]:
        """Return source metadata suitable for diagnostics and evidence."""

        return tuple(
            SourceDescriptor(source_reference=reference, source_id=_source_metadata(source)[1])
            for reference, source in self._sources.items()
        )

    def iter_events(self, source_references: Iterable[str] | None = None) -> Iterator[object]:
        """Yield all selected sources through one transaction boundary."""

        references = (
            tuple(self._sources)
            if source_references is None
            else tuple(
                _text(reference, "source_reference", _MAX_SOURCE_REFERENCE_LENGTH)
                for reference in source_references
            )
        )
        selected = tuple(self.get(reference) for reference in references)
        for source in selected:
            reference, _, iterator = _source_metadata(source)
            try:
                stream = iterator()
                if not isinstance(stream, Iterable):
                    raise TypeError("iter_events() must return an iterable.")
                yield from stream
            except SignalSourceError as error:
                raise SignalSourceError(f"Signal source {reference!r} failed: {error}.") from error
            except Exception as error:
                raise SignalSourceError(f"Signal source {reference!r} failed: {error}.") from error

    def __iter__(self) -> Iterator[object]:
        """Allow all registered sources to be consumed as one stream."""

        return self.iter_events()

    events = iter_events

    def ingest(
        self,
        ingest_callable: Callable[..., _ResultT],
        *args: object,
        source_references: Iterable[str] | None = None,
        **kwargs: object,
    ) -> _ResultT:
        """Run an existing ingestion callable over the registered sources."""

        return ingest_callable(
            *args,
            self.iter_events(source_references),
            **kwargs,
        )


_ResultT = TypeVar("_ResultT")


def _source_metadata(source: SignalSource) -> tuple[str, UUID, Callable[[], Iterable[object]]]:
    reference_value: object = getattr(source, "source_reference", None)
    if reference_value is None:
        reference_value = getattr(source, "name", None)
    source_id_value: object = getattr(source, "source_id", None)
    reference, derived_id = source_identity(reference_value, source_id_value)
    iterator = getattr(source, "iter_events", None)
    if iterator is None:
        iterator = getattr(source, "events", None)
    if not callable(iterator):
        raise SignalSourceConfigurationError(
            f"Signal source {reference!r} must expose iter_events().", field="source"
        )
    source_id = source_id_value if source_id_value is not None else derived_id
    if not isinstance(source_id, UUID):
        _, source_id = source_identity(reference, source_id)
    return reference, source_id, cast(Callable[[], Iterable[object]], iterator)


def _mapping_column(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalSourceConfigurationError(
            f"{field_name} must name a non-empty source column.", field=field_name
        )
    if len(value) > _MAX_MAPPING_FIELD_LENGTH:
        raise SignalSourceConfigurationError(
            f"{field_name} exceeds {_MAX_MAPPING_FIELD_LENGTH} characters.", field=field_name
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SignalSourceConfigurationError(
            f"{field_name} cannot contain control characters.", field=field_name
        )
    return value


def _claim_column(used_columns: dict[str, str], column: str, field_name: str) -> None:
    previous = used_columns.get(column)
    if previous is not None:
        raise SignalSourceConfigurationError(
            f"Source column {column!r} is mapped more than once ({previous}, {field_name}).",
            field="mapping",
        )
    used_columns[column] = field_name


def _text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalSourceConfigurationError(
            f"{field_name} must be a non-empty string.", field=field_name
        )
    if len(value) > maximum:
        raise SignalSourceConfigurationError(
            f"{field_name} exceeds {maximum} characters.", field=field_name
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SignalSourceConfigurationError(
            f"{field_name} cannot contain control characters.", field=field_name
        )
    return value


def _coerce_field_value(field_name: str, value: object) -> object:
    if field_name in {"is_late"}:
        return _coerce_bool(value)
    return value


def _coerce_payload_value(field_name: str, value: object) -> object:
    if field_name == "is_adverse":
        return _coerce_bool(value)
    if field_name == "days_past_due" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def _coerce_bool(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalised = value.strip().lower()
    if normalised in {"true", "1"}:
        return True
    if normalised in {"false", "0"}:
        return False
    return value


__all__ = [
    "SignalInput",
    "SignalSource",
    "SignalSourceConfigurationError",
    "SignalSourceError",
    "SignalSourceRegistry",
    "SourceDescriptor",
    "map_signal_row",
    "source_identity",
    "validate_mapping",
    "validate_source_row",
]
