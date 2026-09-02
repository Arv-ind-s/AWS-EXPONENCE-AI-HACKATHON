"""Atomic model-call ceilings and spend accounting.

The provider call site reserves capacity before it invokes a provider.  This
module deliberately knows nothing about HTTP or model prompts: it only owns
the T7 usage policy and the small persistence-neutral seam needed to replace
the process-local ledger with a shared store when the application is deployed
with multiple workers.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID, uuid4

from covenant_radar.core.clock import Clock, SystemClock

_DEFAULT_CURRENCY: Final[str] = "INR"
_HOUR: Final[timedelta] = timedelta(hours=1)
_DAY: Final[timedelta] = timedelta(days=1)


class CeilingReached(RuntimeError):
    """A model-call ceiling is engaged and the call must be queued.

    The exception carries no prompt, provider payload or customer data.  A
    caller can safely expose ``dimension`` and ``retry_at`` in a banner and
    alert without accidentally disclosing model input.
    """

    def __init__(
        self,
        dimension: str,
        *,
        retry_at: datetime | None,
        limit: int | Decimal,
        observed: int | Decimal,
        currency: str | None = None,
    ) -> None:
        if not dimension or not isinstance(dimension, str):
            raise ValueError("A ceiling dimension is required.")
        if retry_at is not None:
            retry_at = _as_utc(retry_at)
        self.dimension = dimension
        self.ceiling = dimension
        self.retry_at = retry_at
        self.limit = limit
        self.observed = observed
        self.currency = currency
        suffix = f"; retry after {retry_at.isoformat()}" if retry_at is not None else ""
        super().__init__(f"Model-call {dimension} ceiling reached; request queued{suffix}.")


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Validated T7 limits used by :class:`BudgetLedger`."""

    calls_per_hour: int
    calls_per_day: int
    monthly_budget: Decimal | None = None
    currency: str = _DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        for name in ("calls_per_hour", "calls_per_day"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.calls_per_hour > self.calls_per_day:
            raise ValueError("calls_per_hour must not exceed calls_per_day.")

        budget = self.monthly_budget
        if budget is not None:
            budget = _decimal(budget, "monthly_budget")
            if budget <= 0:
                raise ValueError("monthly_budget must be positive when configured.")
            object.__setattr__(self, "monthly_budget", budget)
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise ValueError("currency must be a three-letter code.")
        object.__setattr__(self, "currency", self.currency.upper())

    @classmethod
    def from_t7(
        cls,
        values: Mapping[str, object] | None = None,
        *,
        currency: str = _DEFAULT_CURRENCY,
    ) -> BudgetLimits:
        """Build limits from the validated T7 mapping.

        With no mapping, the packaged threshold store is loaded lazily.  The
        lazy import keeps this low-level ledger usable in isolated tests and
        prevents a module import from reading configuration unexpectedly.
        """

        if values is None:
            from covenant_radar.config.thresholds import ThresholdStore

            values = ThresholdStore().get("T7")
        elif "T7" in values and isinstance(values["T7"], Mapping):
            values = values["T7"]
        try:
            calls_per_hour = values["calls_per_hour"]
            calls_per_day = values["calls_per_day"]
            monthly_budget = values.get("monthly_budget")
        except (KeyError, AttributeError, TypeError) as error:
            raise ValueError("T7 budget values must contain the model-call ceilings.") from error
        return cls(
            calls_per_hour=_positive_int(calls_per_hour, "calls_per_hour"),
            calls_per_day=_positive_int(calls_per_day, "calls_per_day"),
            monthly_budget=(
                None if monthly_budget is None else _decimal(monthly_budget, "monthly_budget")
            ),
            currency=currency,
        )


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """An atomic reservation for one outbound model attempt."""

    id: UUID
    reserved_at: datetime
    estimated_cost: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """Read-only usage totals for health and administrative views."""

    calls_last_hour: int
    calls_last_day: int
    spend_this_month: Decimal
    currency: str


@dataclass(slots=True)
class _UsageEntry:
    id: UUID
    occurred_at: datetime
    cost: Decimal


class BudgetLedger:
    """Thread-safe rolling-window usage ledger.

    Reservations and limit checks happen under one lock, so concurrent web
    requests cannot pass the same remaining capacity.  The ledger is an
    injectable process-local implementation of the budget seam; deployments
    that run multiple workers should provide a shared reservation store to the
    call site.  It never silently falls back to an unsafe, unbounded mode.
    """

    def __init__(
        self,
        limits: BudgetLimits | Mapping[str, object] | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        if limits is None:
            validated_limits = BudgetLimits.from_t7()
        elif isinstance(limits, BudgetLimits):
            validated_limits = limits
        elif isinstance(limits, Mapping):
            validated_limits = BudgetLimits.from_t7(limits)
        else:
            raise TypeError("limits must be BudgetLimits, a T7 mapping, or None.")
        self.limits = validated_limits
        self._clock = clock or SystemClock()
        self._entries: deque[_UsageEntry] = deque()
        self._lock = threading.RLock()

    def reserve(
        self,
        *,
        at: datetime | None = None,
        estimated_cost: Decimal | int | str = Decimal(0),
    ) -> BudgetReservation:
        """Reserve one model attempt or raise :class:`CeilingReached`.

        The reservation remains a call-count entry even if the provider later
        fails: a request was still made and must count against the rate
        ceiling.  Its monetary amount is settled separately after a response.
        """

        instant = _as_utc(at or self._clock.now())
        cost = _nonnegative_decimal(estimated_cost, "estimated_cost")
        with self._lock:
            self._prune(instant)
            hour_entries = self._entries_in_window(instant, _HOUR)
            day_entries = self._entries_in_window(instant, _DAY)
            if len(hour_entries) >= self.limits.calls_per_hour:
                retry_at = min(item.occurred_at for item in hour_entries) + _HOUR
                raise CeilingReached(
                    "hourly",
                    retry_at=retry_at,
                    limit=self.limits.calls_per_hour,
                    observed=len(hour_entries),
                )
            if len(day_entries) >= self.limits.calls_per_day:
                retry_at = min(item.occurred_at for item in day_entries) + _DAY
                raise CeilingReached(
                    "daily",
                    retry_at=retry_at,
                    limit=self.limits.calls_per_day,
                    observed=len(day_entries),
                )

            month_spend = sum(
                (item.cost for item in self._entries if _same_month(item.occurred_at, instant)),
                Decimal(0),
            )
            monthly_budget = self.limits.monthly_budget
            if monthly_budget is not None and month_spend + cost > monthly_budget:
                raise CeilingReached(
                    "budget",
                    retry_at=_next_month(instant),
                    limit=monthly_budget,
                    observed=month_spend + cost,
                    currency=self.limits.currency,
                )

            reservation = BudgetReservation(
                id=uuid4(),
                reserved_at=instant,
                estimated_cost=cost,
                currency=self.limits.currency,
            )
            self._entries.append(_UsageEntry(reservation.id, instant, cost))
            return reservation

    def settle(
        self,
        reservation: BudgetReservation,
        actual_cost: Decimal | int | str | None,
    ) -> Decimal:
        """Replace a reservation's estimate with the observed provider cost."""

        if not isinstance(reservation, BudgetReservation):
            raise TypeError("settle requires a BudgetReservation.")
        cost = (
            reservation.estimated_cost
            if actual_cost is None
            else _nonnegative_decimal(actual_cost, "actual_cost")
        )
        with self._lock:
            for entry in self._entries:
                if entry.id == reservation.id:
                    entry.cost = cost
                    return cost
        raise ValueError("The budget reservation is unknown or has already been discarded.")

    def usage(self, *, at: datetime | None = None) -> BudgetUsage:
        """Return usage totals for the current rolling windows."""

        instant = _as_utc(at or self._clock.now())
        with self._lock:
            self._prune(instant)
            return BudgetUsage(
                calls_last_hour=len(self._entries_in_window(instant, _HOUR)),
                calls_last_day=len(self._entries_in_window(instant, _DAY)),
                spend_this_month=sum(
                    (item.cost for item in self._entries if _same_month(item.occurred_at, instant)),
                    Decimal(0),
                ),
                currency=self.limits.currency,
            )

    def _entries_in_window(self, instant: datetime, window: timedelta) -> list[_UsageEntry]:
        boundary = instant - window
        return [item for item in self._entries if item.occurred_at > boundary]

    def _prune(self, instant: datetime) -> None:
        # Rate windows are rolling, while the monetary ceiling is for the
        # calendar month.  Retaining the current month's entries is essential:
        # pruning at one day would silently make an old spend disappear.
        month_start = instant.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while self._entries and self._entries[0].occurred_at < month_start:
            self._entries.popleft()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Budget timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _same_month(left: datetime, right: datetime) -> bool:
    return left.year == right.year and left.month == right.month


def _next_month(value: datetime) -> datetime:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return value.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal value.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a decimal value.") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be finite.")
    return result


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if result < 0:
        raise ValueError(f"{name} must not be negative.")
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


CallBudget = BudgetLedger
Budget = BudgetLedger
BudgetManager = BudgetLedger
UsageLedger = BudgetLedger


__all__ = [
    "Budget",
    "BudgetLedger",
    "BudgetLimits",
    "BudgetManager",
    "BudgetReservation",
    "BudgetUsage",
    "CallBudget",
    "CeilingReached",
    "UsageLedger",
]
