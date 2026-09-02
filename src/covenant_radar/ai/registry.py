"""The model registry: registration state, the guard the single call site
consults, and the model-card build check (`plan.md §5.9`'s
`model_registration`, `T-107`).

Persistence-neutral by design — the same shape `security/maker_checker.py`
already uses for the shared approval workflow: the rules in this module
never import SQLAlchemy. `services/model_governance.py` supplies the
database-backed repository, wires the maker-checker approval workflow on
top of it, and is the only writer this module trusts.

`ModelRegistryGuard` is the enforcement half: `ai/client.py`'s single call
site consults it, through an injected instance, before a masked prompt
ever reaches a provider. Two states are refused: no registration at all,
and a registration that has not (or no longer) been approved. Both are
hard failures in production and loud, logged warnings in development, so
the constraint is real without blocking local work that has not registered
its component yet.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

import structlog

from covenant_radar.ai.errors import ModelGovernanceBlocked

logger = structlog.get_logger(__name__)

__all__ = [
    "APPROVED",
    "CHALLENGER",
    "CHAMPION",
    "DEFAULT_MODEL_CARD_DIR",
    "DEVELOPMENT",
    "KNOWN_MODEL_COMPONENTS",
    "MODEL_REGISTRATION_STATES",
    "PRODUCTION",
    "REGISTERED",
    "RETIRED",
    "ComponentNotApproved",
    "ComponentNotRegistered",
    "ModelCardMissingError",
    "ModelRegistrationRecord",
    "ModelRegistryRepository",
    "ModelRegistryGuard",
    "check_model_cards",
]

_COMPONENT_MAX_LENGTH: Final[int] = 100
_PROVIDER_MAX_LENGTH: Final[int] = 50
_MODEL_ID_MAX_LENGTH: Final[int] = 100
_PROMPT_VERSION_MAX_LENGTH: Final[int] = 50
_PURPOSE_MAX_LENGTH: Final[int] = 200
_COMPONENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")

# `db/models/operations.py`'s `_MODEL_REGISTRATION_STATES` CHECK constraint,
# restated here so the persistence-neutral domain layer owns its own closed
# vocabulary rather than importing an ORM module to read it.
REGISTERED: Final[str] = "registered"
APPROVED: Final[str] = "approved"
CHAMPION: Final[str] = "champion"
CHALLENGER: Final[str] = "challenger"
RETIRED: Final[str] = "retired"
MODEL_REGISTRATION_STATES: Final[tuple[str, ...]] = (
    REGISTERED,
    APPROVED,
    CHAMPION,
    CHALLENGER,
    RETIRED,
)
_USABLE_STATES: Final[frozenset[str]] = frozenset({APPROVED, CHAMPION, CHALLENGER})

DEVELOPMENT: Final[str] = "development"
PRODUCTION: Final[str] = "production"
_ENVIRONMENTS: Final[frozenset[str]] = frozenset({DEVELOPMENT, PRODUCTION})

# The only two components that call a model today (`ai/intake.py`'s
# `stage1_extract`, `ai/memo.py`'s `stage7_memo`) — `Stage` in `ai/client.py`
# permits exactly stages 1 and 7 for the same reason. A new model-using
# component is added here deliberately, in the same change that gives it a
# model card, rather than discovered missing at build time.
KNOWN_MODEL_COMPONENTS: Final[tuple[str, ...]] = (
    "stage1_extraction",
    "stage4_forecast_ml",
    "stage7_memo",
)

DEFAULT_MODEL_CARD_DIR: Final[Path] = Path(__file__).resolve().parents[3] / "docs" / "model-cards"


class ComponentNotRegistered(ModelGovernanceBlocked):
    """Raised in production when no registration exists for a component."""


class ComponentNotApproved(ModelGovernanceBlocked):
    """Raised in production when a registered component is not approved for use."""


class ModelCardMissingError(RuntimeError):
    """Raised by the build check when a registered component has no model card."""


@dataclass(frozen=True, slots=True)
class ModelRegistrationRecord:
    """Persistence-neutral shape mapped to the `model_registration` row."""

    id: UUID
    component: str
    provider: str
    model_id: str
    prompt_version: str | None
    purpose: str | None
    owner_id: UUID | None
    evaluation_run_id: UUID | None
    approved_by_id: UUID | None
    approved_at: datetime | None
    state: str
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "component", _validated_component(self.component))
        object.__setattr__(
            self, "provider", _bounded_text(self.provider, "provider", _PROVIDER_MAX_LENGTH)
        )
        object.__setattr__(
            self, "model_id", _bounded_text(self.model_id, "model_id", _MODEL_ID_MAX_LENGTH)
        )
        if self.prompt_version is not None:
            object.__setattr__(
                self,
                "prompt_version",
                _bounded_text(self.prompt_version, "prompt_version", _PROMPT_VERSION_MAX_LENGTH),
            )
        if self.purpose is not None:
            object.__setattr__(
                self, "purpose", _bounded_text(self.purpose, "purpose", _PURPOSE_MAX_LENGTH)
            )
        if self.state not in MODEL_REGISTRATION_STATES:
            raise ValueError(f"Unknown model registration state: {self.state!r}.")
        if (self.approved_by_id is None) != (self.approved_at is None):
            raise ValueError("approved_by_id and approved_at must be set or unset together.")
        if self.state == REGISTERED and self.approved_by_id is not None:
            raise ValueError("A 'registered' record must not carry an approval.")
        if self.state in _USABLE_STATES and self.approved_by_id is None:
            raise ValueError(f"A {self.state!r} record requires an approval.")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("version must be a positive integer.")

    @property
    def is_approved(self) -> bool:
        """Whether this registration is currently usable in production."""
        return self.approved_by_id is not None and self.state in _USABLE_STATES


class ModelRegistryRepository(Protocol):
    """The minimal read port `ModelRegistryGuard` needs.

    `services/model_governance.py`'s SQLAlchemy adapter offers a richer
    surface for registration and approval; this Protocol names only what
    the call-site guard actually depends on, so a guard-only test can
    satisfy it with a two-line fake instead of a full repository.
    """

    def get_by_component(self, component: str) -> ModelRegistrationRecord | None:
        """Return the current registration for `component`, or `None`."""
        ...


class ModelRegistryGuard:
    """The enforcement the single call site (`ai/client.py`) consults.

    `environment` is supplied explicitly by whoever composes the
    application, the same way every other cross-cutting collaborator in
    this codebase is wired — never read from a process-global. A
    development environment never blocks the call; it only logs, loudly,
    so the constraint stays visible without stopping local work on a
    component that has not been registered yet.
    """

    def __init__(self, repository: ModelRegistryRepository, *, environment: str) -> None:
        if environment not in _ENVIRONMENTS:
            raise ValueError(
                f"Unknown deployment environment: {environment!r}; expected one of "
                f"{sorted(_ENVIRONMENTS)}."
            )
        self.repository = repository
        self.environment = environment

    def ensure_permitted(self, component: str) -> ModelRegistrationRecord | None:
        """Return the usable registration for `component`, or refuse/warn.

        Raises `ComponentNotRegistered` or `ComponentNotApproved` in
        production. In development, the same two conditions are logged as a
        warning and the call is allowed to proceed.
        """
        name = _validated_component(component)
        record = self.repository.get_by_component(name)
        if record is None:
            self._deny(
                ComponentNotRegistered,
                f"Component {name!r} is not on the model register; it cannot be used "
                "in production until it is registered and approved.",
                component=name,
                reason="unregistered",
            )
            return None
        if not record.is_approved:
            self._deny(
                ComponentNotApproved,
                f"Component {name!r} is registered but not approved for use "
                f"(state={record.state!r}); it cannot be used in production until "
                "a distinct approver signs off.",
                component=name,
                reason=f"state:{record.state}",
            )
        return record

    def _deny(
        self,
        error_type: type[RuntimeError],
        message: str,
        *,
        component: str,
        reason: str,
    ) -> None:
        if self.environment == PRODUCTION:
            raise error_type(message)
        logger.warning(
            "model_registry_guard_bypassed_in_development",
            component=component,
            reason=reason,
            message=message,
        )


def check_model_cards(
    components: Sequence[str] = KNOWN_MODEL_COMPONENTS,
    *,
    model_card_dir: Path | str = DEFAULT_MODEL_CARD_DIR,
) -> None:
    """Build-check entry point: every named component needs `<component>.md`.

    Raises `ModelCardMissingError`, naming every missing card at once,
    rather than stopping at the first — a build failure should tell an
    engineer everything that is wrong in one pass.
    """
    directory = Path(model_card_dir)
    names = list(dict.fromkeys(_validated_component(component) for component in components))
    missing = sorted(name for name in names if not (directory / f"{name}.md").is_file())
    if missing:
        raise ModelCardMissingError(
            "Missing model card(s) for registered component(s): "
            + ", ".join(missing)
            + f" (expected under {directory})."
        )


def _validated_component(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("component must be a string.")
    name = value.strip()
    if not name or len(name) > _COMPONENT_MAX_LENGTH or _COMPONENT_PATTERN.fullmatch(name) is None:
        raise ValueError(
            "component must be a non-empty identifier of at most "
            f"{_COMPONENT_MAX_LENGTH} characters."
        )
    return name


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    text = value.strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{field} must be a non-empty value of at most {maximum} characters.")
    return text
