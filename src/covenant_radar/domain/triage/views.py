"""Pure value objects for the portfolio queue and saved views.

The queue is a read model, but its filters and page shape are part of the
application contract.  Keeping those values here prevents SQLAlchemy rows,
HTTP query parameters, and templates from developing slightly different
interpretations of the same filter.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, cast
from uuid import UUID

from covenant_radar.domain.signals import FAMILIES

QUEUE_EMPTY_MESSAGE: Final[str] = (
    "No borrowers in scope — import a portfolio or ask an administrator for access."
)
QUEUE_EMPTY_REASON: Final[str] = "no_complete_run"
RELOAD_REQUIRED_MESSAGE: Final[str] = (
    "This queue page is no longer current; reload the queue before continuing."
)

_BANDS: Final[frozenset[str]] = frozenset({"act", "amber", "watch"})
_CASE_STATES: Final[frozenset[str]] = frozenset(
    {"open", "in_progress", "monitoring", "escalated", "closed", "none"}
)
_SMA_BANDS: Final[frozenset[str]] = frozenset({"none", "SMA-0", "SMA-1", "SMA-2", "beyond"})
_SIGNAL_FAMILIES: Final[frozenset[str]] = frozenset(FAMILIES)
_MAX_FILTER_TEXT: Final[int] = 200
_MAX_VIEW_NAME: Final[int] = 100
_MISSING: Final[object] = object()


@dataclass(frozen=True, slots=True, init=False)
class QueueFilters:
    """Validated structured filters accepted by the queue read path.

    The canonical names mirror the queue contract.  Persistence and web
    callers may use the explicit aliases (``portfolio_id``,
    ``industry_code``, ``assignee_id`` and ``case_status``); aliases are
    normalised once and cannot disagree with their canonical counterpart.
    Unknown industry or portfolio values remain valid filters and therefore
    produce an empty result when no row has that value.
    """

    band: str | None
    portfolio: UUID | str | None
    industry: str | None
    assignee: UUID | None
    sma_band: str | None
    case_state: str | None
    signal_family: str | None

    def __init__(
        self,
        band: str | None = None,
        portfolio: UUID | str | None = None,
        industry: str | None = None,
        assignee: UUID | str | None = None,
        sma_band: str | None = None,
        case_state: str | None = None,
        signal_family: str | None = None,
        *,
        portfolio_id: UUID | str | None = None,
        industry_code: str | None = None,
        assignee_id: UUID | str | None = None,
        case_status: str | None = None,
    ) -> None:
        normalized_band = _optional_choice(band, "band", _BANDS, lower=True)
        normalized_portfolio = _coalesce_alias(
            portfolio,
            portfolio_id,
            "portfolio",
            normalizer=_portfolio_value,
        )
        normalized_industry = _coalesce_alias(
            industry,
            industry_code,
            "industry",
            normalizer=lambda value: _optional_text(value, "industry"),
        )
        normalized_assignee = _coalesce_alias(
            assignee,
            assignee_id,
            "assignee",
            normalizer=lambda value: _optional_uuid(value, "assignee"),
        )
        normalized_sma = _optional_sma(sma_band)
        normalized_case_state = _coalesce_alias(
            case_state,
            case_status,
            "case_state",
            normalizer=lambda value: _optional_choice(
                value,
                "case_state",
                _CASE_STATES,
                lower=True,
            ),
        )
        normalized_signal_family = _optional_choice(
            signal_family, "signal_family", _SIGNAL_FAMILIES, lower=True
        )
        object.__setattr__(self, "band", normalized_band)
        object.__setattr__(self, "portfolio", normalized_portfolio)
        object.__setattr__(self, "industry", normalized_industry)
        object.__setattr__(self, "assignee", normalized_assignee)
        object.__setattr__(self, "sma_band", normalized_sma)
        object.__setattr__(self, "case_state", normalized_case_state)
        object.__setattr__(self, "signal_family", normalized_signal_family)

    @classmethod
    def from_value(cls, value: QueueFilters | Mapping[str, object] | object | None) -> QueueFilters:
        """Normalise a queue filter object or its JSON-compatible mapping."""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls(
                band=cast(str | None, _read(value, "band", default=None)),
                portfolio=cast(
                    UUID | str | None,
                    _read(value, "portfolio", "portfolio_id", default=None),
                ),
                industry=cast(
                    str | None,
                    _read(value, "industry", "industry_code", default=None),
                ),
                assignee=cast(
                    UUID | str | None,
                    _read(value, "assignee", "assignee_id", default=None),
                ),
                sma_band=cast(str | None, _read(value, "sma_band", default=None)),
                case_state=cast(
                    str | None,
                    _read(value, "case_state", "case_status", default=None),
                ),
                signal_family=cast(str | None, _read(value, "signal_family", default=None)),
            )

        allowed = {
            "band",
            "portfolio",
            "portfolio_id",
            "industry",
            "industry_code",
            "assignee",
            "assignee_id",
            "sma_band",
            "case_state",
            "case_status",
            "signal_family",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"Unknown queue filter field {unknown[0]!r}.")
        return cls(
            band=cast(str | None, value.get("band")),
            portfolio=cast(UUID | str | None, value.get("portfolio")),
            portfolio_id=cast(UUID | str | None, value.get("portfolio_id")),
            industry=cast(str | None, value.get("industry")),
            industry_code=cast(str | None, value.get("industry_code")),
            assignee=cast(UUID | str | None, value.get("assignee")),
            assignee_id=cast(UUID | str | None, value.get("assignee_id")),
            sma_band=cast(str | None, value.get("sma_band")),
            case_state=cast(str | None, value.get("case_state")),
            case_status=cast(str | None, value.get("case_status")),
            signal_family=cast(str | None, value.get("signal_family")),
        )

    @property
    def portfolio_id(self) -> UUID | None:
        """Return the portfolio UUID when the filter names one directly."""
        return self.portfolio if isinstance(self.portfolio, UUID) else None

    @property
    def portfolio_value(self) -> UUID | str | None:
        """Compatibility-facing name for the portfolio code/path/UUID."""
        return self.portfolio

    @property
    def industry_code(self) -> str | None:
        return self.industry

    @property
    def assignee_id(self) -> UUID | None:
        return self.assignee

    @property
    def case_status(self) -> str | None:
        return self.case_state

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-compatible representation."""
        return {
            "band": self.band,
            "portfolio": _json_value(self.portfolio),
            "industry": self.industry,
            "assignee": _json_value(self.assignee),
            "sma_band": self.sma_band,
            "case_state": self.case_state,
            "signal_family": self.signal_family,
        }


