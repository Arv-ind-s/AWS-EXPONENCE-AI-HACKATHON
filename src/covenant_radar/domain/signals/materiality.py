"""Materiality scoring for the evidence ledger.

Materiality is the T4 half of evidence scoring.  An evidence item can affect
more than one covenant, so each affected covenant is evaluated independently
and the largest projected 90-day headroom erosion becomes the item's score.
Only deterioration is erosion: a projected improvement contributes zero.

The covenant engine expresses headroom as percentage points (``5`` means
5 percent of the covenant threshold), while the versioned threshold store
expresses T4 as a fraction (``0.05`` means 5 percent).  This module keeps
both representations explicit.  ``MaterialityScore.materiality_pct`` is the
ledger/storage representation; ``MaterialityScore.materiality`` is the
fraction used when materiality participates in forecast pressure.

The scorer consumes facts rather than ORM rows.  A caller may provide
precomputed current and projected headroom, or the corresponding current and
90-day projected values plus the signed covenant terms.  Invalid or
non-applicable covenant projections are retained in the explainable result
with a reason and are excluded from the maximum.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, cast
from uuid import UUID

from covenant_radar.domain.covenants.headroom import signed_headroom

_T4_NAME: Final[str] = "T4"
_T4_FIELD: Final[str] = "headroom_erosion_pct"
_PERCENT: Final[Decimal] = Decimal("100")
_ZERO: Final[Decimal] = Decimal("0")
_VALID_DIRECTIONS: Final[frozenset[str]] = frozenset({"min", "max"})
_MAX_IDENTIFIER_LENGTH: Final[int] = 200


@dataclass(frozen=True, slots=True)
class MaterialityThresholds:
    """Validated T4 values read from the active threshold store.

    ``headroom_erosion_pct`` is a fraction on ``[0, 1]``.  It is deliberately
    not given a product default; a scoring run must use the approved store
    that belongs to that run.
    """

    headroom_erosion_pct: Decimal

    def __post_init__(self) -> None:
        _validate_decimal(self.headroom_erosion_pct, _T4_FIELD)
        if not _ZERO <= self.headroom_erosion_pct <= Decimal("1"):
            raise ValueError(f"{_T4_FIELD} must be between zero and one.")

    @classmethod
    def from_store(cls, store: object) -> MaterialityThresholds:
        """Read T4 from a threshold store or threshold mapping.

        Supported stores intentionally mirror the adapter-neutral access
        shape used by the persistence scorer: ``ThresholdStore.get('T4')``,
        ``{'T4': {...}}`` and a direct T4 field mapping/object.
        """

        section = _threshold_section(store)
        value = _read(section, _T4_FIELD)
        return cls(cast(Decimal, value))

    @property
    def threshold_pct(self) -> Decimal:
        """Return the T4 boundary in headroom percentage points."""

        return self.headroom_erosion_pct * _PERCENT


@dataclass(frozen=True, slots=True)
class CovenantHeadroom:
    """Current and projected headroom facts for one affected covenant.

    Headroom fields are percentage points.  Alternatively, callers can pass
    ``current_value`` and ``projected_value_90d`` and the scorer derives the
    two headrooms with :func:`signed_headroom`.  A non-zero threshold is
    required even when headroom was precomputed because T4 is defined against
    the covenant threshold and zero/absent thresholds are not meaningful.
    """

    covenant_id: UUID | str
    threshold: Decimal | None
    current_headroom_pct: Decimal | None = None
    projected_headroom_pct: Decimal | None = None
    direction: str | None = None
    current_value: Decimal | None = None
    projected_value_90d: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "covenant_id", _identifier(self.covenant_id))
        if self.threshold is not None:
            _validate_decimal(self.threshold, "threshold")
        if self.direction is not None and self.direction not in _VALID_DIRECTIONS:
            raise ValueError("direction must be either 'min' or 'max'.")
        for value, name in (
            (self.current_headroom_pct, "current_headroom_pct"),
            (self.projected_headroom_pct, "projected_headroom_pct"),
            (self.current_value, "current_value"),
            (self.projected_value_90d, "projected_value_90d"),
        ):
            if value is not None:
                _validate_decimal(value, name)

        has_headroom = (
            self.current_headroom_pct is not None or self.projected_headroom_pct is not None
        )
        has_values = self.current_value is not None or self.projected_value_90d is not None
        if has_headroom and has_values:
            raise ValueError("Provide headroom facts or value facts, not both.")
        if has_headroom and (
            self.current_headroom_pct is None or self.projected_headroom_pct is None
        ):
            raise ValueError("Both current and projected headroom are required.")
        if has_values and (self.current_value is None or self.projected_value_90d is None):
            raise ValueError("Both current and projected covenant values are required.")
        if has_values and self.direction is None:
            raise ValueError("direction is required when covenant values are provided.")

    @classmethod
    def from_values(
        cls,
        covenant_id: UUID | str,
        *,
        threshold: Decimal | None,
        direction: str | None,
        current_value: Decimal | None,
        projected_value_90d: Decimal | None,
    ) -> CovenantHeadroom:
        """Build facts from current and projected covenant values."""

        return cls(
            covenant_id=covenant_id,
            threshold=threshold,
            direction=direction,
            current_value=current_value,
            projected_value_90d=projected_value_90d,
        )

    @property
    def current_headroom(self) -> Decimal | None:
        """Compatibility spelling for current headroom percentage points."""

        return self.current_headroom_pct

    @property
    def projected_headroom(self) -> Decimal | None:
        """Compatibility spelling for projected headroom percentage points."""

        return self.projected_headroom_pct


@dataclass(frozen=True, slots=True)
class CovenantMateriality:
    """Explainable T4 calculation for one covenant projection."""

    covenant_id: UUID | str
    threshold: Decimal | None
    current_headroom_pct: Decimal | None
    projected_headroom_pct: Decimal | None
    erosion_pct: Decimal
    included: bool
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "covenant_id", _identifier(self.covenant_id))
        if self.threshold is not None:
            _validate_decimal(self.threshold, "threshold")
        for value, name in (
            (self.current_headroom_pct, "current_headroom_pct"),
            (self.projected_headroom_pct, "projected_headroom_pct"),
            (self.erosion_pct, "erosion_pct"),
        ):
            if value is not None:
                _validate_decimal(value, name)
        if self.erosion_pct < _ZERO:
            raise ValueError("erosion_pct must not be negative.")
        if not isinstance(self.included, bool):
            raise TypeError("included must be a boolean.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be non-empty text.")
        if not self.included and self.erosion_pct != _ZERO:
            raise ValueError("An excluded covenant must have zero erosion_pct.")

    @property
    def materiality(self) -> Decimal:
        """Return erosion as a fraction of the covenant threshold."""

        return self.erosion_pct / _PERCENT


@dataclass(frozen=True, slots=True)
class MaterialityScore:
    """The maximum explainable materiality score for one evidence item."""

    materiality_pct: Decimal
    counts_toward_pressure: bool
    driving_covenant_id: UUID | str | None
    reason: str
    thresholds: MaterialityThresholds
    covenant_scores: tuple[CovenantMateriality, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_decimal(self.materiality_pct, "materiality_pct")
        if self.materiality_pct < _ZERO:
            raise ValueError("materiality_pct must not be negative.")
        if not isinstance(self.counts_toward_pressure, bool):
            raise TypeError("counts_toward_pressure must be a boolean.")
        if not isinstance(self.thresholds, MaterialityThresholds):
            raise TypeError("thresholds must be MaterialityThresholds.")
        if self.driving_covenant_id is not None:
            object.__setattr__(self, "driving_covenant_id", _identifier(self.driving_covenant_id))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be non-empty text.")
        if not isinstance(self.covenant_scores, tuple):
            object.__setattr__(self, "covenant_scores", tuple(self.covenant_scores))
        if any(not isinstance(score, CovenantMateriality) for score in self.covenant_scores):
            raise TypeError("covenant_scores must contain CovenantMateriality values.")
        expected_counts = (
            self.driving_covenant_id is not None
            and self.materiality_pct >= self.thresholds.threshold_pct
        )
        if self.counts_toward_pressure is not expected_counts:
            raise ValueError("counts_toward_pressure must agree with the T4 boundary.")
        if self.driving_covenant_id is None and self.materiality_pct != _ZERO:
            raise ValueError("A non-zero materiality score requires a driving covenant.")
        eligible = tuple(score for score in self.covenant_scores if score.included)
        if eligible:
            maximum = max(
                eligible,
                key=lambda score: (score.erosion_pct, _identifier_text(score.covenant_id)),
            )
            if self.materiality_pct != maximum.erosion_pct:
                raise ValueError("materiality_pct must equal the maximum eligible erosion.")
            if self.driving_covenant_id != maximum.covenant_id:
                raise ValueError("driving_covenant_id must name the maximum eligible erosion.")
        elif self.materiality_pct != _ZERO or self.driving_covenant_id is not None:
            raise ValueError("No eligible covenant can only produce zero materiality.")

    @property
    def materiality(self) -> Decimal:
        """Return the score as a fraction for pressure calculations."""

        return self.materiality_pct / _PERCENT

    @property
    def driver(self) -> UUID | str | None:
        """Compatibility spelling for the covenant driving the maximum."""

        return self.driving_covenant_id

    @property
    def driving_covenant(self) -> UUID | str | None:
        """Compatibility spelling for the covenant driving the maximum."""

        return self.driving_covenant_id


def score_materiality(
    covenant_headrooms: Iterable[CovenantHeadroom | Mapping[str, object] | object],
    thresholds: object,
) -> MaterialityScore:
    """Apply T4 to one evidence item's affected covenant projections.

    The maximum is selected deterministically by erosion and then covenant
    identifier.  A projection that has no usable covenant threshold, a zero
    threshold, or incomplete headroom facts is retained as an excluded
    explainability row.  An empty input is a valid item with zero materiality,
    so callers can keep it visible in the ledger without special casing.
    """

    configured = (
        thresholds
        if isinstance(thresholds, MaterialityThresholds)
        else MaterialityThresholds.from_store(thresholds)
    )
    projections = tuple(_normalise_inputs(covenant_headrooms))
    if not projections:
        return _zero_score(configured, "no affected covenant")

    calculations = tuple(_calculate_projection(projection) for projection in projections)
    eligible = tuple(calculation for calculation in calculations if calculation.included)
    if not eligible:
        reasons = "; ".join(calculation.reason for calculation in calculations)
        return _zero_score(configured, reasons, covenant_scores=calculations)

    maximum = max(
        eligible,
        key=lambda calculation: (
            calculation.erosion_pct,
            _identifier_text(calculation.covenant_id),
        ),
    )
    counts = maximum.erosion_pct >= configured.threshold_pct
    if counts:
        reason = (
            f"maximum projected headroom erosion for covenant "
            f"{maximum.covenant_id!s} meets T4 threshold "
            f"{_decimal_text(configured.threshold_pct)}%"
        )
    else:
        reason = (
            f"maximum projected headroom erosion for covenant "
            f"{maximum.covenant_id!s} is below T4 threshold "
            f"{_decimal_text(configured.threshold_pct)}%"
        )
    return MaterialityScore(
        materiality_pct=maximum.erosion_pct,
        counts_toward_pressure=counts,
        driving_covenant_id=maximum.covenant_id,
        reason=reason,
        thresholds=configured,
        covenant_scores=calculations,
    )


def projected_headroom_erosion(
    *,
    current_headroom_pct: Decimal,
    projected_headroom_pct: Decimal,
) -> Decimal:
    """Return non-negative 90-day headroom erosion in percentage points."""

    _validate_decimal(current_headroom_pct, "current_headroom_pct")
    _validate_decimal(projected_headroom_pct, "projected_headroom_pct")
    return max(_ZERO, current_headroom_pct - projected_headroom_pct)


def _calculate_projection(projection: CovenantHeadroom) -> CovenantMateriality:
    if projection.threshold is None:
        return _excluded(projection, "excluded: covenant threshold is absent")
    if projection.threshold == _ZERO:
        return _excluded(projection, "excluded: covenant threshold is zero")

    try:
        current, projected = _headrooms(projection)
    except (ArithmeticError, TypeError, ValueError) as error:
        return _excluded(projection, f"excluded: headroom is unavailable ({error})")
    erosion = projected_headroom_erosion(
        current_headroom_pct=current,
        projected_headroom_pct=projected,
    )
    return CovenantMateriality(
        covenant_id=projection.covenant_id,
        threshold=projection.threshold,
        current_headroom_pct=current,
        projected_headroom_pct=projected,
        erosion_pct=erosion,
        included=True,
        reason="included: usable covenant headroom",
    )


def _headrooms(projection: CovenantHeadroom) -> tuple[Decimal, Decimal]:
    if (
        projection.current_headroom_pct is not None
        and projection.projected_headroom_pct is not None
    ):
        return projection.current_headroom_pct, projection.projected_headroom_pct
    if (
        projection.current_value is None
        or projection.projected_value_90d is None
        or projection.direction is None
    ):
        raise ValueError("both current and projected headroom facts are required")
    assert projection.threshold is not None
    return (
        signed_headroom(projection.current_value, projection.threshold, projection.direction),
        signed_headroom(projection.projected_value_90d, projection.threshold, projection.direction),
    )


def _excluded(projection: CovenantHeadroom, reason: str) -> CovenantMateriality:
    return CovenantMateriality(
        covenant_id=projection.covenant_id,
        threshold=projection.threshold,
        current_headroom_pct=None,
        projected_headroom_pct=None,
        erosion_pct=_ZERO,
        included=False,
        reason=reason,
    )


def _zero_score(
    thresholds: MaterialityThresholds,
    reason: str,
    *,
    covenant_scores: tuple[CovenantMateriality, ...] = (),
) -> MaterialityScore:
    return MaterialityScore(
        materiality_pct=_ZERO,
        counts_toward_pressure=False,
        driving_covenant_id=None,
        reason=reason,
        thresholds=thresholds,
        covenant_scores=covenant_scores,
    )


def _normalise_inputs(
    values: Iterable[CovenantHeadroom | Mapping[str, object] | object],
) -> tuple[CovenantHeadroom, ...]:
    if isinstance(values, Mapping):
        if _looks_like_projection(values):
            return (_projection_from_mapping(values),)
        return tuple(
            _projection_from_value(value, covenant_id=key) for key, value in values.items()
        )
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("covenant_headrooms must be an iterable of covenant projections.")
    try:
        return tuple(_projection_from_value(value) for value in values)
    except TypeError as error:
        raise TypeError(
            "covenant_headrooms must be an iterable of covenant projections."
        ) from error


def _projection_from_value(value: object, covenant_id: object | None = None) -> CovenantHeadroom:
    if isinstance(value, CovenantHeadroom):
        if covenant_id is None or value.covenant_id == _identifier(covenant_id):
            return value
        return CovenantHeadroom(
            covenant_id=cast(UUID | str, covenant_id),
            threshold=value.threshold,
            current_headroom_pct=value.current_headroom_pct,
            projected_headroom_pct=value.projected_headroom_pct,
            direction=value.direction,
            current_value=value.current_value,
            projected_value_90d=value.projected_value_90d,
        )
    if isinstance(value, Mapping):
        return _projection_from_mapping(value, covenant_id=covenant_id)
    identifier = covenant_id if covenant_id is not None else _read_any(value, "covenant_id", "id")
    return CovenantHeadroom(
        covenant_id=cast(UUID | str, identifier),
        threshold=cast(
            Decimal | None,
            _read_any(value, "threshold", "threshold_used", default=None),
        ),
        current_headroom_pct=cast(
            Decimal | None,
            _read_any(value, "current_headroom_pct", "current_headroom", default=None),
        ),
        projected_headroom_pct=cast(
            Decimal | None,
            _read_any(
                value,
                "projected_headroom_pct",
                "projected_headroom",
                default=None,
            ),
        ),
        direction=cast(str | None, _read_any(value, "direction", default=None)),
        current_value=cast(Decimal | None, _read_any(value, "current_value", default=None)),
        projected_value_90d=cast(
            Decimal | None,
            _read_any(value, "projected_value_90d", "projected_value", default=None),
        ),
    )


def _projection_from_mapping(
    value: Mapping[str, object], covenant_id: object | None = None
) -> CovenantHeadroom:
    identifier = covenant_id if covenant_id is not None else _read_any(value, "covenant_id", "id")
    return CovenantHeadroom(
        covenant_id=cast(UUID | str, identifier),
        threshold=cast(
            Decimal | None,
            _read_any(value, "threshold", "threshold_used", default=None),
        ),
        current_headroom_pct=cast(
            Decimal | None,
            _read_any(value, "current_headroom_pct", "current_headroom", default=None),
        ),
        projected_headroom_pct=cast(
            Decimal | None,
            _read_any(
                value,
                "projected_headroom_pct",
                "projected_headroom",
                default=None,
            ),
        ),
        direction=cast(str | None, _read_any(value, "direction", default=None)),
        current_value=cast(Decimal | None, _read_any(value, "current_value", default=None)),
        projected_value_90d=cast(
            Decimal | None,
            _read_any(value, "projected_value_90d", "projected_value", default=None),
        ),
    )


def _threshold_section(store: object) -> object:
    if isinstance(store, MaterialityThresholds):
        return {_T4_FIELD: store.headroom_erosion_pct}
    if isinstance(store, Mapping):
        if _T4_NAME in store:
            return store[_T4_NAME]
        return store
    getter = getattr(store, "get", None)
    if callable(getter):
        try:
            return getter(_T4_NAME)
        except (KeyError, TypeError):
            pass
    for name in (_T4_NAME, "t4", "materiality"):
        value = getattr(store, name, None)
        if value is not None:
            return value
    raise ValueError("T4 threshold store is missing.")


def _read(value: object, name: str) -> object:
    result = _read_any(value, name, default=None)
    if result is None:
        raise ValueError(f"T4 threshold is missing {name!r}.")
    if not isinstance(result, Decimal):
        raise TypeError(f"T4 threshold {name!r} must be a Decimal.")
    return result


def _read_any(
    value: object,
    *names: str,
    default: object = ...,
) -> object:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        marker = object()
        result = getattr(value, name, marker)
        if result is not marker:
            return result
    if default is not ...:
        return default
    joined = " or ".join(repr(name) for name in names)
    raise ValueError(f"Input is missing required field {joined}.")


def _looks_like_projection(value: Mapping[str, object]) -> bool:
    return any(
        key in value
        for key in (
            "covenant_id",
            "threshold",
            "threshold_used",
            "current_headroom_pct",
            "projected_headroom_pct",
            "current_value",
            "projected_value_90d",
        )
    )


def _identifier(value: object) -> UUID | str:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("covenant_id must not be empty.")
        if len(cleaned) > _MAX_IDENTIFIER_LENGTH:
            raise ValueError(f"covenant_id must be at most {_MAX_IDENTIFIER_LENGTH} characters.")
        if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
            raise ValueError("covenant_id contains a control character.")
        return cleaned
    raise TypeError("covenant_id must be a UUID or text.")


def _identifier_text(value: UUID | str) -> str:
    return str(value)


def _decimal_text(value: Decimal) -> str:
    """Render a finite Decimal without redundant trailing zeroes."""

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _validate_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal.")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite.")


# Noun-first aliases follow the discoverability convention used by the other
# pure signal stages while retaining one implementation of the T4 rule.
CovenantHeadroomFacts = CovenantHeadroom
CovenantProjection = CovenantHeadroom
MaterialityResult = MaterialityScore
materiality_score = score_materiality
compute_materiality = score_materiality
calculate_materiality = score_materiality


__all__ = [
    "CovenantHeadroom",
    "CovenantHeadroomFacts",
    "CovenantMateriality",
    "CovenantProjection",
    "MaterialityResult",
    "MaterialityScore",
    "MaterialityThresholds",
    "calculate_materiality",
    "compute_materiality",
    "materiality_score",
    "projected_headroom_erosion",
    "score_materiality",
]
