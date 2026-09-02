"""Forecast, simulation and triage tables: `plan.md §5.7`'s `forecast_run`,
`forecast`, `forecast_path`, `forecast_driver`, `simulation`,
`intervention` and `triage_entry`.

Every `forecast` and `forecast_path` row belongs to a `forecast_run`, so a
whole day's scoring is reproducible after the fact — `C-03`'s
`GET /api/v1/forecasts/{covenant_ref}/path` **reads `forecast_path`; it
never recomputes and never calls a provider**, exactly as `plan.md §6.1`
requires; that is only possible because the run persisted every day of the
path when it ran.

`simulation.created_by_id` and `forecast.direction` reuse columns and a
domain the mixins and `covenant.py` already define — see each class's
docstring for where.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models._decimal import FractionValue, PercentageValue, RatioValue
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, AwareDateTime, MoneyAmount, PortableJSON

_MODEL_VERSION_MAX_LENGTH = 50
_RUN_STATE_MAX_LENGTH = 20
_DIRECTION_MAX_LENGTH = 4
_DRIVER_NAME_MAX_LENGTH = 100
_INTERVENTION_CODE_MAX_LENGTH = 50
_ROLE_TAG_MAX_LENGTH = 50
_EFFECT_MODEL_MAX_LENGTH = 50
_BAND_MAX_LENGTH = 20
_WHAT_CHANGED_MAX_LENGTH = 2000
_PROBABILITY_SOURCE_MAX_LENGTH = 20
_FALLBACK_REASON_MAX_LENGTH = 500

#: `covenant_version.direction`'s domain (`covenant.py`), repeated here
#: because `forecast.direction` names the same min/max sense for the same
#: covenant and plan.md's own prose treats them as one concept.
_DIRECTIONS: Final[tuple[str, ...]] = ("min", "max")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class ForecastRun(Base, UserAttributedColumns, StandardColumns):
    """One batch scoring pass, as of one date — the unit every `Forecast`,
    `ForecastPath` and `TriageEntry` belongs to (`plan.md §5.7`). Written
    once by the job that ran it, never edited by a person."""

    __tablename__ = "forecast_run"
    __table_args__ = (Index("ix_forecast_run_as_of_date", "as_of_date"),)

    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    job_run_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("job_run.id", ondelete="RESTRICT"), nullable=True
    )
    threshold_snapshot_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("threshold_snapshot.id", ondelete="RESTRICT"), nullable=True
    )
    model_version: Mapped[str | None] = mapped_column(
        String(_MODEL_VERSION_MAX_LENGTH), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    covenant_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(
        String(_RUN_STATE_MAX_LENGTH), nullable=False, default="running"
    )


class Forecast(Base, UserAttributedColumns, StandardColumns):
    """The one place any probability shown anywhere comes from first
    (`plan.md §5.7`): one covenant version's outcome at one horizon,
    within one `ForecastRun`. Written once, never edited by a person."""

    __tablename__ = "forecast"
    __table_args__ = (
        CheckConstraint(f"direction IN ({_sql_in_list(_DIRECTIONS)})", name="direction_valid"),
        UniqueConstraint(
            "run_id",
            "covenant_version_id",
            "horizon_days",
            name="uq_forecast_run_covenant_version_horizon",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("forecast_run.id", ondelete="RESTRICT"), nullable=False
    )
    covenant_version_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("covenant_version.id", ondelete="RESTRICT"), nullable=False
    )
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    probability: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)
    # `server_default` mirrors migration 0007, which added the column with one.
    # Without it here, `alembic check` reports drift on every run and a real
    # schema difference would be lost in that standing noise.
    probability_source: Mapped[str] = mapped_column(
        String(_PROBABILITY_SOURCE_MAX_LENGTH),
        nullable=False,
        default="deterministic",
        server_default="deterministic",
    )
    fallback_reason: Mapped[str | None] = mapped_column(String(_FALLBACK_REASON_MAX_LENGTH))
    confidence: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)
    below_confidence_floor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    projected_cross_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(_DIRECTION_MAX_LENGTH), nullable=True)
    formula_inputs: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    data_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    staleness_days: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ForecastPath(Base, UserAttributedColumns, StandardColumns):
    """One day of one `Forecast`'s projected path — what the horizon
    control reads and never recomputes (`plan.md §5.7`, `C-03`)."""

    __tablename__ = "forecast_path"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "covenant_version_id",
            "day_offset",
            name="uq_forecast_path_run_covenant_version_day",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("forecast_run.id", ondelete="RESTRICT"), nullable=False
    )
    covenant_version_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("covenant_version.id", ondelete="RESTRICT"), nullable=False
    )
    day_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    projected_value: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    headroom_pct: Mapped[Decimal | None] = mapped_column(PercentageValue(), nullable=True)


class ForecastDriver(Base, UserAttributedColumns, StandardColumns):
    """One attributed driver behind a `Forecast`'s outcome, `C-38`'s
    normalised shares persisted (`plan.md §5.7`)."""

    __tablename__ = "forecast_driver"

    forecast_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("forecast.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(_DRIVER_NAME_MAX_LENGTH), nullable=False)
    share: Mapped[Decimal] = mapped_column(FractionValue(), nullable=False)
    evidence_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("evidence_item.id", ondelete="RESTRICT"), nullable=True
    )
    is_other: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Simulation(Base, UserAttributedColumns, StandardColumns):
    """A persisted "what if" against one `Forecast` — persisted, not
    transient, because a memo can cite it later (`plan.md §5.7`).

    `created_by_id` is `plan.md §5.7`'s own listed field for this table;
    it is not redeclared here because `UserAttributedColumns` (mixed in
    below) already supplies exactly that column, foreign-keyed to
    `app_user.id`.
    """

    __tablename__ = "simulation"

    forecast_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("forecast.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    intervention_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("intervention.id", ondelete="RESTRICT"), nullable=False
    )
    parameters: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    assumptions: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)
    projected_cross_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    probability: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)
    delta_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delta_probability: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)


class Intervention(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One playbook entry a simulation or an action taken can cite
    (`plan.md §5.7`). Retired, never deleted — `retired_at` set — so a
    historical memo's citation still resolves."""

    __tablename__ = "intervention"

    code: Mapped[str] = mapped_column(
        String(_INTERVENTION_CODE_MAX_LENGTH), nullable=False, unique=True
    )
    role_tag: Mapped[str | None] = mapped_column(String(_ROLE_TAG_MAX_LENGTH), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    effect_model: Mapped[str] = mapped_column(String(_EFFECT_MODEL_MAX_LENGTH), nullable=False)
    effect_parameters: Mapped[dict[str, object] | None] = mapped_column(
        PortableJSON, nullable=True
    )
    applicable_covenant_classes: Mapped[list[str] | None] = mapped_column(
        PortableJSON, nullable=True
    )
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retired_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)


class TriageEntry(Base, UserAttributedColumns, StandardColumns):
    """One borrower's worst outcome within one `ForecastRun` — the queue
    is a read of the latest run's rows, never a live recomputation
    (`plan.md §5.7`). Written once by the job that ran the triage pass."""

    __tablename__ = "triage_entry"
    __table_args__ = (
        Index("ix_triage_entry_run_id_rank", "run_id", "rank"),
        Index("ix_triage_entry_run_id_band", "run_id", "band"),
    )

    run_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("forecast_run.id", ondelete="RESTRICT"), nullable=False
    )
    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="RESTRICT"), nullable=False
    )
    worst_covenant_version_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("covenant_version.id", ondelete="RESTRICT"), nullable=True
    )
    worst_horizon: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probability: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)
    exposure: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    urgency: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    band: Mapped[str | None] = mapped_column(String(_BAND_MAX_LENGTH), nullable=True)
    sma_band: Mapped[str | None] = mapped_column(String(_BAND_MAX_LENGTH), nullable=True)
    what_changed: Mapped[str | None] = mapped_column(
        String(_WHAT_CHANGED_MAX_LENGTH), nullable=True
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
