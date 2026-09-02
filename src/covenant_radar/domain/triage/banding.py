"""Portfolio triage bands and their versioned T1/T2 configuration.

The triage domain deliberately consumes threshold configuration through a
small structural interface.  It therefore remains independent of the
configuration store, persistence and presentation layers while still making
the active threshold snapshot part of every ranking decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final, cast

ACT_BAND: Final[str] = "act"
AMBER_BAND: Final[str] = "amber"
WATCH_BAND: Final[str] = "watch"
BANDS: Final[tuple[str, ...]] = (ACT_BAND, AMBER_BAND, WATCH_BAND)

_ACT_THRESHOLD_FIELD: Final[str] = "act"
_AMBER_THRESHOLD_FIELD: Final[str] = "amber"
_CONFIDENCE_FLOOR_FIELD: Final[str] = "confidence_floor"
_T1_NAME: Final[str] = "T1"
_T2_NAME: Final[str] = "T2"
_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")


@dataclass(frozen=True, slots=True)
class TriageThresholds:
    """The threshold values required by portfolio triage.

    T1 owns banding and T2 owns the confidence guard.  Values are normalized
    to finite ``Decimal`` instances once at the domain boundary.  No policy
    defaults are introduced when a caller supplies a configuration store;
    missing or malformed values fail closed.
    """

    act: Decimal
    amber: Decimal
    confidence_floor: Decimal

    def __post_init__(self) -> None:
        act = _fraction(self.act, "T1.act")
        amber = _fraction(self.amber, "T1.amber")
        confidence_floor = _fraction(self.confidence_floor, "T2.confidence_floor")
        if amber > act:
            raise ValueError("T1.amber must not exceed T1.act.")
        object.__setattr__(self, "act", act)
        object.__setattr__(self, "amber", amber)
        object.__setattr__(self, "confidence_floor", confidence_floor)

    @classmethod
    def from_store(cls, store: object) -> TriageThresholds:
        """Read T1 and T2 from an adapter-neutral threshold store.

        Supported forms include ``ThresholdStore.get(name)``, a mapping with
        T1/T2 sections, and an object exposing T1/T2 sections.  A direct
        three-field mapping/object is accepted for pure-domain callers.
        """

        if isinstance(store, cls):
            return store
        if isinstance(store, Mapping):
            if _T1_NAME in store or _T2_NAME in store:
                t1 = store.get(_T1_NAME)
                t2 = store.get(_T2_NAME)
                return cls(
                    _required_decimal(t1, _ACT_THRESHOLD_FIELD, "T1"),
                    _required_decimal(t1, _AMBER_THRESHOLD_FIELD, "T1"),
                    _required_decimal(t2, _CONFIDENCE_FLOOR_FIELD, "T2"),
                )
            return cls(
                _required_decimal(store, _ACT_THRESHOLD_FIELD, "T1"),
                _required_decimal(store, _AMBER_THRESHOLD_FIELD, "T1"),
                _required_decimal(store, _CONFIDENCE_FLOOR_FIELD, "T2"),
            )

        getter = getattr(store, "get", None)
        if callable(getter):
            t1 = _call_getter(getter, _T1_NAME)
            t2 = _call_getter(getter, _T2_NAME)
            if t1 is not None or t2 is not None:
                return cls(
                    _required_decimal(t1, _ACT_THRESHOLD_FIELD, "T1"),
                    _required_decimal(t1, _AMBER_THRESHOLD_FIELD, "T1"),
                    _required_decimal(t2, _CONFIDENCE_FLOOR_FIELD, "T2"),
                )

        t1 = _attribute_section(store, _T1_NAME, "t1")
        t2 = _attribute_section(store, _T2_NAME, "t2")
        if t1 is not None or t2 is not None:
            return cls(
                _required_decimal(t1, _ACT_THRESHOLD_FIELD, "T1"),
                _required_decimal(t1, _AMBER_THRESHOLD_FIELD, "T1"),
                _required_decimal(t2, _CONFIDENCE_FLOOR_FIELD, "T2"),
            )

        return cls(
            _required_decimal(store, _ACT_THRESHOLD_FIELD, "T1"),
            _required_decimal(store, _AMBER_THRESHOLD_FIELD, "T1"),
            _required_decimal(store, _CONFIDENCE_FLOOR_FIELD, "T2"),
        )

    def as_mapping(self) -> Mapping[str, Mapping[str, Decimal]]:
        """Return the normalized values in the threshold-store shape."""

        return MappingProxyType(
            {
                _T1_NAME: MappingProxyType(
                    {_ACT_THRESHOLD_FIELD: self.act, _AMBER_THRESHOLD_FIELD: self.amber}
                ),
                _T2_NAME: MappingProxyType({_CONFIDENCE_FLOOR_FIELD: self.confidence_floor}),
            }
        )


# ``Thresholds`` is the short name used by the C-39 contract.
Thresholds = TriageThresholds


def band(
    probability: Decimal | int | str | None,
    thresholds: TriageThresholds | Mapping[str, object] | object,
) -> str:
    """Return the inclusive T1 band for one displayed probability.

    The higher band owns its boundary.  ``None`` is never treated as zero:
    it denotes an absent/suppressed probability and is therefore watch.
    """

    configured = TriageThresholds.from_store(thresholds)
    if probability is None:
        return WATCH_BAND
    value = _fraction(probability, "probability")
    if value >= configured.act:
        return ACT_BAND
    if value >= configured.amber:
        return AMBER_BAND
    return WATCH_BAND


def band_for_probability(
    probability: Decimal | int | str | None,
    thresholds: TriageThresholds | Mapping[str, object] | object,
) -> str:
    """Compatibility spelling for :func:`band`."""

    return band(probability, thresholds)


def classify_band(
    probability: Decimal | int | str | None,
    thresholds: TriageThresholds | Mapping[str, object] | object,
) -> str:
    """Compatibility spelling for callers that use classification language."""

    return band(probability, thresholds)


def _call_getter(getter: Callable[[str], object], name: str) -> object | None:
    try:
        value = getter(name)
    except (KeyError, TypeError):
        return None
    return value


def _attribute_section(store: object, *names: str) -> object | None:
    for name in names:
        section = cast(object | None, getattr(store, name, None))
        if section is not None:
            return section
    return None


def _required(section: object | None, field_name: str, threshold_name: str) -> object:
    if section is None:
        raise ValueError(f"{threshold_name} threshold section is missing.")
    if isinstance(section, Mapping):
        if field_name not in section:
            raise ValueError(f"{threshold_name} threshold is missing {field_name!r}.")
        return section[field_name]
    marker = object()
    value = cast(object, getattr(section, field_name, marker))
    if value is marker:
        raise ValueError(f"{threshold_name} threshold is missing {field_name!r}.")
    return value


def _required_decimal(section: object | None, field_name: str, threshold_name: str) -> Decimal:
    return _decimal(
        _required(section, field_name, threshold_name), f"{threshold_name}.{field_name}"
    )


def _fraction(value: object, field_name: str) -> Decimal:
    result = _decimal(value, field_name)
    if not result.is_finite() or not _ZERO <= result <= _ONE:
        raise ValueError(f"{field_name} must be between zero and one inclusive.")
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


__all__ = [
    "ACT_BAND",
    "AMBER_BAND",
    "BANDS",
    "TriageThresholds",
    "Thresholds",
    "WATCH_BAND",
    "band",
    "band_for_probability",
    "classify_band",
]
