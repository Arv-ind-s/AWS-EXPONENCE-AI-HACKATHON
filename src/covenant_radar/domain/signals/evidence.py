"""Evidence-item derivation for the signal ledger (``T-046``).

The signal ingestion layer stores immutable events.  This module turns those
events into the stable evidence identity consumed by the later persistence,
materiality, decay and supersession stages.  It deliberately contains no
database or framework imports: a scoring run can therefore be replayed from
facts alone.

An evidence identity is the tuple ``(borrower_id, facility_id, family,
evidence_type)``.  Magnitude is intentionally not part of that identity.  A
payment delay that changes from 5 to 15 days is one evolving item, not two
items.  Event counts are counts of distinct event dates, so multiple source
events on one date cannot inflate persistence.

The caller supplies the complete event history relevant to the scoring date.
That is important when an existing item is being extended: the persisted item
stores source ids, but not every historical event date, so a rolling count
cannot be reconstructed exactly from the item alone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Final, cast
from uuid import UUID

EVIDENCE_STATES: Final[frozenset[str]] = frozenset(
    {"transient", "sustained", "superseded", "disputed"}
)
TRANSIENT_STATE: Final[str] = "transient"
CERTIFICATE_OVERDUE_TYPE: Final[str] = "certificate_overdue"
_NEW_ITEM_RULE: Final[str] = "evidence.derivation.new_item.v1"
_MAX_TEXT_LENGTH: Final[int] = 100
_MAX_EVENT_IDS: Final[int] = 100_000


class _Missing:
    pass


_MISSING = _Missing()


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The threshold portion needed by base evidence derivation.

    The application normally passes the validated threshold-store mapping to
    :func:`score_evidence`.  This small value object is useful at pure-domain
    call sites and intentionally has no policy defaults: omitted values mean
    that the caller is asking only for identity and full-history derivation.
    Persistence thresholds (T3), materiality (T4) and decay are applied by
    their own later stages.
    """

    event_window_days: int | None = None

    def __post_init__(self) -> None:
        if self.event_window_days is not None:
            _positive_integer(self.event_window_days, "event_window_days")


@dataclass(frozen=True, slots=True)
class SignalEventFacts:
    """Persistence-neutral fields read from one signal event.

    ``event_id`` is the database id when available.  ``content_hash`` is a
    safe source-independent fallback for events that have not been persisted
    yet.  ``id`` is accepted as a compatibility spelling because ORM rows and
    several ingestion adapters expose that name.
    """

    borrower_id: UUID
    facility_id: UUID | None
    event_date: date
    family: str
    event_type: str | None = None
    magnitude: Decimal | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    event_id: UUID | str | None = None
    content_hash: str | None = None
    evidence_type: str | None = None
    id: UUID | str | None = None

    def __post_init__(self) -> None:
        borrower_id = _uuid(self.borrower_id, "borrower_id")
        facility_id = _uuid(self.facility_id, "facility_id", nullable=True)
        event_date = _calendar_date(self.event_date, "event_date")
        family = _bounded_text(self.family, "family", max_length=20)

        event_type = _optional_bounded_text(self.event_type, "event_type")
        evidence_type = _optional_bounded_text(self.evidence_type, "evidence_type")
        if event_type is None and evidence_type is None:
            raise ValueError("An event requires event_type or evidence_type.")
        if event_type is None:
            event_type = evidence_type
        if evidence_type is None:
            evidence_type = event_type
        assert event_type is not None
        assert evidence_type is not None

        event_id = _event_identifier(self.event_id, "event_id")
        row_id = _event_identifier(self.id, "id")
        if event_id is not None and row_id is not None and event_id != row_id:
            raise ValueError("event_id and id must identify the same event.")
        normalized_id = event_id or row_id

        if self.magnitude is not None:
            _decimal(self.magnitude, "magnitude")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping.")
        if self.content_hash is not None:
            _bounded_text(self.content_hash, "content_hash", max_length=128)

        object.__setattr__(self, "borrower_id", borrower_id)
        object.__setattr__(self, "facility_id", facility_id)
        object.__setattr__(self, "event_date", event_date)
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "evidence_type", evidence_type)
        object.__setattr__(self, "event_id", normalized_id)
        object.__setattr__(self, "id", normalized_id)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def from_event(cls, event: object) -> SignalEventFacts:
        """Convert a domain event, ORM row, or source-neutral mapping."""

        if isinstance(event, cls):
            return event
        return cls(
            borrower_id=cast(UUID, _read(event, "borrower_id")),
            facility_id=cast(UUID | None, _read(event, "facility_id", None)),
            event_date=cast(date, _read(event, "event_date")),
            family=cast(str, _read(event, "family")),
            event_type=cast(str | None, _read(event, "event_type", None)),
            magnitude=cast(Decimal | None, _read(event, "magnitude", None)),
            payload=cast(Mapping[str, object], _read(event, "payload", {})),
            event_id=cast(
                UUID | str | None,
                _read(event, "event_id", _read(event, "id", None)),
            ),
            content_hash=cast(str | None, _read(event, "content_hash", None)),
            evidence_type=cast(str | None, _read(event, "evidence_type", None)),
        )

    @property
    def identity(self) -> tuple[UUID, UUID | None, str, str]:
        """Return the stable evidence identity for this event."""

        assert self.evidence_type is not None
        return (self.borrower_id, self.facility_id, self.family, self.evidence_type)


