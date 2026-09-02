"""Read models for the administrator's threshold and action configuration.

The admin surface is intentionally a read model over the persisted snapshots,
maker-checker requests, catalogue rows, and the latest completed forecast run.
It does not derive a new forecast or expose borrower identities while showing
the effect of a proposed banding change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, scoped_session

from covenant_radar.core.errors import ValidationError
from covenant_radar.db.models.forecast import ForecastRun, Intervention, TriageEntry
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.maker_checker import MakerCheckerRequest
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.interventions.catalogue import CatalogueEntry
from covenant_radar.domain.triage.banding import BANDS, TriageThresholds, band
from covenant_radar.services.catalogue import CATALOGUE_OPERATION
from covenant_radar.web.view_models.governance import (
    ThresholdProposalView,
    ThresholdSnapshotView,
    load_governance_view,
)

_COMPLETE_RUN_STATE: Final[str] = "complete"
_PENDING_STATE: Final[str] = "pending"


@dataclass(frozen=True, slots=True)
class CatalogueEntryView:
    """Safe catalogue row for the configuration screen."""

    code: str
    role_tag: str
    text: str
    effect_model: str
    applicable_covenant_classes: tuple[str, ...]
    assumptions: tuple[str, ...]
    requires_approval: bool
    is_active: bool
    version: int

    @property
    def status(self) -> str:
        """Return the stable display state without exposing implementation data."""

        return "active" if self.is_active else "retired"


@dataclass(frozen=True, slots=True)
class CatalogueApprovalView:
    """Review-safe projection of one pending catalogue change."""

    id: UUID
    maker_id: UUID
    maker_name: str | None
    created_at: datetime
    entry_code: str
    is_own_proposal: bool


@dataclass(frozen=True, slots=True)
class BandTransitionView:
    """Aggregate count for one old-to-new band transition."""

    before: str
    after: str
    count: int


@dataclass(frozen=True, slots=True)
class BandChangePreview:
    """The non-persistent blast-radius preview for a threshold proposal."""

    run_id: UUID | None
    run_as_of: date | None
    total_borrowers: int
    changed_borrowers: int
    transitions: tuple[BandTransitionView, ...]
    message: str | None = None

    @property
    def available(self) -> bool:
        """Whether the count was computed against a completed run."""

        return self.run_id is not None

    @property
    def borrower_count(self) -> int:
        """Compatibility spelling for the screen and integration tests."""

        return self.total_borrowers

    @property
    def changed_count(self) -> int:
        """Compatibility spelling for the screen and integration tests."""

        return self.changed_borrowers


@dataclass(frozen=True, slots=True)
class ThresholdApplicationView:
    """The completed run that consumed a threshold snapshot, when known."""

    run_id: UUID
    as_of: date


@dataclass(frozen=True, slots=True)
class AdminConfigView:
    """Complete read model for the admin configuration workspace."""

    current_threshold: ThresholdSnapshotView | None
    current_threshold_application: ThresholdApplicationView | None
    threshold_history: tuple[ThresholdSnapshotView, ...]
    pending_threshold_proposals: tuple[ThresholdProposalView, ...]
    catalogue: tuple[CatalogueEntryView, ...]
    pending_catalogue_approvals: tuple[CatalogueApprovalView, ...]
    band_preview: BandChangePreview | None


def load_admin_config_view(
    session: Session | scoped_session[Session],
    *,
    principal_id: UUID,
    band_preview: BandChangePreview | None = None,
) -> AdminConfigView:
    """Load persisted configuration facts without calculating a forecast."""

    if not is_database_session(session):
        raise TypeError("load_admin_config_view requires a SQLAlchemy Session.")
    if not isinstance(principal_id, UUID):
        raise TypeError("load_admin_config_view requires a principal UUID.")

    governance = load_governance_view(cast(Session, session), principal_id=principal_id)
    application = _threshold_application(session, governance.current_threshold)
    catalogue_rows = session.scalars(
        select(Intervention).order_by(
            Intervention.is_active.desc(), Intervention.code, Intervention.id
        )
    ).all()
    catalogue = tuple(_catalogue_view(row) for row in catalogue_rows)

    pending_rows = session.scalars(
        select(MakerCheckerRequest)
        .where(
            MakerCheckerRequest.operation == CATALOGUE_OPERATION,
            MakerCheckerRequest.state == _PENDING_STATE,
        )
        .order_by(MakerCheckerRequest.created_at, MakerCheckerRequest.id)
    ).all()
    maker_ids = {row.maker_id for row in pending_rows}
    names = _user_names(session, maker_ids)
    pending = tuple(
        CatalogueApprovalView(
            id=row.id,
            maker_id=row.maker_id,
            maker_name=names.get(row.maker_id),
            created_at=_utc(row.created_at),
            entry_code=_entry_code(row.payload),
            is_own_proposal=row.maker_id == principal_id,
        )
        for row in pending_rows
    )

    return AdminConfigView(
        current_threshold=governance.current_threshold,
        current_threshold_application=application,
        threshold_history=governance.threshold_history,
        pending_threshold_proposals=governance.pending_proposals,
        catalogue=catalogue,
        pending_catalogue_approvals=pending,
        band_preview=band_preview,
    )


def build_band_change_preview(
    session: Session | scoped_session[Session],
    *,
    before_values: Mapping[str, object],
    after_values: Mapping[str, object],
) -> BandChangePreview:
    """Compare stored bands with a candidate against the latest complete run.

    The latest run's persisted band is the baseline.  This matters when an
    approved snapshot is waiting for the next run: the preview describes what
    that next application would change and never rewrites the old run.
    """

    if not is_database_session(session):
        raise TypeError("build_band_change_preview requires a SQLAlchemy Session.")
    try:
        before_thresholds = TriageThresholds.from_store(before_values)
        after_thresholds = TriageThresholds.from_store(after_values)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            f"Threshold band preview cannot be computed: {error}.",
            field="thresholds.T1",
        ) from error

    run = session.scalar(
        select(ForecastRun)
        .where(ForecastRun.state == _COMPLETE_RUN_STATE)
        .order_by(
            ForecastRun.as_of_date.desc(),
            ForecastRun.finished_at.desc().nullslast(),
            ForecastRun.id.desc(),
        )
        .limit(1)
    )
    if run is None:
        return BandChangePreview(
            run_id=None,
            run_as_of=None,
            total_borrowers=0,
            changed_borrowers=0,
            transitions=(),
            message="No completed forecast run is available for a band-change preview.",
        )

    rows = session.scalars(
        select(TriageEntry)
        .where(TriageEntry.run_id == run.id)
        .order_by(TriageEntry.rank, TriageEntry.borrower_id)
    ).all()
    transition_counts: dict[tuple[str, str], int] = {}
    changed = 0
    for row in rows:
        stored_band = row.band if row.band in BANDS else band(row.probability, before_thresholds)
        proposed_band = band(row.probability, after_thresholds)
        if stored_band == proposed_band:
            continue
        changed += 1
        key = (stored_band, proposed_band)
        transition_counts[key] = transition_counts.get(key, 0) + 1

    transitions = tuple(
        BandTransitionView(before=before, after=after, count=count)
        for (before, after), count in sorted(transition_counts.items())
    )
    return BandChangePreview(
        run_id=run.id,
        run_as_of=run.as_of_date,
        total_borrowers=len(rows),
        changed_borrowers=changed,
        transitions=transitions,
    )


def _catalogue_view(row: Intervention) -> CatalogueEntryView:
    try:
        entry = CatalogueEntry.from_record(row)
    except (TypeError, ValueError, ValidationError) as error:
        raise ValidationError(
            f"Intervention {row.code!r} is invalid and cannot be shown: {error}.",
            field="intervention",
        ) from error
    return CatalogueEntryView(
        code=entry.code,
        role_tag=entry.role_tag.value,
        text=entry.text,
        effect_model=entry.effect_model.value,
        applicable_covenant_classes=tuple(sorted(entry.applicable_covenant_classes)),
        assumptions=entry.assumptions,
        requires_approval=entry.requires_approval,
        is_active=entry.is_active,
        version=entry.version,
    )


def _threshold_application(
    session: Session | scoped_session[Session],
    snapshot: ThresholdSnapshotView | None,
) -> ThresholdApplicationView | None:
    if snapshot is None:
        return None
    run = session.scalar(
        select(ForecastRun)
        .where(
            ForecastRun.threshold_snapshot_id == snapshot.id,
            ForecastRun.state == _COMPLETE_RUN_STATE,
        )
        .order_by(
            ForecastRun.as_of_date.desc(),
            ForecastRun.finished_at.desc().nullslast(),
            ForecastRun.id.desc(),
        )
        .limit(1)
    )
    if run is None:
        return None
    return ThresholdApplicationView(run_id=run.id, as_of=run.as_of_date)


def _entry_code(payload: object) -> str:
    if isinstance(payload, Mapping):
        raw_entry = payload.get("entry")
        if isinstance(raw_entry, Mapping):
            code = raw_entry.get("code", raw_entry.get("id"))
            if isinstance(code, str) and code.strip():
                return code.strip()
    return "Unavailable entry"


def _user_names(
    session: Session | scoped_session[Session], ids: set[UUID]
) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = session.execute(select(AppUser.id, AppUser.full_name).where(AppUser.id.in_(ids)))
    return {user_id: full_name for user_id, full_name in rows.tuples()}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("Configuration timestamps must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "AdminConfigView",
    "BandChangePreview",
    "BandTransitionView",
    "CatalogueApprovalView",
    "CatalogueEntryView",
    "ThresholdApplicationView",
    "build_band_change_preview",
    "load_admin_config_view",
]
