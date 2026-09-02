"""Driver attribution for forecast risk contributions (``C-38``).

The forecast stores signed contributions for each driver.  This module turns
those contributions into an ordered, inspectable set of shares.  Positive
drivers at or above the configured T5 share are retained individually;
smaller positive drivers are represented by one ``other`` row.  Negative
drivers are always retained individually because folding a risk-reducing
factor into ``other`` would hide an important part of the explanation.

The normal case uses the signed risk delta as the denominator.  That permits
negative drivers to remain negative while positive drivers may exceed 100%
when a reducing factor offsets part of the gross deterioration.  A set with
no positive net delta cannot be normalized by that denominator without
changing signs or dividing by zero.  In that case the gross absolute
contribution is used and an explicit residual ``other`` row closes the
breakdown to one.  An entirely zero set is a separate neutral result with a
reason rather than an arbitrary division.

Only standard-library types are used here so the domain remains independent
of configuration adapters, persistence and web frameworks.  A threshold
store is accepted through a small structural reader for replay-friendly
callers; the active value is still required and is never replaced by a
module default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, cast

_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_T5_NAME: Final[str] = "T5"
_CONTRIBUTION_SHARE_FIELD: Final[str] = "contribution_share"
_OTHER_NAME: Final[str] = "other"
_NEUTRAL_NAME: Final[str] = "neutral"
_NEUTRAL_REASON: Final[str] = "all driver contributions are zero; no risk delta can be attributed"


@dataclass(frozen=True, slots=True)
class AttributionThresholds:
    """Validated T5 configuration for driver visibility."""

    contribution_share: Decimal

    def __post_init__(self) -> None:
        value = _decimal(self.contribution_share, _CONTRIBUTION_SHARE_FIELD)
        if not _ZERO <= value <= _ONE:
            raise ValueError(f"{_CONTRIBUTION_SHARE_FIELD} must be between zero and one inclusive.")
        object.__setattr__(self, "contribution_share", value)

    @classmethod
    def from_store(cls, store: object) -> AttributionThresholds:
        """Read T5 from a threshold store or an adapter-neutral mapping.

        Supported shapes are ``store.get('T5')`` with a
        ``contribution_share`` field, ``{'T5': {...}}`` and a direct T5 field
        mapping/object.  Missing or malformed configuration is rejected so a
        scoring run cannot silently fall back to a policy value.
        """

        if isinstance(store, cls):
            return store
        section = _threshold_section(store)
        value = _read(section, _CONTRIBUTION_SHARE_FIELD)
        return cls(value)


@dataclass(frozen=True, slots=True)
class DriverShare:
    """One named, normalized driver share.

    ``share`` is signed.  The neutral row is the only row that carries a
    reason; ordinary rows are explained by their name and their retained
    signed share.
    """

    name: str
    share: Decimal
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Driver name must not be blank.")
        share = _decimal(self.share, f"{self.name} share")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError(f"{self.name} reason must not be blank.")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "share", share)
        if self.reason is not None:
            object.__setattr__(self, "reason", self.reason.strip())

    @property
    def normalized_share(self) -> Decimal:
        """Return the share using the American spelling used by the API."""

        return self.share

    @property
    def normalised_share(self) -> Decimal:
        """Return the share using the spelling used by product prose."""

        return self.share

    @property
    def contribution(self) -> Decimal:
        """Compatibility view for consumers that call shares contributions."""

        return self.share

    @property
    def value(self) -> Decimal:
        """Return the normalized value for generic result consumers."""

        return self.share

    @property
    def is_other(self) -> bool:
        """Whether this row contains folded contributions."""

        return self.name == _OTHER_NAME

    @property
    def is_neutral(self) -> bool:
        """Whether this row documents the no-signal result."""

        return self.name == _NEUTRAL_NAME


def attribute(
    terms: Mapping[str, Decimal],
    threshold_t5: Decimal | AttributionThresholds | Mapping[str, object] | object,
) -> list[DriverShare]:
    """Normalize signed driver contributions and apply the inclusive T5 cut.

    ``threshold_t5`` is normally the configured ``Decimal`` share.  A T5
    store or T5-shaped mapping is also accepted so callers do not have to
    duplicate threshold extraction at an adapter boundary.

    The input mapping order is retained for listed drivers, making the result
    deterministic for ordered sources and preserving the source's explanation
    order.  ``other`` is appended after listed drivers when any contribution
    is folded or when a non-positive signed delta requires a residual to make
    the returned shares sum to one.
    """

    if not isinstance(terms, Mapping):
        raise TypeError("terms must be a mapping of driver names to contributions.")
    threshold = _resolve_threshold(threshold_t5)
    contributions = _validated_terms(terms)
    if not contributions or all(value == _ZERO for value in contributions.values()):
        return [DriverShare(_NEUTRAL_NAME, _ONE, _NEUTRAL_REASON)]

    normalized, requires_residual = _normalize(contributions)
    listed: list[DriverShare] = []
    folded_names: list[str] = []
    for name, share in normalized.items():
        if share < _ZERO or share >= threshold:
            listed.append(DriverShare(name, share))
        else:
            folded_names.append(name)

    listed_total = sum((row.share for row in listed), _ZERO)
    if folded_names or requires_residual:
        other_share = _ONE - listed_total
        if other_share != _ZERO:
            listed.append(DriverShare(_OTHER_NAME, other_share))

    return listed


def _resolve_threshold(value: object) -> Decimal:
    if isinstance(value, AttributionThresholds):
        return value.contribution_share
    if isinstance(value, Mapping):
        return AttributionThresholds.from_store(value).contribution_share
    if isinstance(value, Decimal | int | str) and not isinstance(value, bool):
        return AttributionThresholds(cast(Decimal, value)).contribution_share
    return AttributionThresholds.from_store(value).contribution_share


def _validated_terms(terms: Mapping[str, Decimal]) -> dict[str, Decimal]:
    validated: dict[str, Decimal] = {}
    for raw_name, raw_value in terms.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("Every driver name must be non-empty text.")
        name = raw_name.strip()
        if name in {_OTHER_NAME, _NEUTRAL_NAME}:
            raise ValueError(f"Driver name {name!r} is reserved for attribution output.")
        if name in validated:
            raise ValueError(f"Driver names must be unique after trimming: {name!r}.")
        validated[name] = _decimal(raw_value, f"{name} contribution")
    return validated


def _normalize(
    contributions: Mapping[str, Decimal],
) -> tuple[dict[str, Decimal], bool]:
    total = sum(contributions.values(), _ZERO)
    if not total.is_finite():
        raise ValueError("Driver contribution total must be finite.")
    if total > _ZERO:
        denominator = total
        return (
            {name: value / denominator for name, value in contributions.items()},
            False,
        )

    gross = sum((abs(value) for value in contributions.values()), _ZERO)
    if not gross.is_finite() or gross <= _ZERO:
        raise ValueError("Driver contribution magnitude must be finite and non-zero.")
    return (
        {name: value / gross for name, value in contributions.items()},
        True,
    )


def _threshold_section(store: object) -> object:
    if isinstance(store, AttributionThresholds):
        return {_CONTRIBUTION_SHARE_FIELD: store.contribution_share}
    if isinstance(store, Mapping):
        if _T5_NAME in store:
            return store[_T5_NAME]
        return store
    getter = getattr(store, "get", None)
    if callable(getter):
        try:
            section = getter(_T5_NAME)
        except (KeyError, TypeError):
            section = None
        if section is not None:
            return section
    for name in (_T5_NAME, "t5", "attribution"):
        section = getattr(store, name, None)
        if section is not None:
            return section
    raise ValueError("T5 threshold store is missing.")


def _read(section: object, field_name: str) -> Decimal:
    if isinstance(section, Mapping):
        if field_name not in section:
            raise ValueError(f"T5 threshold is missing {field_name!r}.")
        value = section[field_name]
    else:
        marker = object()
        value = getattr(section, field_name, marker)
        if value is marker:
            raise ValueError(f"T5 threshold is missing {field_name!r}.")
    return _decimal(value, f"T5 threshold {field_name!r}")


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
    "AttributionThresholds",
    "DriverShare",
    "attribute",
]