@dataclass(frozen=True, slots=True)
class EvidenceFacts:
    """Persistence-neutral fields of an already-derived evidence item."""

    borrower_id: UUID
    facility_id: UUID | None
    family: str
    evidence_type: str
    first_seen: date
    last_seen: date
    persistence_days: int | None = None
    event_count_window: int | None = None
    materiality_pct: Decimal | None = None
    decay_factor: Decimal | None = None
    state: str = TRANSIENT_STATE
    counts_toward_pressure: bool = False
    superseded_by_id: UUID | None = None
    supersedes_id: UUID | None = None
    source_event_ids: tuple[str, ...] = ()
    id: UUID | None = None

    def __post_init__(self) -> None:
        borrower_id = _uuid(self.borrower_id, "borrower_id")
        facility_id = _uuid(self.facility_id, "facility_id", nullable=True)
        family = _bounded_text(self.family, "family", max_length=20)
        evidence_type = _bounded_text(self.evidence_type, "evidence_type", max_length=50)
        first_seen = _calendar_date(self.first_seen, "first_seen")
        last_seen = _calendar_date(self.last_seen, "last_seen")
        if last_seen < first_seen:
            raise ValueError("last_seen must not precede first_seen.")
        if self.state not in EVIDENCE_STATES:
            raise ValueError(f"Unknown evidence state {self.state!r}.")
        if not isinstance(self.counts_toward_pressure, bool):
            raise TypeError("counts_toward_pressure must be a boolean.")
        for count_value, name in (
            (self.persistence_days, "persistence_days"),
            (self.event_count_window, "event_count_window"),
        ):
            if count_value is not None:
                _non_negative_integer(count_value, name)
        for decimal_value, name in (
            (self.materiality_pct, "materiality_pct"),
            (self.decay_factor, "decay_factor"),
        ):
            if decimal_value is not None:
                _decimal(decimal_value, name)
        decay_factor = self.decay_factor
        if decay_factor is not None and not Decimal("0") <= decay_factor <= Decimal("1"):
            raise ValueError("decay_factor must be between 0 and 1.")
        item_id = _uuid(self.id, "id", nullable=True)
        superseded_by_id = _uuid(self.superseded_by_id, "superseded_by_id", nullable=True)
        supersedes_id = _uuid(self.supersedes_id, "supersedes_id", nullable=True)
        if item_id is not None and item_id in {superseded_by_id, supersedes_id}:
            raise ValueError("An evidence item cannot link to itself.")
        source_event_ids = _source_ids(self.source_event_ids)

        object.__setattr__(self, "borrower_id", borrower_id)
        object.__setattr__(self, "facility_id", facility_id)
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "evidence_type", evidence_type)
        object.__setattr__(self, "first_seen", first_seen)
        object.__setattr__(self, "last_seen", last_seen)
        object.__setattr__(self, "id", item_id)
        object.__setattr__(self, "superseded_by_id", superseded_by_id)
        object.__setattr__(self, "supersedes_id", supersedes_id)
        object.__setattr__(self, "source_event_ids", source_event_ids)

    @classmethod
    def from_item(cls, item: object) -> EvidenceFacts:
        """Convert an ORM row or mapping without importing its adapter type."""

        raw_source_event_ids = cast(Sequence[str], _read(item, "source_event_ids", ()))
        return cls(
            borrower_id=cast(UUID, _read(item, "borrower_id")),
            facility_id=cast(UUID | None, _read(item, "facility_id", None)),
            family=cast(str, _read(item, "family")),
            evidence_type=cast(str, _read(item, "evidence_type")),
            first_seen=cast(date, _read(item, "first_seen")),
            last_seen=cast(date, _read(item, "last_seen")),
            persistence_days=cast(int | None, _read(item, "persistence_days", None)),
            event_count_window=cast(int | None, _read(item, "event_count_window", None)),
            materiality_pct=cast(Decimal | None, _read(item, "materiality_pct", None)),
            decay_factor=cast(Decimal | None, _read(item, "decay_factor", None)),
            state=cast(str, _read(item, "state", TRANSIENT_STATE)),
            counts_toward_pressure=cast(bool, _read(item, "counts_toward_pressure", False)),
            superseded_by_id=cast(UUID | None, _read(item, "superseded_by_id", None)),
            supersedes_id=cast(UUID | None, _read(item, "supersedes_id", None)),
            source_event_ids=tuple(raw_source_event_ids),
            id=cast(UUID | None, _read(item, "id", None)),
        )

    @property
    def identity(self) -> tuple[UUID, UUID | None, str, str]:
        return (self.borrower_id, self.facility_id, self.family, self.evidence_type)


