"""Operations tables: `plan.md §5.9`'s `model_call`, `model_registration`,
`drift_observation`, `job_run`, `connector`/`connector_run`,
`feed_source`/`entity_match`, `retention_purge_log` and `evaluation_run`.

`plan.md §5.9`'s prose gives `connector`/`connector_run` and
`feed_source`/`entity_match` only their purpose ("configuration and
per-run reconciliation totals, lag, rejects", "feed configuration; match
candidate, confidence, decision, `is_negative`"), not a "Key fields" row
the way every other table in `§5` gets. This module's column choices for
those four tables are this task's own reasonable design against that
prose — kept deliberately generic (a JSON `config` blob rather than named
per-source-system fields) so the ingestion tasks that actually build
connectors (`plan.md`'s later milestones) can extend them without a
migration that fights this shape.

`request_id` is `plan.md §5.9`'s own listed field for `model_call`; it is
not redeclared here because `StandardColumns` already supplies it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models._decimal import FractionValue, RatioValue
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID, AwareDateTime, MoneyAmount, PortableJSON

_STAGE_MAX_LENGTH = 50
_PROVIDER_MAX_LENGTH = 50
_MODEL_VERSION_MAX_LENGTH = 50
_PROMPT_VERSION_MAX_LENGTH = 50
_CURRENCY_MAX_LENGTH = 3
_CHECK_VERDICT_MAX_LENGTH = 50
_REFUSAL_REASON_MAX_LENGTH = 2000
_COMPONENT_MAX_LENGTH = 100
_MODEL_ID_MAX_LENGTH = 100
_PURPOSE_MAX_LENGTH = 200
_REGISTRATION_STATE_MAX_LENGTH = 20
_METRIC_MAX_LENGTH = 100
_JOB_NAME_MAX_LENGTH = 100
_RUN_ID_MAX_LENGTH = 64
_TRIGGER_MAX_LENGTH = 20
_JOB_STATE_MAX_LENGTH = 20
_ERROR_MAX_LENGTH = 4000
_NAME_MAX_LENGTH = 100
_CONNECTOR_TYPE_MAX_LENGTH = 50
_CONNECTOR_RUN_STATE_MAX_LENGTH = 20
_FEED_TYPE_MAX_LENGTH = 50
_SUBJECT_TYPE_MAX_LENGTH = 50
_EXTERNAL_REFERENCE_MAX_LENGTH = 200
_DECISION_MAX_LENGTH = 20
_ENTITY_MAX_LENGTH = 100
_EXECUTED_BY_MAX_LENGTH = 200
_COMMIT_SHA_MAX_LENGTH = 40
_ARM_MAX_LENGTH = 50

_MODEL_REGISTRATION_STATES: Final[tuple[str, ...]] = (
    "registered",
    "approved",
    "champion",
    "challenger",
    "retired",
)


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class ModelCall(Base, UserAttributedColumns, StandardColumns):
    """One call out to an AI provider, with its cost, latency and shape
    check outcome (`plan.md §5.9`). Written once by the call site, never
    edited by a person."""

    __tablename__ = "model_call"

    stage: Mapped[str] = mapped_column(String(_STAGE_MAX_LENGTH), nullable=False)
    provider: Mapped[str] = mapped_column(String(_PROVIDER_MAX_LENGTH), nullable=False)
    model_version: Mapped[str] = mapped_column(
        String(_MODEL_VERSION_MAX_LENGTH), nullable=False
    )
    prompt_version: Mapped[str | None] = mapped_column(
        String(_PROMPT_VERSION_MAX_LENGTH), nullable=True
    )
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(_CURRENCY_MAX_LENGTH), nullable=True)
    check_verdict: Mapped[str | None] = mapped_column(
        String(_CHECK_VERDICT_MAX_LENGTH), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refusal_reason: Mapped[str | None] = mapped_column(
        String(_REFUSAL_REASON_MAX_LENGTH), nullable=True
    )
    from_cassette: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ModelRegistration(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One AI model registered for one purpose in the product, tracked
    from registration through champion/challenger to retirement
    (`plan.md §5.9`)."""

    __tablename__ = "model_registration"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({_sql_in_list(_MODEL_REGISTRATION_STATES)})", name="state_valid"
        ),
    )

    component: Mapped[str] = mapped_column(String(_COMPONENT_MAX_LENGTH), nullable=False)
    provider: Mapped[str] = mapped_column(String(_PROVIDER_MAX_LENGTH), nullable=False)
    model_id: Mapped[str] = mapped_column(String(_MODEL_ID_MAX_LENGTH), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(
        String(_PROMPT_VERSION_MAX_LENGTH), nullable=True
    )
    purpose: Mapped[str | None] = mapped_column(String(_PURPOSE_MAX_LENGTH), nullable=True)
    owner_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    evaluation_run_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("evaluation_run.id", ondelete="RESTRICT"), nullable=True
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    state: Mapped[str] = mapped_column(
        String(_REGISTRATION_STATE_MAX_LENGTH), nullable=False, default="registered"
    )


class DriftObservation(Base, UserAttributedColumns, StandardColumns):
    """One measured drift metric for one monitored component, against its
    baseline (`plan.md §5.9`). Written once by the monitoring job."""

    __tablename__ = "drift_observation"

    component: Mapped[str] = mapped_column(String(_COMPONENT_MAX_LENGTH), nullable=False)
    metric: Mapped[str] = mapped_column(String(_METRIC_MAX_LENGTH), nullable=False)
    window_start: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    value: Mapped[Decimal] = mapped_column(RatioValue(), nullable=False)
    baseline: Mapped[Decimal] = mapped_column(RatioValue(), nullable=False)
    breached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class JobRun(Base, UserAttributedColumns, StandardColumns):
    """One attempt of one scheduled or triggered job — the batch ledger
    `R-28` resumes from (`plan.md §5.9`)."""

    __tablename__ = "job_run"
    __table_args__ = (Index("ix_job_run_run_id", "run_id"),)

    job_name: Mapped[str] = mapped_column(String(_JOB_NAME_MAX_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(_RUN_ID_MAX_LENGTH), nullable=False)
    trigger: Mapped[str] = mapped_column(String(_TRIGGER_MAX_LENGTH), nullable=False)
    started_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    state: Mapped[str] = mapped_column(
        String(_JOB_STATE_MAX_LENGTH), nullable=False, default="running"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error: Mapped[str | None] = mapped_column(String(_ERROR_MAX_LENGTH), nullable=True)
    metrics: Mapped[dict[str, object] | None] = mapped_column(PortableJSON, nullable=True)


class Connector(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One configured upstream source-system connection (`plan.md
    §5.9`)."""

    __tablename__ = "connector"

    name: Mapped[str] = mapped_column(String(_NAME_MAX_LENGTH), nullable=False, unique=True)
    connector_type: Mapped[str] = mapped_column(
        String(_CONNECTOR_TYPE_MAX_LENGTH), nullable=False
    )
    config: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ConnectorRun(Base, UserAttributedColumns, StandardColumns):
    """One reconciliation pass of one `Connector` — its totals, lag and
    rejects (`plan.md §5.9`). Written once by the run itself."""

    __tablename__ = "connector_run"

    connector_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("connector.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    state: Mapped[str] = mapped_column(
        String(_CONNECTOR_RUN_STATE_MAX_LENGTH), nullable=False, default="running"
    )
    record_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reconciled_total: Mapped[Decimal | None] = mapped_column(RatioValue(), nullable=True)
    lag_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reject_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FeedSource(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """One configured entity-matching feed (`plan.md §5.9`)."""

    __tablename__ = "feed_source"

    name: Mapped[str] = mapped_column(String(_NAME_MAX_LENGTH), nullable=False, unique=True)
    feed_type: Mapped[str] = mapped_column(String(_FEED_TYPE_MAX_LENGTH), nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EntityMatch(Base, UserAttributedColumns, StandardColumns):
    """One candidate match between a `FeedSource` record and an internal
    entity, including a rejected one — `R-30.b`'s negative-match memory
    depends on `is_negative` rows never being deleted (`plan.md §5.9`)."""

    __tablename__ = "entity_match"

    feed_source_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("feed_source.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    candidate_subject_type: Mapped[str] = mapped_column(
        String(_SUBJECT_TYPE_MAX_LENGTH), nullable=False
    )
    candidate_subject_id: Mapped[UUID | None] = mapped_column(GUID, nullable=True)
    external_reference: Mapped[str] = mapped_column(
        String(_EXTERNAL_REFERENCE_MAX_LENGTH), nullable=False
    )
    confidence: Mapped[Decimal | None] = mapped_column(FractionValue(), nullable=True)
    decision: Mapped[str] = mapped_column(String(_DECISION_MAX_LENGTH), nullable=False)
    is_negative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decided_by_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)


class RetentionPurgeLog(Base, UserAttributedColumns, StandardColumns):
    """`N-11`'s proof that a retention rule actually ran and what it did
    (`plan.md §5.9`). Append-only; no `version` column.

    `executed_by` is a label, not a foreign key — the same `_id`-suffix
    convention `plan.md §5` uses throughout distinguishes it from
    `executed_by_id`, and a scheduled purge with no human actor still
    needs somewhere to say so (`audit_event.actor_label` is the same
    pattern)."""

    __tablename__ = "retention_purge_log"

    entity: Mapped[str] = mapped_column(String(_ENTITY_MAX_LENGTH), nullable=False)
    criteria: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    purged_count: Mapped[int] = mapped_column(Integer, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
    executed_by: Mapped[str] = mapped_column(String(_EXECUTED_BY_MAX_LENGTH), nullable=False)


class EvaluationRun(Base, UserAttributedColumns, StandardColumns):
    """One offline evaluation pass, at one commit, on one arm — the
    scoreboard the release notes carry (`plan.md §5.9`)."""

    __tablename__ = "evaluation_run"

    commit_sha: Mapped[str] = mapped_column(String(_COMMIT_SHA_MAX_LENGTH), nullable=False)
    arm: Mapped[str] = mapped_column(String(_ARM_MAX_LENGTH), nullable=False)
    scores: Mapped[dict[str, object]] = mapped_column(PortableJSON, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(AwareDateTime, nullable=False)
