"""Deterministic comparison of consecutive portfolio triage runs.

The queue is a snapshot, not a live calculation.  This module compares two
such snapshots without depending on persistence or presentation code.  It
deliberately returns a record for the union of both runs: a borrower that is
missing from the newer run is still a visible ``newly_unmonitored`` change.

Only the configured thresholds decide whether a probability movement is
reported and whether a driver is dominant.  No default policy is introduced
when the caller has not supplied those values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

if TYPE_CHECKING:
    from covenant_radar.domain.triage.urgency import TriageEntry


class ChangeType(str, Enum):
    """The finite set of changes a queue row can report."""

    FIRST_RUN = "first_run"
    NEWLY_MONITORED = "newly_monitored"
    NEWLY_UNMONITORED = "newly_unmonitored"
    NEW_TO_ACT = "new_to_act"
    BAND_WORSENED = "band_worsened"
    BAND_IMPROVED = "band_improved"
    PROBABILITY_INCREASED = "probability_increased"
    PROBABILITY_DECREASED = "probability_decreased"
    NEWLY_SUPPRESSED = "newly_suppressed"
    NEWLY_UNSUPPRESSED = "newly_unsuppressed"
    NO_CHANGE = "no_change"


# Both names are useful at call sites: ``ChangeType`` reads well in models,
# while ``ChangeKind`` reads well when describing the result of a comparison.
ChangeKind = ChangeType

FIRST_RUN: Final[ChangeType] = ChangeType.FIRST_RUN
NEWLY_MONITORED: Final[ChangeType] = ChangeType.NEWLY_MONITORED
NEWLY_UNMONITORED: Final[ChangeType] = ChangeType.NEWLY_UNMONITORED
NEW_TO_ACT: Final[ChangeType] = ChangeType.NEW_TO_ACT
BAND_WORSENED: Final[ChangeType] = ChangeType.BAND_WORSENED
BAND_IMPROVED: Final[ChangeType] = ChangeType.BAND_IMPROVED
PROBABILITY_INCREASED: Final[ChangeType] = ChangeType.PROBABILITY_INCREASED
PROBABILITY_DECREASED: Final[ChangeType] = ChangeType.PROBABILITY_DECREASED
NEWLY_SUPPRESSED: Final[ChangeType] = ChangeType.NEWLY_SUPPRESSED
NEWLY_UNSUPPRESSED: Final[ChangeType] = ChangeType.NEWLY_UNSUPPRESSED
NO_CHANGE: Final[ChangeType] = ChangeType.NO_CHANGE

_ACT_BAND: Final[str] = "act"
_AMBER_BAND: Final[str] = "amber"
_WATCH_BAND: Final[str] = "watch"
_BAND_RANK: Final[Mapping[str, int]] = MappingProxyType(
    {_WATCH_BAND: 0, _AMBER_BAND: 1, _ACT_BAND: 2}
)
_SUPPRESSED_STATE: Final[str] = "suppressed"
_MAX_SUMMARY_LENGTH: Final[int] = 2_000
_MAX_REFERENCE_LENGTH: Final[int] = 100
_MAX_DRIVER_NAME_LENGTH: Final[int] = 100
_MISSING: Final[object] = object()


@dataclass(frozen=True, slots=True)
class ChangeThresholds:
    """Policy thresholds used by the comparison.

    ``reporting_threshold`` is an absolute probability delta.  A movement is
    reportable at the threshold (``>=``).  ``dominant_driver_share`` is a
    strict threshold: a driver exactly on the boundary is not named, which
    prevents a boundary rounding artefact from presenting a driver as
    dominant.
    """

    reporting_threshold: Decimal
    dominant_driver_share: Decimal

    def __post_init__(self) -> None:
        reporting_threshold = _fraction(
            self.reporting_threshold, "reporting_threshold", allow_one=True
        )
        dominant_driver_share = _fraction(
            self.dominant_driver_share, "dominant_driver_share", allow_one=True
        )
        object.__setattr__(self, "reporting_threshold", reporting_threshold)
        object.__setattr__(self, "dominant_driver_share", dominant_driver_share)

    @classmethod
    def from_value(
        cls,
        value: ChangeThresholds | Mapping[str, object] | object | None = None,
        *,
        reporting_threshold: object | None = None,
        dominant_driver_share: object | None = None,
    ) -> ChangeThresholds:
        """Normalize a threshold object or mapping and fail closed if absent."""

        if isinstance(value, cls):
            configured_reporting: object = value.reporting_threshold
            configured_driver: object = value.dominant_driver_share
        else:
            configured_reporting = _configured_value(
                value,
                "reporting_threshold",
                "probability_delta_threshold",
                "probability_change_threshold",
                "probability_movement_threshold",
                "change_threshold",
            )
            configured_driver = _configured_value(
                value,
                "dominant_driver_share",
                "driver_dominance_threshold",
                "driver_share_threshold",
                "dominant_share",
            )
        if reporting_threshold is not None:
            configured_reporting = reporting_threshold
        if dominant_driver_share is not None:
            configured_driver = dominant_driver_share
        if configured_reporting is _MISSING:
            raise ValueError("A reporting threshold must be configured for what-changed.")
        if configured_driver is _MISSING:
            raise ValueError("A dominant-driver share threshold must be configured.")
        return cls(
            _decimal(configured_reporting, "reporting_threshold"),
            _decimal(configured_driver, "dominant_driver_share"),
        )


@dataclass(frozen=True, slots=True)
class WhatChanged:
    """One immutable, typed change for one borrower."""

    borrower_id: UUID
    kind: ChangeType
    summary: str
    previous_band: str | None
    current_band: str | None
    previous_probability: Decimal | None
    current_probability: Decimal | None
    probability_delta: Decimal | None
    reporting_threshold: Decimal | None
    dominant_driver: str | None = None
    dominant_driver_share: Decimal | None = None
    previous_state: str | None = None
    current_state: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        if not isinstance(self.kind, ChangeType):
            try:
                object.__setattr__(self, "kind", ChangeType(self.kind))
            except (TypeError, ValueError) as error:
                raise ValueError("kind must be a supported ChangeType.") from error
        _bounded_text(self.summary, "summary", _MAX_SUMMARY_LENGTH)
        previous_band = _normalise_band(self.previous_band, "previous_band")
        current_band = _normalise_band(self.current_band, "current_band")
        previous_probability = _optional_fraction(self.previous_probability, "previous_probability")
        current_probability = _optional_fraction(self.current_probability, "current_probability")
        object.__setattr__(self, "previous_band", previous_band)
        object.__setattr__(self, "current_band", current_band)
        object.__setattr__(self, "previous_probability", previous_probability)
        object.__setattr__(self, "current_probability", current_probability)
        if self.probability_delta is not None:
            object.__setattr__(
                self,
                "probability_delta",
                _decimal(self.probability_delta, "probability_delta"),
            )
        if self.reporting_threshold is not None:
            object.__setattr__(
                self,
                "reporting_threshold",
                _fraction(self.reporting_threshold, "reporting_threshold", allow_one=True),
            )
        if self.dominant_driver is not None:
            object.__setattr__(
                self,
                "dominant_driver",
                _bounded_text(self.dominant_driver, "dominant_driver", _MAX_DRIVER_NAME_LENGTH),
            )
        if self.dominant_driver_share is not None:
            object.__setattr__(
                self,
                "dominant_driver_share",
                _decimal(self.dominant_driver_share, "dominant_driver_share"),
            )
        if self.previous_state is not None:
            object.__setattr__(
                self,
                "previous_state",
                _bounded_text(self.previous_state, "previous_state", 50).lower(),
            )
        if self.current_state is not None:
            object.__setattr__(
                self,
                "current_state",
                _bounded_text(self.current_state, "current_state", 50).lower(),
            )

    @property
    def change_type(self) -> ChangeType:
        """Compatibility spelling for consumers that call it a type."""

        return self.kind

    @property
    def type(self) -> ChangeType:
        return self.kind

    @property
    def code(self) -> str:
        return self.kind.value

    @property
    def text(self) -> str:
        return self.summary

    @property
    def is_disappearance(self) -> bool:
        return self.kind is ChangeType.NEWLY_UNMONITORED


@dataclass(frozen=True, slots=True)
class ChangeComparison:
    """The complete comparison, including borrowers missing from the new run."""

    changes: Mapping[UUID, WhatChanged]
    current: tuple[WhatChanged, ...]
    disappeared: tuple[WhatChanged, ...]
    first_run: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", MappingProxyType(dict(self.changes)))

    @property
    def newly_unmonitored(self) -> tuple[WhatChanged, ...]:
        return self.disappeared

    def __getitem__(self, borrower_id: UUID) -> WhatChanged:
        return self.changes[borrower_id]

    def __len__(self) -> int:
        return len(self.changes)


@dataclass(frozen=True, slots=True)
class _EntryFacts:
    borrower_id: UUID
    reference: str
    band: str | None
    probability: Decimal | None
    state: str
    suppressed: bool
    drivers: Mapping[str, Decimal]


def compute_what_changed(
    current_entries: Iterable[object],
    previous_entries: Iterable[object] | None = None,
    thresholds: ChangeThresholds | Mapping[str, object] | object | None = None,
    *,
    reporting_threshold: object | None = None,
    dominant_driver_share: object | None = None,
) -> dict[UUID, WhatChanged]:
    """Compare two runs and return changes for their borrower union.

    ``previous_entries=None`` means there was no prior completed run.  An
    empty iterable means there was a prior completed run containing no
    borrower, so current rows are correctly classified as newly monitored.
    """

    current = _normalise_entries(current_entries, "current_entries")
    prior_is_first_run = previous_entries is None
    previous: tuple[_EntryFacts, ...]
    if prior_is_first_run:
        previous = ()
    else:
        assert previous_entries is not None
        previous = _normalise_entries(previous_entries, "previous_entries")
    configured = _optional_thresholds(
        thresholds,
        reporting_threshold=reporting_threshold,
        dominant_driver_share=dominant_driver_share,
    )

    current_by_id: dict[UUID, _EntryFacts] = {entry.borrower_id: entry for entry in current}
    previous_by_id: dict[UUID, _EntryFacts] = {entry.borrower_id: entry for entry in previous}
    result: dict[UUID, WhatChanged] = {}
    for entry in current:
        prior = previous_by_id.get(entry.borrower_id)
        if prior is None:
            kind = FIRST_RUN if prior_is_first_run else NEWLY_MONITORED
            result[entry.borrower_id] = _new_change(
                entry,
                kind=kind,
                thresholds=configured,
            )
        else:
            result[entry.borrower_id] = _compare_entry(entry, prior, configured)

    # Preserve a deterministic order for rows that are present only in the
    # older run.  The map still contains them, so callers cannot accidentally
    # drop a borrower by iterating only the current run.
    disappeared = sorted(
        (entry for entry in previous if entry.borrower_id not in current_by_id),
        key=_entry_sort_key,
    )
    for entry in disappeared:
        result[entry.borrower_id] = _new_change(
            entry,
            kind=NEWLY_UNMONITORED,
            thresholds=configured,
            current_state="unmonitored",
            summary="newly unmonitored; absent from the current completed run",
        )
    return result


def what_changed(
    current_entries: Iterable[object],
    previous_entries: Iterable[object] | None = None,
    thresholds: ChangeThresholds | Mapping[str, object] | object | None = None,
    *,
    reporting_threshold: object | None = None,
    dominant_driver_share: object | None = None,
) -> dict[UUID, WhatChanged]:
    """Compatibility spelling for :func:`compute_what_changed`."""

    return compute_what_changed(
        current_entries,
        previous_entries,
        thresholds,
        reporting_threshold=reporting_threshold,
        dominant_driver_share=dominant_driver_share,
    )


def compare_runs(
    current_entries: Iterable[object],
    previous_entries: Iterable[object] | None = None,
    thresholds: ChangeThresholds | Mapping[str, object] | object | None = None,
    *,
    reporting_threshold: object | None = None,
    dominant_driver_share: object | None = None,
) -> ChangeComparison:
    """Return an ordered comparison object for UI and persistence adapters."""

    current = _normalise_entries(current_entries, "current_entries")
    previous = (
        () if previous_entries is None else _normalise_entries(previous_entries, "previous_entries")
    )
    changes = compute_what_changed(
        current,
        None if previous_entries is None else previous,
        thresholds,
        reporting_threshold=reporting_threshold,
        dominant_driver_share=dominant_driver_share,
    )
    current_changes = tuple(changes[entry.borrower_id] for entry in current)
    current_ids = {entry.borrower_id for entry in current}
    disappeared = tuple(
        changes[entry.borrower_id]
        for entry in sorted(previous, key=_entry_sort_key)
        if entry.borrower_id not in current_ids
    )
    return ChangeComparison(
        changes=changes,
        current=current_changes,
        disappeared=disappeared,
        first_run=previous_entries is None,
    )


def apply_what_changed(
    current_entries: Sequence[TriageEntry],
    previous_entries: Iterable[object] | None = None,
    thresholds: ChangeThresholds | Mapping[str, object] | object | None = None,
    *,
    reporting_threshold: object | None = None,
    dominant_driver_share: object | None = None,
) -> list[TriageEntry]:
    """Return domain triage entries with their persisted summary populated."""

    # The import is intentionally local: urgency is allowed to consume this
    # module through the public package, while this module remains usable with
    # adapter-neutral mappings and does not create an import cycle at import
    # time.
    from dataclasses import replace

    from covenant_radar.domain.triage.urgency import TriageEntry

    if isinstance(current_entries, str | bytes | bytearray):
        raise TypeError("current_entries must be a sequence of TriageEntry values.")
    normalised_entries = tuple(current_entries)
    if any(not isinstance(entry, TriageEntry) for entry in normalised_entries):
        raise TypeError("apply_what_changed requires domain TriageEntry values.")
    changes = compute_what_changed(
        normalised_entries,
        previous_entries,
        thresholds,
        reporting_threshold=reporting_threshold,
        dominant_driver_share=dominant_driver_share,
    )
    return [
        replace(entry, what_changed=changes[entry.borrower_id].summary)
        for entry in normalised_entries
    ]


attach_what_changed = apply_what_changed


def _compare_entry(
    current: _EntryFacts,
    previous: _EntryFacts,
    thresholds: ChangeThresholds | None,
) -> WhatChanged:
    delta = _probability_delta(current.probability, previous.probability)
    if current.suppressed and not previous.suppressed:
        return _new_change(
            current,
            kind=NEWLY_SUPPRESSED,
            thresholds=thresholds,
            previous=previous,
            summary="newly suppressed; the current forecast is suppressed",
            probability_delta=delta,
        )
    if previous.suppressed and not current.suppressed:
        return _new_change(
            current,
            kind=NEWLY_UNSUPPRESSED,
            thresholds=thresholds,
            previous=previous,
            summary="newly unsuppressed; the current forecast is usable",
            probability_delta=delta,
        )
    if current.band == _ACT_BAND and previous.band != _ACT_BAND:
        return _new_change(
            current,
            kind=NEW_TO_ACT,
            thresholds=thresholds,
            previous=previous,
            summary=f"new to act; band worsened from {previous.band} to {_ACT_BAND}",
            probability_delta=delta,
        )
    current_band = current.band
    previous_band = previous.band
    if current_band != previous_band and current_band in _BAND_RANK and previous_band in _BAND_RANK:
        assert current_band is not None
        assert previous_band is not None
        if _BAND_RANK[current_band] > _BAND_RANK[previous_band]:
            kind = BAND_WORSENED
            summary = f"band worsened from {previous_band} to {current_band}"
        else:
            kind = BAND_IMPROVED
            summary = f"band improved from {previous_band} to {current_band}"
        return _new_change(
            current,
            kind=kind,
            thresholds=thresholds,
            previous=previous,
            summary=summary,
            probability_delta=delta,
        )

    if (
        delta is not None
        and thresholds is not None
        and abs(delta) >= thresholds.reporting_threshold
    ):
        if delta > 0:
            kind = PROBABILITY_INCREASED
            summary = (
                f"probability increased by {_decimal_text(delta)} "
                f"from {_decimal_text(previous.probability)} "
                f"to {_decimal_text(current.probability)}"
            )
        elif delta < 0:
            kind = PROBABILITY_DECREASED
            summary = (
                f"probability decreased by {_decimal_text(abs(delta))} "
                f"from {_decimal_text(previous.probability)} "
                f"to {_decimal_text(current.probability)}"
            )
        else:  # pragma: no cover - abs(0) cannot meet a positive threshold
            kind = NO_CHANGE
            summary = _no_change_summary(delta, thresholds)
        return _new_change(
            current,
            kind=kind,
            thresholds=thresholds,
            previous=previous,
            summary=summary,
            probability_delta=delta,
        )

    if thresholds is None:
        raise ValueError("A reporting threshold must be configured for probability comparison.")
    return _new_change(
        current,
        kind=NO_CHANGE,
        thresholds=thresholds,
        previous=previous,
        summary=_no_change_summary(delta, thresholds),
        probability_delta=delta,
    )


def _new_change(
    current: _EntryFacts,
    *,
    kind: ChangeType,
    thresholds: ChangeThresholds | None,
    previous: _EntryFacts | None = None,
    summary: str | None = None,
    current_state: str | None = None,
    probability_delta: Decimal | None = None,
) -> WhatChanged:
    dominant_driver, driver_share = _dominant_driver(current, thresholds)
    if dominant_driver is not None and kind is not NEWLY_UNMONITORED:
        assert driver_share is not None
        summary = (
            f"{summary or _default_summary(kind)}; dominant driver: {dominant_driver} "
            f"({_decimal_text(driver_share)})"
        )
    return WhatChanged(
        borrower_id=current.borrower_id,
        kind=kind,
        summary=summary or _default_summary(kind),
        previous_band=previous.band if previous is not None else None,
        current_band=current.band,
        previous_probability=previous.probability if previous is not None else None,
        current_probability=current.probability,
        probability_delta=probability_delta,
        reporting_threshold=thresholds.reporting_threshold if thresholds is not None else None,
        dominant_driver=dominant_driver if kind is not NEWLY_UNMONITORED else None,
        dominant_driver_share=driver_share if kind is not NEWLY_UNMONITORED else None,
        previous_state=previous.state if previous is not None else None,
        current_state=current_state or current.state,
    )


def _dominant_driver(
    entry: _EntryFacts,
    thresholds: ChangeThresholds | None,
) -> tuple[str | None, Decimal | None]:
    if not entry.drivers:
        return None, None
    if thresholds is None:
        raise ValueError("A dominant-driver share threshold must be configured.")
    name, share = max(entry.drivers.items(), key=lambda item: (item[1], _reverse_text(item[0])))
    if share <= thresholds.dominant_driver_share:
        return None, None
    return name, share


def _no_change_summary(delta: Decimal | None, thresholds: ChangeThresholds) -> str:
    if delta is None:
        movement = "no comparable probability"
    else:
        movement = f"probability movement {_decimal_text(abs(delta))}"
    return (
        f"no change; {movement} is below reporting threshold "
        f"{_decimal_text(thresholds.reporting_threshold)}"
    )


def _default_summary(kind: ChangeType) -> str:
    return {
        FIRST_RUN: "first run; no prior completed run to compare",
        NEWLY_MONITORED: "newly monitored; no entry in the prior completed run",
        NEWLY_UNMONITORED: "newly unmonitored; absent from the current completed run",
        NEW_TO_ACT: "new to act",
        BAND_WORSENED: "band worsened",
        BAND_IMPROVED: "band improved",
        PROBABILITY_INCREASED: "probability increased",
        PROBABILITY_DECREASED: "probability decreased",
        NEWLY_SUPPRESSED: "newly suppressed",
        NEWLY_UNSUPPRESSED: "newly unsuppressed",
        NO_CHANGE: "no change",
    }[kind]


def _normalise_entries(entries: Iterable[object], field_name: str) -> tuple[_EntryFacts, ...]:
    if isinstance(entries, str | bytes | bytearray):
        raise TypeError(f"{field_name} must be an iterable of triage entries, not text.")
    try:
        values = tuple(_entry_from_value(value) for value in entries)
    except TypeError as error:
        raise TypeError(f"{field_name} must be an iterable of triage entries.") from error
    seen: set[UUID] = set()
    for value in values:
        if value.borrower_id in seen:
            raise ValueError(f"Borrower {value.borrower_id} occurs more than once in {field_name}.")
        seen.add(value.borrower_id)
    return values


def _entry_from_value(value: object) -> _EntryFacts:
    borrower_id = _read_any(value, "borrower_id", default=None)
    if not isinstance(borrower_id, UUID):
        raise TypeError("triage entry borrower_id must be a UUID.")
    reference_value = _read_any(
        value,
        "reference",
        "borrower_reference",
        "borrower_ref",
        default=str(borrower_id),
    )
    reference = _bounded_text(reference_value, "reference", _MAX_REFERENCE_LENGTH)
    probability = _optional_fraction(_read_any(value, "probability", default=None), "probability")
    band_value = _read_any(value, "band", default=_WATCH_BAND)
    band = None if band_value is None else _bounded_text(band_value, "band", 20).lower()
    _optional_band(band, "band")
    state_value = _read_any(value, "state", "status", "forecast_state", default=None)
    explicit_suppressed = _read_any(value, "suppressed", "probability_suppressed", default=None)
    if explicit_suppressed is not None and not isinstance(explicit_suppressed, bool):
        raise TypeError("suppressed must be a boolean when supplied.")
    if state_value is not None:
        state = _bounded_text(state_value, "state", 50).lower()
    elif explicit_suppressed:
        state = _SUPPRESSED_STATE
    elif (
        probability is None
        and _read_any(value, "worst_covenant_version_id", default=None) is not None
    ):
        state = _SUPPRESSED_STATE
    elif probability is None:
        state = "no_forecast"
    else:
        state = "available"
    suppressed = bool(explicit_suppressed) or state == _SUPPRESSED_STATE
    drivers = _normalise_drivers(value)
    return _EntryFacts(
        borrower_id=borrower_id,
        reference=reference,
        band=band,
        probability=probability,
        state=state,
        suppressed=suppressed,
        drivers=drivers,
    )


def _normalise_drivers(value: object) -> Mapping[str, Decimal]:
    raw = _read_any(
        value,
        "drivers",
        "driver_shares",
        "forecast_drivers",
        default=_MISSING,
    )
    if raw is _MISSING:
        why = _read_any(value, "why", "explanation", default=None)
        raw = _read_any(
            why,
            "drivers",
            "driver_shares",
            "forecast_drivers",
            "attribution",
            default=_MISSING,
        )
    if raw is _MISSING or raw is None:
        return MappingProxyType({})
    if isinstance(raw, Mapping):
        nested = raw.get("drivers", _MISSING)
        if nested is not _MISSING:
            raw = nested
        elif set(raw).issubset({"name", "share"}) and "name" in raw:
            raw = (raw,)
        else:
            return MappingProxyType(_driver_pairs(raw.items()))
    if isinstance(raw, str | bytes | bytearray):
        raise TypeError("drivers must be a mapping or iterable of named shares.")
    try:
        raw_iterable = cast(Iterable[object], raw)
        pairs = (_driver_parts(item) for item in raw_iterable)
        return MappingProxyType(_driver_pairs(pairs))
    except TypeError as error:
        raise TypeError("drivers must be a mapping or iterable of named shares.") from error


def _driver_pairs(pairs: Iterable[tuple[object, object]]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for raw_name, raw_share in pairs:
        name = _bounded_text(raw_name, "driver name", _MAX_DRIVER_NAME_LENGTH)
        if name in result:
            raise ValueError(f"Driver {name!r} occurs more than once in attribution.")
        result[name] = _decimal(raw_share, f"driver share for {name}")
    return result


def _driver_parts(item: object) -> tuple[object, object]:
    if isinstance(item, tuple | list) and len(item) == 2:
        return item[0], item[1]
    return (
        _read_any(item, "name", "driver", default=None),
        _read_any(item, "share", default=None),
    )


def _configured_value(value: object | None, *names: str) -> object:
    if value is None:
        return _MISSING
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        nested = value.get("what_changed", _MISSING)
        if nested is not _MISSING:
            return _configured_value(nested, *names)
        return _MISSING
    for name in names:
        candidate = getattr(value, name, _MISSING)
        if candidate is not _MISSING:
            return candidate
    return _MISSING


def _optional_thresholds(
    value: ChangeThresholds | Mapping[str, object] | object | None,
    *,
    reporting_threshold: object | None,
    dominant_driver_share: object | None,
) -> ChangeThresholds | None:
    if value is None and reporting_threshold is None and dominant_driver_share is None:
        return None
    return ChangeThresholds.from_value(
        value,
        reporting_threshold=reporting_threshold,
        dominant_driver_share=dominant_driver_share,
    )


def _probability_delta(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous is None:
        return None
    return current - previous


def _entry_sort_key(entry: _EntryFacts) -> tuple[str, int]:
    return entry.reference, entry.borrower_id.int


def _reverse_text(value: str) -> str:
    # ``max`` uses this only after the numeric share.  Returning the negative
    # codepoint tuple gives a deterministic ascending-name tie-break without
    # relying on input order.
    return "".join(chr(0x10FFFF - ord(character)) for character in value)


def _read_any(value: object, *names: str, default: object = _MISSING) -> object:
    if value is None:
        return default
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        candidate = getattr(value, name, _MISSING)
        if candidate is not _MISSING:
            return candidate
    return default


def _optional_band(value: object, field_name: str) -> None:
    if value is not None and value not in _BAND_RANK:
        raise ValueError(f"{field_name} must be one of act, amber or watch.")


def _normalise_band(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    normalised = _bounded_text(value, field_name, 20).lower()
    _optional_band(normalised, field_name)
    return normalised


def _optional_fraction(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _fraction(value, field_name, allow_one=True)


def _fraction(value: object, field_name: str, *, allow_one: bool) -> Decimal:
    result = _decimal(value, field_name)
    if result < Decimal("0") or (result > Decimal("1") if allow_one else result >= Decimal("1")):
        upper = "one" if allow_one else "one exclusive"
        raise ValueError(f"{field_name} must be between zero and {upper}.")
    return result


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field_name} must be a finite Decimal.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite Decimal.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal.")
    return result


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError(f"{field_name} must be non-empty text of at most {maximum} characters.")
    return value.strip()


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return format(value, "f")


__all__ = [
    "BAND_IMPROVED",
    "BAND_WORSENED",
    "ChangeComparison",
    "ChangeKind",
    "ChangeThresholds",
    "ChangeType",
    "FIRST_RUN",
    "NEWLY_MONITORED",
    "NEWLY_SUPPRESSED",
    "NEWLY_UNMONITORED",
    "NEWLY_UNSUPPRESSED",
    "NEW_TO_ACT",
    "NO_CHANGE",
    "PROBABILITY_DECREASED",
    "PROBABILITY_INCREASED",
    "WhatChanged",
    "apply_what_changed",
    "attach_what_changed",
    "compare_runs",
    "compute_what_changed",
    "what_changed",
]