@dataclass(frozen=True, slots=True)
class SavedView:
    """A named, immutable queue filter set suitable for persistence."""

    name: str
    filters: QueueFilters

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _bounded_text(self.name, "name", _MAX_VIEW_NAME))
        object.__setattr__(self, "filters", QueueFilters.from_value(self.filters))

    @classmethod
    def from_value(cls, value: SavedView | Mapping[str, object] | object) -> SavedView:
        """Build a saved view from an object or a persisted mapping."""
        if isinstance(value, cls):
            return value
        name = _read(value, "name", default=_MISSING)
        filters = _read(value, "filters", "filter_set", default=_MISSING)
        if name is _MISSING or filters is _MISSING:
            raise ValueError("A saved view requires a name and filters.")
        return cls(name=cast(str, name), filters=QueueFilters.from_value(filters))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SavedView:
        """Deserialize a saved view while rejecting unexpected fields."""
        if not isinstance(value, Mapping):
            raise TypeError("A saved view must be deserialized from a mapping.")
        unknown = sorted(set(value) - {"name", "filters"})
        if unknown:
            raise ValueError(f"Unknown saved view field {unknown[0]!r}.")
        return cls.from_value(value)

    @classmethod
    def from_json(cls, value: str) -> SavedView:
        """Deserialize one saved view from JSON."""
        if not isinstance(value, str):
            raise TypeError("A saved view JSON payload must be text.")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("A saved view JSON payload is malformed.") from error
        return cls.from_dict(decoded)

    def to_dict(self) -> dict[str, object]:
        """Serialize the view without leaking implementation details."""
        return {"name": self.name, "filters": self.filters.to_dict()}

    def to_json(self) -> str:
        """Serialize deterministically for storage and content hashing."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SavedQueueView = SavedView


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """One scoped row returned by the queue read model."""

    triage_entry_id: UUID
    run_id: UUID
    borrower_id: UUID
    borrower_reference: str
    legal_name: str
    portfolio_id: UUID
    portfolio_code: str
    portfolio_path: str
    industry_code: str | None
    worst_covenant_version_id: UUID | None
    worst_horizon: int | None
    probability: Decimal | None
    confidence: Decimal | None
    exposure: Decimal | None
    urgency: Decimal | None
    band: str
    sma_band: str | None
    what_changed: str | None
    rank: int
    case_state: str | None
    assignee_id: UUID | None
    case_reference: str | None = None

    @property
    def id(self) -> UUID:
        return self.triage_entry_id

    @property
    def borrower_ref(self) -> str:
        return self.borrower_reference

    @property
    def case_status(self) -> str | None:
        return self.case_state

    @property
    def portfolio(self) -> UUID:
        return self.portfolio_id


@dataclass(frozen=True, slots=True)
class QueueEmptyState:
    """Reason and next step for a queue with no rows."""

    reason: str
    message: str


@dataclass(frozen=True, slots=True)
class QueueSummary:
    """Aggregate facts for the scoped latest queue run.

    A page is allowed to be seek-paginated, while a portfolio snapshot must
    not change when the first page size or cursor changes.
    """

    total: int
    act: int
    amber: int
    watch: int
    what_changed: int
    exposure_total: Decimal | None

    def __post_init__(self) -> None:
        counts = (self.total, self.act, self.amber, self.watch, self.what_changed)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
            raise TypeError("Queue summary counts must be integers.")
        if any(value < 0 for value in counts):
            raise ValueError("Queue summary counts cannot be negative.")
        if self.act + self.amber + self.watch > self.total:
            raise ValueError("Queue band counts cannot exceed the total row count.")
        if self.exposure_total is not None and (
            not isinstance(self.exposure_total, Decimal) or not self.exposure_total.is_finite()
        ):
            raise ValueError("Queue summary exposure must be a finite Decimal or None.")


@dataclass(frozen=True, slots=True)
class QueuePage:
    """A stable page of queue rows and its run-bound continuation cursor."""

    run_id: UUID | None
    as_of_date: date | None
    entries: tuple[QueueEntry, ...]
    next_cursor: str | None = None
    empty_state: QueueEmptyState | None = None

    def __post_init__(self) -> None:
        if self.run_id is not None and not isinstance(self.run_id, UUID):
            raise TypeError("QueuePage.run_id must be a UUID or None.")
        if self.as_of_date is not None and (
            isinstance(self.as_of_date, datetime) or not isinstance(self.as_of_date, date)
        ):
            raise TypeError("QueuePage.as_of_date must be a calendar date or None.")
        object.__setattr__(self, "entries", tuple(self.entries))
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor
        ):
            raise ValueError("QueuePage.next_cursor must be non-empty text or None.")

    @classmethod
    def no_complete_run(cls) -> QueuePage:
        """Return the documented first-use empty state."""
        return cls(
            run_id=None,
            as_of_date=None,
            entries=(),
            empty_state=QueueEmptyState(QUEUE_EMPTY_REASON, QUEUE_EMPTY_MESSAGE),
        )

    @property
    def items(self) -> tuple[QueueEntry, ...]:
        return self.entries

    @property
    def rows(self) -> tuple[QueueEntry, ...]:
        return self.entries

    @property
    def cursor(self) -> str | None:
        return self.next_cursor

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def state(self) -> str:
        return "empty" if self.is_empty else "ready"

    @property
    def message(self) -> str | None:
        return None if self.empty_state is None else self.empty_state.message

    @property
    def reason(self) -> str | None:
        return None if self.empty_state is None else self.empty_state.reason


def _coalesce_alias(
    primary: object,
    alias: object,
    field_name: str,
    *,
    normalizer: Callable[[object], object],
) -> object:
    values = [value for value in (primary, alias) if value is not None]
    if len(values) > 1:
        first = normalizer(values[0])
        second = normalizer(values[1])
        if first != second:
            raise ValueError(f"{field_name} and its alias must identify the same value.")
        return first
    if not values:
        return None
    return normalizer(values[0])


def _optional_choice(
    value: object,
    field_name: str,
    choices: frozenset[str],
    *,
    lower: bool = False,
) -> str | None:
    if value is None:
        return None
    normalized = _optional_text(value, field_name)
    if lower:
        normalized = normalized.lower()
    if normalized not in choices:
        rendered = ", ".join(sorted(choices))
        raise ValueError(f"{field_name} must be one of {rendered}.")
    return normalized


def _optional_sma(value: object) -> str | None:
    if value is None:
        return None
    normalized = _optional_text(value, "sma_band")
    lowered = normalized.lower()
    canonical = lowered if lowered in {"none", "beyond"} else normalized.upper()
    if canonical not in _SMA_BANDS:
        rendered = ", ".join(sorted(_SMA_BANDS))
        raise ValueError(f"sma_band must be one of {rendered}.")
    return canonical


def _portfolio_value(value: object) -> UUID | str:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= _MAX_FILTER_TEXT:
        raise ValueError("portfolio must be a UUID, code, or path of at most 200 characters.")
    text = value.strip()
    try:
        return UUID(text)
    except ValueError:
        return text


def _optional_uuid(value: object, field_name: str) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a UUID or None.")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid UUID.") from error


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name, _MAX_FILTER_TEXT)


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{field_name} must be non-empty text of at most {maximum} characters.")
    return value.strip()


def _read(value: object, *names: str, default: object) -> object:
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


def _json_value(value: object) -> object:
    return str(value) if isinstance(value, UUID) else value


__all__ = [
    "QUEUE_EMPTY_MESSAGE",
    "QUEUE_EMPTY_REASON",
    "RELOAD_REQUIRED_MESSAGE",
    "QueueEmptyState",
    "QueueEntry",
    "QueueFilters",
    "QueuePage",
    "QueueSummary",
    "SavedQueueView",
    "SavedView",
]
