"""Read model for the threshold, model registry, drift, and evaluation
workspace (`T-081`): `spec §17.5`'s boundary table, `spec §16.4`'s "actor and
approver" requirement, and `spec §R-25`'s "before and after" requirement,
assembled without inventing a figure the store does not hold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.config.thresholds import THRESHOLD_NAMES
from covenant_radar.db.models.audit import ThresholdSnapshot
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.maker_checker import MakerCheckerRequest
from covenant_radar.db.models.operations import DriftObservation, EvaluationRun, ModelRegistration
from covenant_radar.db.session import is_database_session

_CALIBRATION_REFERENCE: Final[str] = (
    "config/thresholds.default.json — the packaged engineering defaults calibrated "
    "on the reference portfolio (spec §17.5)."
)

# `spec §17.5`'s boundary table, copied exactly: every threshold names what
# happens above, below and exactly at it, so this view never states a number
# without the behaviour that number decides.
_THRESHOLD_BOUNDARIES: Final[Mapping[str, tuple[str, str, str, str]]] = {
    "T1": (
        "Escalation probability at the worst covenant-horizon",
        "Act: queue top, case raised, memo offered",
        "Watch: listed, no escalation",
        "The boundary belongs to the higher band",
    ),
    "T2": (
        "Confidence floor for showing a probability",
        "Probability shown with its confidence",
        "“Insufficient evidence — watching” with the reason; no bare "
        "probability renders anywhere, including the API",
        "Shown — the floor is inclusive",
    ),
    "T3": (
        "Persistence: sustained if ≥14 consecutive days or ≥3 events in 30 days",
        "Sustained: feeds forecast pressure",
        "Transient: visible, decaying, no forecast effect",
        "Sustained — inclusive",
    ),
    "T4": (
        "Materiality: evidence counts if projected 90-day headroom erosion is ≥5% "
        "of the threshold value",
        "Counts toward pressure",
        "Noted on the ledger, excluded from pressure",
        "Counts — inclusive",
    ),
    "T5": (
        "Driver listed if contribution is ≥10% of the risk delta",
        "Listed with its share",
        "Folded into “other”",
        "Listed",
    ),
    "T6": (
        "Memo length ceiling",
        "Regenerate once shorter, else refuse",
        "Fine",
        "Fine",
    ),
    "T7": (
        "Model calls per hour, per day, and monetary budget per month",
        "Queued, with a banner and an alert",
        "Fine",
        "The ceiling engages at the value",
    ),
    "T8": (
        "Bad-shape retries, then refuse",
        "—",
        "—",
        "—",
    ),
    "T9": (
        "OCR page confidence floor",
        "Page text used",
        "Page routed to human review",
        "Used — inclusive",
    ),
    "T10": (
        "Entity-match confidence floor",
        "Auto-accepted",
        "Below the review floor discarded; between, queued for review",
        "Auto-accepted at the auto-accept value",
    ),
    "T11": (
        "SLA hours by band",
        "Overdue: escalates and appears in the digest",
        "Within SLA",
        "Overdue at the value",
    ),
    "T12": (
        "Batch completion deadline",
        "Alert raised, run continues",
        "Fine",
        "Alert at the deadline",
    ),
}

# `champion` is the live, non-rolled-back state; `retired` is what a
# documented rollback (`N-12.c`) leaves behind.  Order matters only for
# picking one representative row per monitored component.
_MODEL_STATE_PRIORITY: Final[Mapping[str, int]] = {
    "champion": 0,
    "challenger": 1,
    "approved": 2,
    "registered": 3,
    "retired": 4,
}


@dataclass(frozen=True, slots=True)
class ThresholdFieldView:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class ThresholdRowView:
    code: str
    label: str
    fields: tuple[ThresholdFieldView, ...]
    above: str
    below: str
    at: str


@dataclass(frozen=True, slots=True)
class ThresholdChangeView:
    code: str
    field: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class ThresholdSnapshotView:
    id: UUID
    effective_from: datetime
    source: str
    rows: tuple[ThresholdRowView, ...]
    proposed_by_id: UUID | None
    proposed_by_name: str | None
    approved_by_id: UUID | None
    approved_by_name: str | None
    note: str | None
    is_shipped_default: bool
    calibration_reference: str | None
    changes: tuple[ThresholdChangeView, ...]


@dataclass(frozen=True, slots=True)
class ThresholdProposalView:
    id: UUID
    maker_id: UUID
    proposer_name: str | None
    created_at: datetime
    note: str | None
    changes: tuple[ThresholdChangeView, ...]
    is_own_proposal: bool


@dataclass(frozen=True, slots=True)
class ModelRegistryView:
    id: UUID
    component: str
    provider: str
    model_id: str
    prompt_version: str | None
    purpose: str | None
    owner_name: str | None
    state: str
    approved: bool
    approved_by_name: str | None
    approved_at: datetime | None


@dataclass(frozen=True, slots=True)
class DriftView:
    id: UUID
    component: str
    metric: str
    value: str
    baseline: str
    window_start: datetime
    window_end: datetime
    breached: bool
    rollback_state: str


@dataclass(frozen=True, slots=True)
class EvaluationArmView:
    arm: str
    executed_at: datetime
    passed: bool
    scores: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class EvaluationReleaseView:
    commit_sha: str
    executed_at: datetime
    arms: tuple[EvaluationArmView, ...]
    all_passed: bool


@dataclass(frozen=True, slots=True)
class GovernanceView:
    current_threshold: ThresholdSnapshotView | None
    threshold_history: tuple[ThresholdSnapshotView, ...]
    pending_proposals: tuple[ThresholdProposalView, ...]
    models: tuple[ModelRegistryView, ...]
    unapproved_models: tuple[ModelRegistryView, ...]
    drift: tuple[DriftView, ...]
    breached_drift: tuple[DriftView, ...]
    evaluations: tuple[EvaluationReleaseView, ...]


def load_governance_view(session: Session, *, principal_id: UUID) -> GovernanceView:
    """Load read-only governance facts without inventing missing records."""

    if not is_database_session(session):
        raise TypeError("load_governance_view requires a SQLAlchemy Session.")
    if not isinstance(principal_id, UUID):
        raise TypeError("load_governance_view requires a principal UUID.")

    snapshot_rows = (
        session.execute(
            select(ThresholdSnapshot).order_by(
                ThresholdSnapshot.effective_from.desc(),
                ThresholdSnapshot.id.desc(),
            )
        )
        .scalars()
        .all()
    )
    proposal_rows = (
        session.execute(
            select(MakerCheckerRequest)
            .where(
                MakerCheckerRequest.state == "pending",
                MakerCheckerRequest.operation.in_(("threshold_change", "thresholds")),
            )
            .order_by(MakerCheckerRequest.created_at, MakerCheckerRequest.id)
        )
        .scalars()
        .all()
    )
    model_rows = (
        session.execute(
            select(ModelRegistration).order_by(ModelRegistration.component, ModelRegistration.id)
        )
        .scalars()
        .all()
    )
    drift_rows = (
        session.execute(
            select(DriftObservation).order_by(
                DriftObservation.window_end.desc(), DriftObservation.id
            )
        )
        .scalars()
        .all()
    )
    evaluation_rows = (
        session.execute(
            select(EvaluationRun).order_by(
                EvaluationRun.executed_at.desc(), EvaluationRun.id.desc()
            )
        )
        .scalars()
        .all()
    )

    user_ids: set[UUID] = set()
    for snapshot in snapshot_rows:
        user_ids.update(uid for uid in (snapshot.proposed_by_id, snapshot.approved_by_id) if uid)
    for proposal in proposal_rows:
        user_ids.add(proposal.maker_id)
    for model in model_rows:
        user_ids.update(uid for uid in (model.owner_id, model.approved_by_id) if uid)
    names = _user_names(session, user_ids)

    models_by_component = _representative_by_component(model_rows)

    snapshots = tuple(
        _snapshot_view(
            row,
            names=names,
            previous=(snapshot_rows[index + 1] if index + 1 < len(snapshot_rows) else None),
        )
        for index, row in enumerate(snapshot_rows)
    )
    proposals = tuple(
        _proposal_view(row, names=names, principal_id=principal_id) for row in proposal_rows
    )
    models = tuple(_model_view(row, names=names) for row in model_rows)
    unapproved = tuple(model for model in models if not model.approved)
    drift = tuple(_drift_view(row, models_by_component=models_by_component) for row in drift_rows)
    breached = tuple(row for row in drift if row.breached)
    evaluations = _evaluation_releases(evaluation_rows)

    return GovernanceView(
        current_threshold=snapshots[0] if snapshots else None,
        threshold_history=snapshots[1:],
        pending_proposals=proposals,
        models=models,
        unapproved_models=unapproved,
        drift=drift,
        breached_drift=breached,
        evaluations=evaluations,
    )


def _snapshot_view(
    row: ThresholdSnapshot,
    *,
    names: Mapping[UUID, str],
    previous: ThresholdSnapshot | None,
) -> ThresholdSnapshotView:
    is_default = row.source == "default"
    return ThresholdSnapshotView(
        id=row.id,
        effective_from=row.effective_from.astimezone(UTC),
        source=row.source,
        rows=_threshold_rows(row.values),
        proposed_by_id=row.proposed_by_id,
        proposed_by_name=names.get(row.proposed_by_id) if row.proposed_by_id else None,
        approved_by_id=row.approved_by_id,
        approved_by_name=names.get(row.approved_by_id) if row.approved_by_id else None,
        note=row.note,
        is_shipped_default=is_default,
        calibration_reference=_CALIBRATION_REFERENCE if is_default else None,
        changes=_threshold_diff(previous.values if previous is not None else {}, row.values),
    )


def _proposal_view(
    row: MakerCheckerRequest,
    *,
    names: Mapping[UUID, str],
    principal_id: UUID,
) -> ThresholdProposalView:
    payload = _as_mapping(row.payload)
    before = _as_mapping(payload.get("before"))
    after = _as_mapping(payload.get("after"))
    note = payload.get("note")
    return ThresholdProposalView(
        id=row.id,
        maker_id=row.maker_id,
        proposer_name=names.get(row.maker_id),
        created_at=row.created_at.astimezone(UTC),
        note=note if isinstance(note, str) else None,
        changes=_threshold_diff(before, after),
        is_own_proposal=row.maker_id == principal_id,
    )


def _model_view(row: ModelRegistration, *, names: Mapping[UUID, str]) -> ModelRegistryView:
    return ModelRegistryView(
        id=row.id,
        component=row.component,
        provider=row.provider,
        model_id=row.model_id,
        prompt_version=row.prompt_version,
        purpose=row.purpose,
        owner_name=names.get(row.owner_id) if row.owner_id else None,
        state=row.state,
        approved=row.approved_by_id is not None,
        approved_by_name=names.get(row.approved_by_id) if row.approved_by_id else None,
        approved_at=row.approved_at.astimezone(UTC) if row.approved_at else None,
    )


def _drift_view(
    row: DriftObservation,
    *,
    models_by_component: Mapping[str, ModelRegistration],
) -> DriftView:
    return DriftView(
        id=row.id,
        component=row.component,
        metric=row.metric,
        value=format(row.value, "f"),
        baseline=format(row.baseline, "f"),
        window_start=row.window_start.astimezone(UTC),
        window_end=row.window_end.astimezone(UTC),
        breached=row.breached,
        rollback_state=_rollback_state(models_by_component.get(row.component)),
    )


def _rollback_state(model: ModelRegistration | None) -> str:
    if model is None:
        return "No registered model for this component"
    if model.state == "champion":
        return "Active — no rollback"
    if model.state == "retired":
        return "Rolled back — retired to the prior version"
    return "Not promoted — no rollback needed"


def _evaluation_releases(rows: Sequence[EvaluationRun]) -> tuple[EvaluationReleaseView, ...]:
    by_release: dict[str, list[EvaluationRun]] = {}
    order: list[str] = []
    for row in rows:
        if row.commit_sha not in by_release:
            by_release[row.commit_sha] = []
            order.append(row.commit_sha)
        by_release[row.commit_sha].append(row)

    releases: list[EvaluationReleaseView] = []
    for commit_sha in order:
        runs = sorted(by_release[commit_sha], key=lambda run: run.arm)
        arms = tuple(
            EvaluationArmView(
                arm=run.arm,
                executed_at=run.executed_at.astimezone(UTC),
                passed=run.passed,
                scores=_pairs(run.scores),
            )
            for run in runs
        )
        releases.append(
            EvaluationReleaseView(
                commit_sha=commit_sha,
                executed_at=max(arm.executed_at for arm in arms),
                arms=arms,
                all_passed=all(arm.passed for arm in arms),
            )
        )
    releases.sort(key=lambda release: release.executed_at, reverse=True)
    return tuple(releases)


def _threshold_rows(values: Mapping[str, object]) -> tuple[ThresholdRowView, ...]:
    rows: list[ThresholdRowView] = []
    for code in THRESHOLD_NAMES:
        fields_raw = values.get(code)
        if not isinstance(fields_raw, Mapping):
            continue
        label, above, below, at = _THRESHOLD_BOUNDARIES[code]
        fields = tuple(
            ThresholdFieldView(name=str(key).replace("_", " "), value=_display(value))
            for key, value in sorted(fields_raw.items(), key=lambda item: str(item[0]))
        )
        rows.append(
            ThresholdRowView(code=code, label=label, fields=fields, above=above, below=below, at=at)
        )
    return tuple(rows)


def _threshold_diff(
    before: Mapping[str, object], after: Mapping[str, object]
) -> tuple[ThresholdChangeView, ...]:
    changes: list[ThresholdChangeView] = []
    for code in THRESHOLD_NAMES:
        before_fields = _as_mapping(before.get(code))
        after_fields = _as_mapping(after.get(code))
        field_names = sorted(set(before_fields) | set(after_fields))
        for field in field_names:
            before_display = _display(before_fields.get(field)) if field in before_fields else "—"
            after_display = _display(after_fields.get(field)) if field in after_fields else "—"
            if before_display != after_display:
                changes.append(
                    ThresholdChangeView(
                        code=code, field=field, before=before_display, after=after_display
                    )
                )
    return tuple(changes)


def _representative_by_component(
    rows: Sequence[ModelRegistration],
) -> dict[str, ModelRegistration]:
    best: dict[str, ModelRegistration] = {}
    for row in rows:
        current = best.get(row.component)
        if current is None or _MODEL_STATE_PRIORITY.get(row.state, 99) < _MODEL_STATE_PRIORITY.get(
            current.state, 99
        ):
            best[row.component] = row
    return best


def _user_names(session: Session, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = session.execute(select(AppUser.id, AppUser.full_name).where(AppUser.id.in_(ids)))
    return {user_id: full_name for user_id, full_name in rows.tuples()}


def _pairs(values: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(key).replace("_", " "), _display(value))
        for key, value in sorted(values.items(), key=lambda item: str(item[0]))
    )


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _display(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_display(item)}" for key, item in sorted(value.items()))
    if isinstance(value, list | tuple):
        return ", ".join(_display(item) for item in value)
    return str(value)


__all__ = [
    "DriftView",
    "EvaluationArmView",
    "EvaluationReleaseView",
    "GovernanceView",
    "ModelRegistryView",
    "ThresholdChangeView",
    "ThresholdFieldView",
    "ThresholdProposalView",
    "ThresholdRowView",
    "ThresholdSnapshotView",
    "load_governance_view",
]