@dataclass(frozen=True, slots=True)
class EvidenceTransitionFacts:
    """One append-only evidence state transition."""

    from_state: str | None
    to_state: str
    occurred_on: date
    rule: str
    evidence_id: UUID | None = None
    threshold_snapshot_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.from_state is not None and self.from_state not in EVIDENCE_STATES:
            raise ValueError(f"Unknown previous evidence state {self.from_state!r}.")
        if self.to_state not in EVIDENCE_STATES:
            raise ValueError(f"Unknown next evidence state {self.to_state!r}.")
        occurred_on = _calendar_date(self.occurred_on, "occurred_on")
        rule = _bounded_text(self.rule, "rule", max_length=100)
        evidence_id = _uuid(self.evidence_id, "evidence_id", nullable=True)
        threshold_snapshot_id = _uuid(
            self.threshold_snapshot_id, "threshold_snapshot_id", nullable=True
        )
        object.__setattr__(self, "occurred_on", occurred_on)
        object.__setattr__(self, "rule", rule)
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "threshold_snapshot_id", threshold_snapshot_id)


@dataclass(frozen=True, slots=True)
class EvidenceScore(EvidenceFacts):
    """Derived evidence fields plus an optional transition to persist."""

    transition: EvidenceTransitionFacts | None = None

    def __post_init__(self) -> None:
        EvidenceFacts.__post_init__(self)
        if self.transition is not None and self.transition.to_state != self.state:
            raise ValueError("A score transition must end at the score state.")
        if self.transition is not None and self.transition.evidence_id not in {None, self.id}:
            raise ValueError("A score transition must reference the scored evidence item.")


def evidence_identity(
    borrower_id: UUID,
    facility_id: UUID | None,
    family: str,
    evidence_type: str,
) -> tuple[UUID, UUID | None, str, str]:
    """Build and validate the key used to group evidence items."""

    return SignalEventFacts(
        borrower_id=borrower_id,
        facility_id=facility_id,
        event_date=date.min,
        family=family,
        evidence_type=evidence_type,
    ).identity


def derive_evidence(
    events: Iterable[SignalEventFacts | Mapping[str, object] | object],
    existing: Iterable[EvidenceFacts | Mapping[str, object] | object] = (),
    *,
    as_of: date | None = None,
    event_window_days: int | None = None,
) -> list[EvidenceScore]:
    """Derive stable evidence items from a complete event history.

    Events dated after ``as_of`` are ignored because future information must
    never enter a historical scoring run.  Existing items are always carried
    through, including those with no event in the current input; later decay
    logic changes their contribution, not their visibility or existence.
    """

    event_facts = [SignalEventFacts.from_event(event) for event in events]
    existing_facts = [EvidenceFacts.from_item(item) for item in existing]
    if not event_facts and not existing_facts:
        return []

    scoring_date = _scoring_date(as_of, event_facts, existing_facts)
    if event_window_days is not None:
        _positive_integer(event_window_days, "event_window_days")

    existing_by_identity: dict[tuple[UUID, UUID | None, str, str], EvidenceFacts] = {}
    for item in existing_facts:
        if item.identity in existing_by_identity:
            raise ValueError(f"Duplicate existing evidence identity {item.identity!r}.")
        existing_by_identity[item.identity] = item

    grouped: dict[tuple[UUID, UUID | None, str, str], list[SignalEventFacts]] = {}
    for event in event_facts:
        if event.event_date > scoring_date:
            continue
        grouped.setdefault(event.identity, []).append(event)

    identities = set(existing_by_identity).union(grouped)
    results: list[EvidenceScore] = []
    for identity in sorted(identities, key=_identity_sort_key):
        prior = existing_by_identity.get(identity)
        current_events = _deduplicate_events(grouped.get(identity, ()))
        result = _score_identity(
            identity,
            current_events,
            prior,
            scoring_date,
            event_window_days,
        )
        if result is not None:
            results.append(result)
    return results


