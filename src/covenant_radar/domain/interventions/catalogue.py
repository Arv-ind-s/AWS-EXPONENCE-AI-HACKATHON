"""The bounded intervention catalogue used by recommendation and simulation.

Catalogue rows are configuration, not model output.  This module keeps the
configuration boundary independent from SQLAlchemy and validates the closed
effect-model vocabulary before a row can reach the service layer.  A catalogue
entry is therefore always executable by the deterministic simulator, or it is
refused at construction time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.interventions.applicability import (
    CovenantClass,
    InterventionNotApplicable,
    normalize_covenant_classes,
    require_applicable,
)
from covenant_radar.domain.interventions.effects import (
    CombinationEffect,
    EffectModelType,
    InterventionEffect,
    InterventionFacts,
    build_effect,
)

_MAX_ID_LENGTH: Final[int] = 50
_MAX_TEXT_LENGTH: Final[int] = 2_000
_ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,49}$"


class RoleTag(StrEnum):
    """The three desks that may receive an advisory intervention."""

    RELATIONSHIP_MANAGER = "relationship_manager"
    RM = "relationship_manager"
    CREDIT = "credit"
    RISK = "risk"

    @classmethod
    def from_value(cls, value: RoleTag | str) -> RoleTag:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("role_tag must be text or a RoleTag.")
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized == "rm":
            normalized = cls.RELATIONSHIP_MANAGER.value
        try:
            return cls(normalized)
        except ValueError as error:
            raise ValueError(
                "role_tag must be one of: relationship_manager (RM), credit, risk."
            ) from error


def normalize_role_tag(value: RoleTag | str) -> RoleTag:
    """Normalize a role tag while retaining one canonical persisted value."""

    return RoleTag.from_value(value)


class CatalogueEntry:
    """An immutable, simulator-backed action catalogue entry.

    ``id`` is the stable bank-facing intervention code.  ``database_id`` is
    the internal UUID used by historical simulation and memo records.  The
    distinction lets a code remain stable and human-readable without making
    callers expose database identifiers in configuration.

    ``effect_parameters`` may carry the private ``_assumptions`` key when
    loaded from the JSON/ORM representation.  It is consumed at the boundary
    and never passed to the effect factory, which remains strict about model
    parameters.  The public property contains only parameters understood by
    the selected effect model.
    """

    __slots__ = (
        "_id",
        "_database_id",
        "_role_tag",
        "_text",
        "_effect",
        "_requires_approval",
        "_is_active",
        "_retired_at",
        "_version",
        "_sealed",
    )

    def __init__(
        self,
        id: str | None = None,
        role_tag: RoleTag | str | None = None,
        text: str | None = None,
        effect_model: EffectModelType | str | InterventionEffect | None = None,
        effect_parameters: Mapping[str, object] | None = None,
        applicable_covenant_classes: Iterable[CovenantClass | str] | None = None,
        requires_approval: bool = False,
        is_active: bool = True,
        retired_at: datetime | None = None,
        version: int = 0,
        *,
        code: str | None = None,
        intervention_id: UUID | str | None = None,
        database_id: UUID | None = None,
        effect: InterventionEffect | None = None,
        assumptions: Iterable[str] | None = None,
    ) -> None:
        identifier = _identifier(id, code)
        resolved_database_id = _database_id(intervention_id, database_id)
        normalized_role = _role_tag(role_tag)
        normalized_text = _text(text)
        normalized_active = _boolean(is_active, "is_active")
        normalized_approval = _boolean(requires_approval, "requires_approval")
        normalized_retired_at = _utc_or_none(retired_at, "retired_at")
        if normalized_active and normalized_retired_at is not None:
            raise ValidationError(
                "An active intervention cannot have retired_at set.",
                field="intervention.retired_at",
            )

        normalized_version = _version(version)
        built_effect = _effect(
            effect_model=effect_model,
            effect_parameters=effect_parameters,
            applicable_covenant_classes=applicable_covenant_classes,
            assumptions=assumptions,
            supplied_effect=effect,
        )
        self._id = identifier
        self._database_id = resolved_database_id
        self._role_tag = normalized_role
        self._text = normalized_text
        self._effect = built_effect
        self._requires_approval = normalized_approval
        self._is_active = normalized_active
        self._retired_at = normalized_retired_at
        self._version = normalized_version
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("CatalogueEntry is immutable.")
        object.__setattr__(self, name, value)

    @property
    def id(self) -> str:
        """Return the stable bank-facing intervention identifier."""

        return self._id

    @property
    def code(self) -> str:
        """Compatibility spelling for the persisted intervention code."""

        return self._id

    @property
    def database_id(self) -> UUID | None:
        """Return the internal UUID, when the entry is persisted."""

        return self._database_id

    @property
    def intervention_id(self) -> UUID | None:
        """Compatibility spelling for the internal intervention UUID."""

        return self._database_id

    @property
    def role_tag(self) -> RoleTag:
        return self._role_tag

    @property
    def text(self) -> str:
        return self._text

    @property
    def effect(self) -> InterventionEffect:
        return self._effect

    @property
    def effect_model(self) -> EffectModelType:
        return self._effect.model_type

    @property
    def effect_parameters(self) -> Mapping[str, object]:
        return self._effect.effect_parameters

    @property
    def assumptions(self) -> tuple[str, ...]:
        return self._effect.assumptions

    @property
    def applicable_covenant_classes(self) -> frozenset[str]:
        return self._effect.applicable_covenant_classes

    @property
    def requires_approval(self) -> bool:
        return self._requires_approval

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def is_retired(self) -> bool:
        return not self._is_active

    @property
    def retired_at(self) -> datetime | None:
        return self._retired_at

    @property
    def version(self) -> int:
        """Return the optimistic-concurrency version of the persisted row."""

        return self._version

    def is_applicable(self, covenant_class: CovenantClass | str) -> bool:
        """Return whether this entry is applicable to a covenant class."""

        try:
            require_applicable(self._effect, covenant_class)
        except InterventionNotApplicable:
            return False
        return True

    def for_simulation(self, covenant_class: CovenantClass | str) -> InterventionFacts:
        """Return the simulator facts after checking applicability."""

        require_applicable(self._effect, covenant_class)
        return InterventionFacts(code=self._id, effect=self._effect, text=self._text)

    as_intervention = for_simulation

    def retire(self, retired_at: datetime) -> CatalogueEntry:
        """Return a retired copy while preserving the immutable entry."""

        when = _utc_or_none(retired_at, "retired_at")
        if when is None:
            raise ValidationError("retired_at is required when retiring an intervention.")
        return _copy_entry(self, is_active=False, retired_at=when)

    def activate(self) -> CatalogueEntry:
        """Return an active copy with any retirement marker cleared."""

        return _copy_entry(self, is_active=True, retired_at=None)

    def with_database_id(self, database_id: UUID) -> CatalogueEntry:
        """Return a copy associated with a persisted UUID."""

        return _copy_entry(self, database_id=database_id)

    def to_record_values(self) -> dict[str, object]:
        """Return JSON-compatible values for the ORM/seed boundary."""

        parameters = _json_value(_persistence_parameters(self._effect))
        if not isinstance(parameters, dict):
            raise TypeError("Effect parameters must serialize to a JSON object.")
        parameters["_assumptions"] = list(self.assumptions)
        return {
            "code": self._id,
            "role_tag": self._role_tag.value,
            "text": self._text,
            "effect_model": self.effect_model.value,
            "effect_parameters": parameters,
            "applicable_covenant_classes": sorted(self.applicable_covenant_classes),
            "requires_approval": self._requires_approval,
            "is_active": self._is_active,
            "retired_at": self._retired_at.isoformat() if self._retired_at is not None else None,
        }

    @classmethod
    def from_record(cls, row: object) -> CatalogueEntry:
        """Build an entry from an ORM-like intervention record."""

        database_id = getattr(row, "id", None)
        if not isinstance(database_id, UUID):
            raise ValidationError(
                "Intervention record has no valid UUID id.", field="intervention.id"
            )
        raw_parameters = getattr(row, "effect_parameters", None)
        if not isinstance(raw_parameters, Mapping):
            raise ValidationError(
                "Intervention effect_parameters must be a JSON object.",
                field="intervention.effect_parameters",
            )
        parameters = dict(raw_parameters)
        assumptions = parameters.pop("_assumptions", parameters.pop("assumptions", None))
        raw_retired_at = getattr(row, "retired_at", None)
        if isinstance(raw_retired_at, str):
            try:
                raw_retired_at = datetime.fromisoformat(raw_retired_at)
            except ValueError as error:
                raise ValidationError(
                    "Intervention retired_at must be an ISO timestamp.",
                    field="intervention.retired_at",
                ) from error
        return cls(
            id=cast(str | None, getattr(row, "code", None)),
            database_id=database_id,
            role_tag=cast(RoleTag | str | None, getattr(row, "role_tag", None)),
            text=cast(str | None, getattr(row, "text", None)),
            effect_model=cast(EffectModelType | str | None, getattr(row, "effect_model", None)),
            effect_parameters=parameters,
            applicable_covenant_classes=cast(
                Iterable[CovenantClass | str] | None,
                getattr(row, "applicable_covenant_classes", None),
            ),
            assumptions=cast(Iterable[str] | None, assumptions),
            requires_approval=cast(bool, getattr(row, "requires_approval", False)),
            is_active=cast(bool, getattr(row, "is_active", True)),
            retired_at=cast(datetime | None, raw_retired_at),
            version=cast(int, getattr(row, "version", 0)),
        )

    def __repr__(self) -> str:
        return (
            f"CatalogueEntry(id={self._id!r}, role_tag={self._role_tag.value!r}, "
            f"effect_model={self.effect_model.value!r}, is_active={self._is_active!r})"
        )


InterventionCatalogueEntry = CatalogueEntry
ActionCatalogueEntry = CatalogueEntry
Action = CatalogueEntry


def _effect(
    *,
    effect_model: EffectModelType | str | InterventionEffect | None,
    effect_parameters: Mapping[str, object] | None,
    applicable_covenant_classes: Iterable[CovenantClass | str] | None,
    assumptions: Iterable[str] | None,
    supplied_effect: InterventionEffect | None,
) -> InterventionEffect:
    if supplied_effect is None and isinstance(effect_model, InterventionEffect):
        supplied_effect = effect_model
        effect_model = None
    if supplied_effect is not None:
        if not isinstance(supplied_effect, InterventionEffect):
            raise TypeError("effect must be an InterventionEffect instance.")
        if effect_model is not None:
            raise ValidationError(
                "Provide effect or effect_model, not both.", field="intervention.effect_model"
            )
        if applicable_covenant_classes is not None:
            supplied_classes = normalize_covenant_classes(applicable_covenant_classes)
            if supplied_classes != supplied_effect.applicable_covenant_classes:
                raise ValidationError(
                    "Applicable covenant classes do not match the supplied effect.",
                    field="intervention.applicable_covenant_classes",
                )
        if assumptions is not None and tuple(assumptions) != supplied_effect.assumptions:
            raise ValidationError(
                "Assumptions do not match the supplied effect.",
                field="intervention.assumptions",
            )
        return supplied_effect

    if effect_model is None:
        raise ValidationError(
            "An intervention requires an effect model; unsimulatable actions cannot be saved.",
            field="intervention.effect_model",
        )
    if effect_parameters is None:
        parameters: dict[str, object] = {}
    elif isinstance(effect_parameters, Mapping):
        parameters = dict(effect_parameters)
    else:
        raise ValidationError(
            "effect_parameters must be a JSON object.",
            field="intervention.effect_parameters",
        )
    embedded_assumptions = parameters.pop("_assumptions", parameters.pop("assumptions", None))
    normalized_embedded_assumptions = (
        None if embedded_assumptions is None else _assumptions(embedded_assumptions)
    )
    normalized_assumptions: tuple[str, ...] | None = None
    if assumptions is None:
        normalized_assumptions = normalized_embedded_assumptions
    else:
        normalized_assumptions = _assumptions(assumptions)
        if (
            normalized_embedded_assumptions is not None
            and normalized_assumptions != normalized_embedded_assumptions
        ):
            raise ValidationError(
                "Assumptions were supplied twice with different values.",
                field="intervention.assumptions",
            )
    if normalized_assumptions is None:
        raise ValidationError(
            "An intervention effect requires at least one assumption.",
            field="intervention.assumptions",
        )
    if applicable_covenant_classes is None:
        raise ValidationError(
            "An intervention must apply to at least one covenant class.",
            field="intervention.applicable_covenant_classes",
        )
    if not isinstance(effect_model, EffectModelType | str):
        raise ValidationError(
            "effect_model must be one of the supported effect model names.",
            field="intervention.effect_model",
        )
    try:
        normalized_classes = normalize_covenant_classes(applicable_covenant_classes)
        model = EffectModelType.from_value(effect_model)
        if model is EffectModelType.COMBINATION:
            parameters = _combination_parameters(
                parameters,
                applicable_covenant_classes=normalized_classes,
            )
        return build_effect(
            effect_model,
            parameters,
            assumptions=normalized_assumptions,
            applicable_covenant_classes=normalized_classes,
        )
    except ValidationError:
        raise
    except (TypeError, ValueError) as error:
        raise ValidationError(
            f"Invalid intervention effect model: {error}.",
            field="intervention.effect_model",
        ) from error


def _identifier(id_value: str | None, code: str | None) -> str:
    if id_value is not None and code is not None and id_value.strip() != code.strip():
        raise ValidationError(
            "id and code must identify the same intervention.", field="intervention.id"
        )
    value = id_value if id_value is not None else code
    if not isinstance(value, str):
        raise ValidationError("An intervention id is required.", field="intervention.id")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_ID_LENGTH
        or re.fullmatch(_ID_PATTERN, normalized) is None
    ):
        raise ValidationError(
            "intervention.id must be a safe identifier of at most 50 characters.",
            field="intervention.id",
        )
    return normalized


def _assumptions(value: object) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Iterable):
        raise ValidationError(
            "assumptions must be an iterable of statements, not text.",
            field="intervention.assumptions",
        )
    try:
        normalized = tuple(value)
    except TypeError as error:
        raise ValidationError(
            "assumptions must be an iterable of statements.",
            field="intervention.assumptions",
        ) from error
    if not all(isinstance(item, str) for item in normalized):
        raise ValidationError(
            "assumptions must contain only text statements.",
            field="intervention.assumptions",
        )
    if not normalized:
        raise ValidationError(
            "An intervention effect requires at least one assumption.",
            field="intervention.assumptions",
        )
    return cast(tuple[str, ...], normalized)


def _database_id(intervention_id: UUID | str | None, database_id: UUID | None) -> UUID | None:
    if intervention_id is not None and database_id is not None:
        if isinstance(intervention_id, str):
            try:
                intervention_id = UUID(intervention_id)
            except ValueError as error:
                raise ValidationError(
                    "intervention_id must be a UUID.", field="intervention.id"
                ) from error
        if intervention_id != database_id:
            raise ValidationError(
                "intervention_id and database_id must match.", field="intervention.id"
            )
    value = database_id if database_id is not None else intervention_id
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = UUID(value)
        except ValueError as error:
            raise ValidationError(
                "intervention_id must be a UUID.", field="intervention.id"
            ) from error
    if not isinstance(value, UUID):
        raise ValidationError("intervention_id must be a UUID.", field="intervention.id")
    return value


def _role_tag(value: RoleTag | str | None) -> RoleTag:
    if value is None:
        raise ValidationError("role_tag is required.", field="intervention.role_tag")
    try:
        return normalize_role_tag(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(str(error), field="intervention.role_tag") from error


def _text(value: str | None) -> str:
    if not isinstance(value, str):
        raise ValidationError("text is required.", field="intervention.text")
    normalized = value.strip()
    if not normalized:
        raise ValidationError("text must not be blank.", field="intervention.text")
    if len(normalized) > _MAX_TEXT_LENGTH:
        raise ValidationError("text must be at most 2000 characters.", field="intervention.text")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in normalized):
        raise ValidationError(
            "text contains a prohibited control character.", field="intervention.text"
        )
    return normalized


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be boolean.", field=f"intervention.{field}")
    return value


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(
            "version must be a non-negative integer.", field="intervention.version"
        )
    return value


def _utc_or_none(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware.", field=f"intervention.{field}")
    return value.astimezone(UTC)


def _copy_entry(entry: CatalogueEntry, **changes: object) -> CatalogueEntry:
    unknown = set(changes) - {
        "id",
        "database_id",
        "role_tag",
        "text",
        "effect_model",
        "applicable_covenant_classes",
        "assumptions",
        "requires_approval",
        "is_active",
        "retired_at",
        "version",
    }
    if unknown:
        raise TypeError(f"Unknown catalogue copy field {sorted(unknown)[0]!r}.")
    values = entry.to_record_values()
    parameters = cast(dict[str, object], values["effect_parameters"])
    assumptions = parameters.pop("_assumptions")
    return CatalogueEntry(
        id=cast(str, changes.pop("id", entry.id)),
        database_id=cast(UUID | None, changes.pop("database_id", entry.database_id)),
        role_tag=cast(RoleTag, changes.pop("role_tag", entry.role_tag)),
        text=cast(str, changes.pop("text", entry.text)),
        effect_model=cast(EffectModelType, changes.pop("effect_model", entry.effect_model)),
        effect_parameters=parameters,
        applicable_covenant_classes=cast(
            Iterable[str],
            changes.pop("applicable_covenant_classes", entry.applicable_covenant_classes),
        ),
        assumptions=cast(Iterable[str], changes.pop("assumptions", assumptions)),
        requires_approval=cast(bool, changes.pop("requires_approval", entry.requires_approval)),
        is_active=cast(bool, changes.pop("is_active", entry.is_active)),
        retired_at=cast(datetime | None, changes.pop("retired_at", entry.retired_at)),
        version=cast(int, changes.pop("version", entry.version)),
    )


def _json_value(value: object) -> object:
    from decimal import Decimal
    from enum import Enum

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_json_value(item) for item in value), key=str)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Unsupported catalogue value: {type(value).__name__}.")


def _combination_parameters(
    parameters: Mapping[str, object],
    *,
    applicable_covenant_classes: Iterable[CovenantClass | str],
) -> dict[str, object]:
    unknown = set(parameters) - {"components"}
    if unknown:
        raise ValueError(f"combination has unknown parameter {sorted(unknown)[0]!r}.")
    raw_components = parameters.get("components")
    if isinstance(raw_components, str | bytes | bytearray) or not isinstance(
        raw_components, Iterable
    ):
        raise TypeError("combination components must be an iterable of effect definitions.")
    components: list[InterventionEffect] = []
    for index, raw_component in enumerate(raw_components):
        if isinstance(raw_component, InterventionEffect):
            components.append(raw_component)
            continue
        if not isinstance(raw_component, Mapping):
            raise TypeError(f"combination component {index} must be an effect definition.")
        component_model = raw_component.get("effect_model", raw_component.get("model_type"))
        component_parameters = raw_component.get(
            "effect_parameters", raw_component.get("parameters")
        )
        if not isinstance(component_parameters, Mapping):
            raise TypeError(f"combination component {index} parameters must be an object.")
        component_parameters = dict(component_parameters)
        component_assumptions = raw_component.get("assumptions")
        embedded_assumptions = component_parameters.pop(
            "_assumptions", component_parameters.pop("assumptions", None)
        )
        if (
            component_assumptions is not None
            and embedded_assumptions is not None
            and tuple(component_assumptions) != tuple(embedded_assumptions)
        ):
            raise ValueError(f"combination component {index} has conflicting assumptions.")
        if component_assumptions is None:
            component_assumptions = embedded_assumptions
        component_classes = raw_component.get(
            "applicable_covenant_classes", applicable_covenant_classes
        )
        components.append(
            build_effect(
                cast(EffectModelType | str, component_model),
                component_parameters,
                assumptions=cast(Iterable[str] | None, component_assumptions),
                applicable_covenant_classes=cast(
                    Iterable[CovenantClass | str] | None, component_classes
                ),
            )
        )
    return {"components": tuple(components)}


def _persistence_parameters(effect: InterventionEffect) -> Mapping[str, object]:
    """Include component metadata needed to reconstruct combinations."""

    if not isinstance(effect, CombinationEffect):
        return effect.effect_parameters
    return {
        "components": tuple(
            {
                "effect_model": component.model_type.value,
                "effect_parameters": _persistence_parameters(component),
                "assumptions": list(component.assumptions),
                "applicable_covenant_classes": sorted(component.applicable_covenant_classes),
            }
            for component in effect.components
        )
    }


__all__ = [
    "Action",
    "ActionCatalogueEntry",
    "CatalogueEntry",
    "InterventionCatalogueEntry",
    "RoleTag",
    "normalize_role_tag",
]
