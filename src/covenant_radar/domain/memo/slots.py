"""Immutable, provenance-carrying values for the memo template.

No value in this module is calculated.  ``MemoRecord`` is the explicit
boundary at which an already-persisted record (or a record-shaped read model)
enters memo assembly.  ``MemoSlotMap`` retains the source reference for every
slot, including every record contributing to a collection slot.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Final
from uuid import UUID

from covenant_radar.domain.memo.template import DEFAULT_MEMO_TEMPLATE

ABSENT_VALUE_TEXT: Final[str] = "Not available from the recorded evidence."
NO_SIMULATIONS_VALUE_TEXT: Final[str] = "No simulations are recorded for this borrower."
SUPPRESSED_PROBABILITY_PREFIX: Final[str] = "Not shown: the forecast probability is suppressed."

_MAX_REASON_LENGTH = 500
_MAX_RECORD_TYPE_LENGTH = 50
_MAX_RECORD_ID_LENGTH = 200
_MISSING: Final[object] = object()


class SlotState(StrEnum):
    """The display state of a memo slot."""

    PRESENT = "present"
    ABSENT = "absent"
    SUPPRESSED = "suppressed"

    @classmethod
    def from_value(cls, value: SlotState | str) -> SlotState:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("Slot state must be text.")
        try:
            return cls(value.strip().lower())
        except ValueError as error:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"Slot state must be one of: {allowed}.") from error


@dataclass(frozen=True, slots=True)
class RecordReference:
    """The stable identity of a record that supplied a memo value."""

    record_type: str
    record_id: UUID | str

    def __post_init__(self) -> None:
        record_type = _bounded_text(self.record_type, "record_type", _MAX_RECORD_TYPE_LENGTH)
        record_id = self.record_id
        if isinstance(record_id, UUID):
            normalized_id: UUID | str = record_id
        else:
            normalized_id = _bounded_text(record_id, "record_id", _MAX_RECORD_ID_LENGTH)
        object.__setattr__(self, "record_type", record_type)
        object.__setattr__(self, "record_id", normalized_id)

    @property
    def type(self) -> str:
        """JSON-facing alias for the record type."""

        return self.record_type

    @property
    def id(self) -> UUID | str:
        """JSON-facing alias for the record identity."""

        return self.record_id

    def as_mapping(self) -> dict[str, object]:
        """Return a portable representation suitable for persistence."""

        return {
            "type": self.record_type,
            "id": str(self.record_id) if isinstance(self.record_id, UUID) else self.record_id,
        }


@dataclass(frozen=True, slots=True)
class MemoRecord:
    """A record reference and its already-computed, named values.

    Values are not interpreted or recalculated by the domain object.  The
    explicit mapping makes it impossible for a caller to provide a value
    without also naming the record that produced it.
    """

    reference: RecordReference
    values: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.reference, RecordReference):
            raise TypeError("MemoRecord.reference must be a RecordReference.")
        if not isinstance(self.values, Mapping):
            raise TypeError("MemoRecord.values must be a mapping.")
        normalized: dict[str, object] = {}
        for key, value in self.values.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("MemoRecord value names must be non-blank text.")
            normalized_key = key.strip()
            if normalized_key in normalized:
                raise ValueError(f"MemoRecord contains duplicate value {normalized_key!r}.")
            normalized[normalized_key] = _freeze(value)
        if not normalized:
            raise ValueError("MemoRecord must carry at least one named value.")
        object.__setattr__(self, "values", MappingProxyType(normalized))

    @classmethod
    def from_value(cls, value: MemoRecord | Mapping[str, object] | object) -> MemoRecord:
        """Normalise the explicit record DTO accepted at the service boundary."""

        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            raw_reference = value.get("reference", _MISSING)
            raw_values = value.get("values", _MISSING)
            if raw_reference is _MISSING:
                raw_reference = {
                    "record_type": value.get("record_type", _MISSING),
                    "record_id": value.get("record_id", _MISSING),
                }
            if raw_values is _MISSING:
                raise ValueError("MemoRecord requires a values mapping.")
        else:
            raw_reference = getattr(value, "reference", _MISSING)
            raw_values = getattr(value, "values", _MISSING)
            if raw_reference is _MISSING:
                raw_reference = {
                    "record_type": getattr(value, "record_type", _MISSING),
                    "record_id": getattr(value, "record_id", _MISSING),
                }
            if raw_values is _MISSING:
                raise ValueError("MemoRecord requires a values mapping.")
        return cls(
            reference=_reference_from_value(raw_reference),
            values=raw_values,  # type: ignore[arg-type]
        )

    def has(self, name: str) -> bool:
        """Return whether the source explicitly carries ``name``."""

        return name in self.values

    def value(self, *names: str) -> object:
        """Return one explicitly supplied value, rejecting conflicting aliases."""

        supplied = [(name, self.values[name]) for name in names if name in self.values]
        if not supplied:
            raise KeyError(names[0] if names else "value")
        first_name, first_value = supplied[0]
        for name, value in supplied[1:]:
            if value != first_value:
                raise ValueError(f"MemoRecord values {first_name!r} and {name!r} disagree.")
        return first_value

    def as_mapping(self) -> dict[str, object]:
        """Return the record in a stable persistence-friendly shape."""

        return {
            "reference": self.reference.as_mapping(),
            "values": {key: _json_safe(value) for key, value in self.values.items()},
        }


@dataclass(frozen=True, slots=True)
class MemoRecords:
    """All records needed to assemble one fixed memo slot map.

    Collection members are intentionally records rather than derived lists.
    Their references are retained in the resulting collection slots.
    """

    situation: MemoRecord | None = None
    covenant_position: MemoRecord | None = None
    drivers: tuple[MemoRecord, ...] = ()
    evidence: tuple[MemoRecord, ...] = ()
    simulations: tuple[MemoRecord, ...] = ()
    recommendations: tuple[MemoRecord, ...] = ()

    def __post_init__(self) -> None:
        for name in ("situation", "covenant_position"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, MemoRecord):
                raise TypeError(f"MemoRecords.{name} must be a MemoRecord or None.")
        for name in ("drivers", "evidence", "simulations", "recommendations"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, MemoRecord) for value in values):
                raise TypeError(f"MemoRecords.{name} must contain MemoRecord values.")
            references = tuple(value.reference for value in values)
            if len(references) != len(set(references)):
                raise ValueError(f"MemoRecords.{name} must not contain duplicate records.")
            object.__setattr__(self, name, values)

    @classmethod
    def from_value(cls, value: MemoRecords | Mapping[str, object] | object) -> MemoRecords:
        """Normalise a record bundle while rejecting unknown categories."""

        if isinstance(value, cls):
            return value
        allowed = {
            "situation",
            "covenant_position",
            "covenant",
            "drivers",
            "evidence",
            "simulations",
            "recommendations",
        }
        if isinstance(value, Mapping):
            keys = tuple(value)
            if any(not isinstance(key, str) for key in keys):
                raise ValueError("Memo record category names must be text.")
            unknown = sorted(set(keys).difference(allowed))
            if unknown:
                raise ValueError(f"Unknown memo record category {unknown[0]!r}.")
            read = value.get
        else:

            def read(name: str, default: object = None) -> object:
                return getattr(value, name, default)

        covenant = read("covenant_position", _MISSING)
        covenant_alias = read("covenant", _MISSING)
        if covenant is _MISSING:
            covenant = covenant_alias
        elif covenant_alias is not _MISSING and covenant_alias != covenant:
            raise ValueError("covenant_position and covenant must identify the same record.")

        return cls(
            situation=_optional_record(read("situation", None)),
            covenant_position=_optional_record(None if covenant is _MISSING else covenant),
            drivers=_record_tuple(read("drivers", ())),
            evidence=_record_tuple(read("evidence", ())),
            simulations=_record_tuple(read("simulations", ())),
            recommendations=_record_tuple(read("recommendations", ())),
        )


@dataclass(frozen=True, slots=True)
class MemoSlot:
    """One template slot with its value, state and source references."""

    name: str
    value: object
    record_references: tuple[RecordReference, ...] = ()
    state: SlotState = SlotState.PRESENT
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Memo slot name must be non-blank text.")
        name = self.name.strip()
        if name not in DEFAULT_MEMO_TEMPLATE.slot_names:
            raise ValueError(f"Memo slot {name!r} is not part of the fixed template.")
        references = tuple(self.record_references)
        if any(not isinstance(reference, RecordReference) for reference in references):
            raise TypeError("Memo slot record_references must contain RecordReference values.")
        if len(references) != len(set(references)):
            raise ValueError(f"Memo slot {name!r} contains duplicate record references.")
        state = SlotState.from_value(self.state)
        reason = (
            None
            if self.reason is None
            else _bounded_text(self.reason, "reason", _MAX_REASON_LENGTH)
        )
        if self.value is None:
            raise ValueError(f"Memo slot {name!r} must use explicit text instead of null.")
        if state is SlotState.PRESENT and reason is not None:
            raise ValueError(f"Present memo slot {name!r} cannot carry a reason.")
        if state is not SlotState.PRESENT:
            if reason is None:
                raise ValueError(f"Non-present memo slot {name!r} requires a reason.")
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError(f"Non-present memo slot {name!r} requires explanatory text.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "record_references", references)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "value", _freeze(self.value))

    @property
    def references(self) -> tuple[RecordReference, ...]:
        """Alias used by audit and UI consumers."""

        return self.record_references

    @property
    def source(self) -> tuple[RecordReference, ...]:
        """Compatibility-facing name for all contributing record references."""

        return self.record_references

    @property
    def source_reference(self) -> RecordReference | tuple[RecordReference, ...] | None:
        """Return the single source or all sources for collection slots."""

        return self.record_reference

    @property
    def record_ref(self) -> RecordReference | tuple[RecordReference, ...] | None:
        """Short alias for callers displaying provenance beside a value."""

        return self.record_reference

    @property
    def status(self) -> SlotState:
        """Status alias used by presentation consumers."""

        return self.state

    @property
    def absence_reason(self) -> str | None:
        """Return the explicit absence or suppression reason, if any."""

        return self.reason

    @property
    def record_reference(self) -> RecordReference | tuple[RecordReference, ...] | None:
        """Return the single source or all sources for collection slots."""

        if len(self.record_references) == 1:
            return self.record_references[0]
        if self.record_references:
            return self.record_references
        return None

    @property
    def resolved(self) -> bool:
        """Whether this slot has a value from at least one record."""

        return self.state is SlotState.PRESENT and bool(self.record_references)

    def as_mapping(self) -> dict[str, object]:
        """Return the slot with portable values and complete provenance."""

        return {
            "value": _json_safe(self.value),
            "state": self.state.value,
            "reason": self.reason,
            "record_references": [reference.as_mapping() for reference in self.record_references],
        }


@dataclass(frozen=True, slots=True)
class MemoSlotMap:
    """The complete fixed-template slot map."""

    slots: tuple[MemoSlot, ...]
    template_version: str = DEFAULT_MEMO_TEMPLATE.version

    def __post_init__(self) -> None:
        slots = tuple(self.slots)
        if any(not isinstance(slot, MemoSlot) for slot in slots):
            raise TypeError("MemoSlotMap.slots must contain MemoSlot values.")
        expected = DEFAULT_MEMO_TEMPLATE.slot_names
        actual = tuple(slot.name for slot in slots)
        if actual != expected:
            raise ValueError("MemoSlotMap must contain the fixed template slots in section order.")
        if self.template_version != DEFAULT_MEMO_TEMPLATE.version:
            raise ValueError("MemoSlotMap.template_version must identify the fixed template.")
        object.__setattr__(self, "slots", slots)

    def get(self, name: str) -> MemoSlot | None:
        """Return a slot by name, or ``None`` for an unknown name."""

        for slot in self.slots:
            if slot.name == name:
                return slot
        return None

    def __getitem__(self, name: str) -> MemoSlot:
        slot = self.get(name)
        if slot is None:
            raise KeyError(name)
        return slot

    def __iter__(self) -> Iterator[MemoSlot]:
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.slots)

    @property
    def slot_names(self) -> tuple[str, ...]:
        return tuple(slot.name for slot in self.slots)

    @property
    def all_resolved(self) -> bool:
        return all(slot.resolved or slot.state is not SlotState.PRESENT for slot in self.slots)

    def as_mapping(self) -> dict[str, object]:
        """Return a stable map for persistence and later prompt masking."""

        return {
            "template_version": self.template_version,
            "slots": {slot.name: slot.as_mapping() for slot in self.slots},
        }

    to_dict = as_mapping


def present_slot(name: str, value: object, references: Iterable[RecordReference]) -> MemoSlot:
    """Construct a present slot from one or more source records."""

    return MemoSlot(name=name, value=value, record_references=tuple(references))


def absent_slot(
    name: str,
    reason: str,
    *,
    references: Iterable[RecordReference] = (),
    value_text: str = ABSENT_VALUE_TEXT,
) -> MemoSlot:
    """Construct an explicit absence that can never be mistaken for zero."""

    return MemoSlot(
        name=name,
        value=value_text,
        record_references=tuple(references),
        state=SlotState.ABSENT,
        reason=reason,
    )


def suppressed_slot(
    name: str,
    reason: str,
    references: Iterable[RecordReference],
    *,
    value_text: str | None = None,
) -> MemoSlot:
    """Construct a suppressed slot while retaining its limiting record."""

    normalized_reason = _bounded_text(reason, "reason", _MAX_REASON_LENGTH)
    text = value_text or f"{SUPPRESSED_PROBABILITY_PREFIX} Limiting factor: {normalized_reason}."
    return MemoSlot(
        name=name,
        value=text,
        record_references=tuple(references),
        state=SlotState.SUPPRESSED,
        reason=normalized_reason,
    )


def _reference_from_value(value: object) -> RecordReference:
    if isinstance(value, RecordReference):
        return value
    if isinstance(value, Mapping):
        record_type = value.get("record_type", value.get("type", _MISSING))
        record_id = value.get("record_id", value.get("id", _MISSING))
    else:
        record_type = getattr(value, "record_type", getattr(value, "type", _MISSING))
        record_id = getattr(value, "record_id", getattr(value, "id", _MISSING))
    if record_type is _MISSING or record_id is _MISSING:
        raise ValueError("A record reference requires a type and id.")
    return RecordReference(record_type=record_type, record_id=record_id)


def _optional_record(value: object) -> MemoRecord | None:
    if value is None or value is _MISSING:
        return None
    return MemoRecord.from_value(value)


def _record_tuple(value: object) -> tuple[MemoRecord, ...]:
    if value is None:
        return ()
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Iterable):
        raise TypeError("Memo record categories must be iterable record values.")
    return tuple(MemoRecord.from_value(item) for item in value)


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains a control character.")
    return normalized


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("Memo slot contains a non-finite float.")
        return value
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(item) for item in value]
    raise TypeError(f"Memo slot contains unsupported value {type(value).__name__}.")


__all__ = [
    "ABSENT_VALUE_TEXT",
    "MemoRecord",
    "MemoRecords",
    "MemoSlot",
    "MemoSlotMap",
    "NO_SIMULATIONS_VALUE_TEXT",
    "RecordReference",
    "SlotState",
    "SUPPRESSED_PROBABILITY_PREFIX",
    "absent_slot",
    "present_slot",
    "suppressed_slot",
]