def score_evidence(
    events: Sequence[SignalEventFacts | Mapping[str, object] | object],
    existing: Sequence[EvidenceFacts | Mapping[str, object] | object],
    as_of: date,
    thresholds: Thresholds | Mapping[str, object] | object,
) -> list[EvidenceScore]:
    """Contract ``C-34`` entry point for evidence derivation.

    This stage records the identity, observation dates and rolling event
    count.  T3 persistence decisions, T4 materiality and geometric decay are
    deliberately separate stages, but their result shape is already carried
    by :class:`EvidenceScore` so those stages can update it without changing
    the ledger contract.
    """

    _calendar_date(as_of, "as_of")
    window = _configured_event_window_days(thresholds)
    return derive_evidence(
        events,
        existing,
        as_of=as_of,
        event_window_days=window,
    )


def to_signal_event_facts(value: object) -> SignalEventFacts:
    """Public adapter-neutral conversion helper."""

    return SignalEventFacts.from_event(value)


def to_evidence_facts(value: object) -> EvidenceFacts:
    """Public adapter-neutral conversion helper."""

    return EvidenceFacts.from_item(value)


def _score_identity(
    identity: tuple[UUID, UUID | None, str, str],
    events: Sequence[SignalEventFacts],
    prior: EvidenceFacts | None,
    as_of: date,
    event_window_days: int | None,
) -> EvidenceScore | None:
    if prior is None and not events:
        return None

    event_dates = {event.event_date for event in events}
    observed_dates = set(event_dates)
    if prior is not None:
        observed_dates.update((prior.first_seen, prior.last_seen))
    first_seen = min(observed_dates, default=None)
    last_seen = max(observed_dates, default=None)
    if first_seen is None or last_seen is None:  # pragma: no cover - guarded above
        return None

    window_dates = (
        {
            event.event_date
            for event in events
            if _inside_event_window(event.event_date, as_of, event_window_days)
        }
        if events
        else set()
    )
    source_ids = _merged_source_ids(
        prior.source_event_ids if prior is not None else (),
        tuple(_event_source_id(event) for event in events),
    )
    persistence_days = (
        _longest_consecutive_run(event_dates)
        if event_dates
        else (prior.persistence_days if prior is not None else None)
    )
    event_count_window = (
        len(window_dates) if events else (prior.event_count_window if prior is not None else 0)
    )
    state = prior.state if prior is not None else TRANSIENT_STATE
    counts_toward_pressure = prior.counts_toward_pressure if prior is not None else False
    transition = None
    item_id = prior.id if prior is not None else None
    if prior is None:
        transition = EvidenceTransitionFacts(
            from_state=None,
            to_state=TRANSIENT_STATE,
            occurred_on=first_seen,
            rule=_NEW_ITEM_RULE,
        )

    return EvidenceScore(
        id=item_id,
        borrower_id=identity[0],
        facility_id=identity[1],
        family=identity[2],
        evidence_type=identity[3],
        first_seen=first_seen,
        last_seen=last_seen,
        persistence_days=persistence_days,
        event_count_window=event_count_window,
        materiality_pct=prior.materiality_pct if prior is not None else None,
        decay_factor=prior.decay_factor if prior is not None else Decimal("1"),
        state=state,
        counts_toward_pressure=counts_toward_pressure,
        superseded_by_id=prior.superseded_by_id if prior is not None else None,
        supersedes_id=prior.supersedes_id if prior is not None else None,
        source_event_ids=source_ids,
        transition=transition,
    )


def _deduplicate_events(events: Sequence[SignalEventFacts]) -> tuple[SignalEventFacts, ...]:
    seen: dict[str, SignalEventFacts] = {}
    for event in events:
        key = _event_source_id(event)
        previous = seen.get(key)
        if previous is not None and previous != event:
            raise ValueError(f"Event identifier {key!r} describes conflicting events.")
        seen[key] = event
    return tuple(
        sorted(seen.values(), key=lambda event: (event.event_date, _event_source_id(event)))
    )


def _event_source_id(event: SignalEventFacts) -> str:
    if event.event_id is not None:
        return str(event.event_id)
    if event.content_hash is not None:
        return event.content_hash
    value = {
        "borrower_id": str(event.borrower_id),
        "facility_id": str(event.facility_id) if event.facility_id else None,
        "event_date": event.event_date.isoformat(),
        "family": event.family,
        "event_type": event.event_type,
        "evidence_type": event.evidence_type,
        "magnitude": str(event.magnitude) if event.magnitude is not None else None,
        "payload": _json_safe(event.payload),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"derived:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _merged_source_ids(*groups: Sequence[str]) -> tuple[str, ...]:
    values: set[str] = set()
    for group in groups:
        values.update(group)
    if len(values) > _MAX_EVENT_IDS:
        raise ValueError(f"An evidence item cannot reference more than {_MAX_EVENT_IDS} events.")
    return tuple(sorted(values))


def _source_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("source_event_ids must be a sequence of identifiers.")
    normalized: list[str] = []
    for value in values:
        normalized.append(_bounded_text(value, "source_event_id", max_length=128))
    return _merged_source_ids(normalized)


def _inside_event_window(event_date: date, as_of: date, window_days: int | None) -> bool:
    if event_date > as_of:
        return False
    if window_days is None:
        return True
    return event_date >= as_of - timedelta(days=window_days - 1)


def _longest_consecutive_run(event_dates: set[date]) -> int:
    if not event_dates:
        return 0
    longest = 0
    for candidate in sorted(event_dates):
        if candidate - timedelta(days=1) in event_dates:
            continue
        run = 1
        while candidate + timedelta(days=run) in event_dates:
            run += 1
        longest = max(longest, run)
    return longest


def _scoring_date(
    as_of: date | None,
    events: Sequence[SignalEventFacts],
    existing: Sequence[EvidenceFacts],
) -> date:
    if as_of is not None:
        return _calendar_date(as_of, "as_of")
    candidates = [event.event_date for event in events]
    candidates.extend(item.last_seen for item in existing)
    if not candidates:
        raise ValueError("as_of is required when there are no events or existing items.")
    return max(candidates)


def _configured_event_window_days(thresholds: object) -> int | None:
    if thresholds is None:
        return None
    if isinstance(thresholds, Thresholds):
        return thresholds.event_window_days

    candidate: object = thresholds
    if isinstance(thresholds, Mapping):
        candidate = thresholds.get("T3", thresholds)
    else:
        for name in ("T3", "t3", "persistence"):
            value = getattr(thresholds, name, None)
            if value is not None:
                candidate = value
                break
    if isinstance(candidate, Mapping):
        value = candidate.get("event_window_days")
    else:
        value = getattr(candidate, "event_window_days", None)
    if value is None:
        return None
    _positive_integer(value, "T3.event_window_days")
    return cast(int, value)


def _read(value: object, name: str, default: object = _MISSING) -> object:
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
    else:
        marker = object()
        result = getattr(value, name, marker)
        if result is not marker:
            return result
    if default is not _MISSING:
        return default
    raise ValueError(f"Input is missing required field {name!r}.")


def _identity_sort_key(identity: tuple[UUID, UUID | None, str, str]) -> tuple[str, str, str, str]:
    return (
        str(identity[0]),
        str(identity[1]) if identity[1] is not None else "",
        identity[2],
        identity[3],
    )


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID | date):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, str | bool | int | float):
        return value
    return str(value)


def _uuid(value: object, field_name: str, *, nullable: bool = False) -> UUID | None:
    if value is None and nullable:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be a UUID.") from error
    raise TypeError(f"{field_name} must be a UUID.")


def _optional_uuid(value: object, field_name: str) -> None:
    if value is not None:
        _uuid(value, field_name)


def _event_identifier(value: object, field_name: str) -> UUID | str | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return _bounded_text(value, field_name, max_length=128)
    raise TypeError(f"{field_name} must be a UUID, text, or None.")


def _calendar_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a calendar date, not a datetime.")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO calendar date.") from error
    raise TypeError(f"{field_name} must be a calendar date.")


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool | float):
        raise TypeError(f"{field_name} must be an exact Decimal value.")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | str):
        try:
            result = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"{field_name} must be a valid Decimal value.") from error
    else:
        raise TypeError(f"{field_name} must be a Decimal value.")
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    return result


def _positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _non_negative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _bounded_text(value: object, field_name: str, *, max_length: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains a control character.")
    return normalized


def _optional_bounded_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name)


__all__ = [
    "CERTIFICATE_OVERDUE_TYPE",
    "EVIDENCE_STATES",
    "EvidenceFacts",
    "EvidenceScore",
    "EvidenceTransitionFacts",
    "SignalEventFacts",
    "TRANSIENT_STATE",
    "Thresholds",
    "derive_evidence",
    "evidence_identity",
    "score_evidence",
    "to_evidence_facts",
    "to_signal_event_facts",
]
