"""Read model for the borrower case file (T-075).

The case file is a presentation read, not a second risk engine.  All values
that can affect a credit decision come from persisted records: exposure and
the worst-risk pointer come from the latest completed triage entry, covenant
values come from the latest covenant test, thresholds come from the effective
covenant version, and dates come from schedules or forecasts.  This module
only selects, groups and formats those records for the templates.

The child queries carry the same portfolio predicate as the borrower lookup.
That is intentional even after the borrower has been resolved: it keeps this
read model safe if it is reused by a caller that supplies a different scoped
borrower object, and makes the row-level access rule visible at every query
boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal, cast
from urllib.parse import quote
from uuid import UUID

from markupsafe import Markup
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
)
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastDriver,
    ForecastPath,
    ForecastRun,
    Intervention,
    TriageEntry,
)
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import EvidenceItem, SignalEvent
from covenant_radar.db.models.workflow import Case
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.repositories.forecast import ForecastRepository
from covenant_radar.db.scoping import Scope, ownership_path_for
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.interventions.applicability import is_applicable
from covenant_radar.domain.signals import FAMILIES, definition_for
from covenant_radar.i18n.formatting import format_indian_currency, format_ist_date
from covenant_radar.web.svg.trajectory import (
    TrajectoryCrossing,
    TrajectoryLedgerFigure,
    TrajectoryPoint,
    render_trajectory_sparkline_svg,
    render_trajectory_svg,
)

PanelState = Literal["rest", "loading", "error", "empty", "degraded"]
CaseFileState = Literal["ready", "loading", "error", "degraded"]

NO_COMPLETED_TEST: Final[str] = "No completed test is recorded for this covenant."
NO_SCHEDULE: Final[str] = "No next test is recorded in the covenant schedule."
NO_THRESHOLD: Final[str] = "Threshold unavailable — the covenant version has no threshold record."
NO_VALUE: Final[str] = "Value unavailable — no completed test is recorded."
NO_HEADROOM: Final[str] = "Headroom unavailable — no completed test is recorded."
NO_WORST_COVENANT: Final[str] = (
    "Unavailable — the latest completed run has no worst-covenant record."
)
NO_EXPOSURE: Final[str] = "Unavailable — the latest completed run has no stored exposure record."
NO_DATED_RISK: Final[str] = "Unavailable — the latest completed run has no dated-risk record."
NO_EVIDENCE: Final[str] = (
    "No evidence has been recorded for this borrower yet. Evidence will appear here after "
    "the next scored run."
)
NO_COVENANTS: Final[str] = "No active covenant is recorded for this borrower."
NO_FORECAST_PANEL: Final[str] = (
    "No covenant forecast is available for this borrower yet. The panel will populate after "
    "the next completed forecast run."
)
NO_FORECAST_FOR_HORIZON: Final[str] = (
    "No forecast is recorded for this horizon in the latest completed run."
)
NO_CONFIDENCE: Final[str] = "Confidence unavailable — the forecast did not record confidence."
NO_PROBABILITY: Final[str] = (
    "Probability unavailable — the forecast did not record a displayable probability."
)
NO_FORECAST_PATH: Final[str] = (
    "Trajectory unavailable — the latest completed run has no complete stored daily path."
)
NO_FORECAST_TRAJECTORY: Final[str] = (
    "Trajectory unavailable — no stored forecast record is available for the plotted horizon."
)
SUPPRESSED_RISK: Final[str] = "Suppressed — confidence is below the display floor."
NO_PROJECTED_CROSSING: Final[str] = "No projected crossing in the stored forecast horizon."
NOT_COMPUTABLE_PREFIX: Final[str] = "Not computable — "
FORECAST_HORIZONS: Final[tuple[int, ...]] = (30, 60, 90)

_DIRECTION_SYMBOLS: Final[Mapping[str, str]] = {"min": "≥", "max": "≤"}
_DIRECTION_ARROWS: Final[Mapping[str, str]] = {"min": "↓", "max": "↑"}
_VERDICT_LABELS: Final[Mapping[str, str]] = {
    "pass": "Pass",
    "warning": "Warning",
    "breach": "Breach",
    "breach_cure_open": "Breach — cure open",
    "stale": "Stale",
    "not_computable": "Not computable",
    "not_tested": "Not tested",
}
_STALE_PERIOD_KEYS: Final[tuple[str, ...]] = (
    "last_complete_period",
    "last_complete_period_label",
    "complete_period",
    "period_label",
)
_STALE_REDUCTION_KEYS: Final[tuple[str, ...]] = (
    "confidence_reduction",
    "confidence_reduction_pct",
    "confidence_reduced_by",
)
_DECAY_STATE_LABELS: Final[Mapping[str, str]] = {
    "fresh": "Fresh",
    "decaying": "Decaying",
    "decayed": "Decayed",
    "not_recorded": "Decay not recorded",
}
_FACTOR_LABELS: Final[Mapping[str, str]] = {
    "distance": "Distance to covenant threshold",
    "velocity": "Rate of deterioration",
    "pressure": "Sustained evidence pressure",
}
_DRIVER_LABELS: Final[Mapping[str, str]] = {
    "distance": "Proximity to covenant threshold",
    "velocity": "Rate of deterioration",
    "trend": "Covenant trajectory trend",
    "pressure": "Sustained evidence pressure",
    "other": "Other smaller contributions",
    "neutral": "No attributable risk movement",
}
_ROLE_LABELS: Final[Mapping[str, str]] = {
    "relationship_manager": "Relationship manager",
    "credit": "Credit",
    "risk": "Risk",
}
NO_SIMULATION_FORECAST: Final[str] = "No forecast is available to simulate against."
NO_MEMO_FORECAST: Final[str] = "No forecast is available to draft a memo from."
LOG_ACTION_UNAVAILABLE: Final[str] = (
    "No case is open for this borrower yet; an action is logged against a case."
)


@dataclass(frozen=True, slots=True)
class HeaderFactView:
    """One of the four and only four facts allowed above the fold."""

    key: str
    label: str
    value: str
    detail: str = ""
    missing: bool = False
    raw_value: Decimal | date | str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.label.strip() or not self.value.strip():
            raise ValueError("A case-file header fact requires non-empty key, label and value.")


@dataclass(frozen=True, slots=True)
class CaseFileHeaderView:
    """Header data with a structural four-fact invariant."""

    borrower_name: str
    borrower_reference: str
    facts: tuple[HeaderFactView, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", tuple(self.facts))
        if len(self.facts) != 4:
            raise ValueError("The case-file header must contain exactly four facts.")
        if len({fact.key for fact in self.facts}) != 4:
            raise ValueError("Case-file header fact keys must be unique.")

    @property
    def exposure(self) -> Decimal | None:
        """Return the stored exposure, when the exposure fact has one."""
        raw_value = self._fact("exposure").raw_value
        return raw_value if isinstance(raw_value, Decimal) else None

    @property
    def worst_covenant(self) -> str | None:
        """Return the displayed worst covenant, when it exists."""
        fact = self._fact("worst_covenant")
        return None if fact.missing else fact.value

    @property
    def dated_risk(self) -> str:
        """Return the non-empty dated-risk presentation text."""
        return self._fact("dated_risk").value

    def _fact(self, key: str) -> HeaderFactView:
        return next(fact for fact in self.facts if fact.key == key)


@dataclass(frozen=True, slots=True)
class CaseFilePanelView:
    """State and message for a panel that may degrade independently."""

    state: PanelState
    title: str
    empty_title: str
    empty_message: str
    error_title: str = "Unable to load this panel"
    error_message: str = "Reload the case file. If the problem continues, contact an administrator."
    degraded_message: str = "Locally stored borrower facts remain available."

    def __post_init__(self) -> None:
        if self.state not in {"rest", "loading", "error", "empty", "degraded"}:
            raise ValueError(f"Unsupported case-file panel state: {self.state!r}.")


ForecastHorizonState = Literal["available", "suppressed", "not_computable", "unavailable"]


@dataclass(frozen=True, slots=True)
class ForecastFactorView:
    """One inspectable term in the deterministic probability rule."""

    name: str
    label: str
    input_display: str
    normalized_display: str
    weight_display: str
    contribution_display: str


@dataclass(frozen=True, slots=True)
class ForecastDriverView:
    """One persisted attribution with an optional evidence citation."""

    name: str
    label: str
    share: Decimal
    share_display: str
    evidence_id: UUID | None
    evidence_href: str
    is_other: bool


@dataclass(frozen=True, slots=True)
class ModelContributionView:
    """One signed contribution disclosed by the statistical model."""

    name: str
    label: str
    value_display: str
    direction: str


@dataclass(frozen=True, slots=True)
class ForecastCitationView:
    """A source record supporting a displayed prediction explanation."""

    label: str
    href: str
    source_type: str


@dataclass(frozen=True, slots=True)
class ForecastExplanationView:
    """Human-readable reasoning assembled only from persisted forecast facts.

    This is deliberately not an LLM completion.  The TCS-backed model may
    draft a stage-7 memo, but the explanation beside a credit-risk prediction
    must remain available during provider outages and must reproduce exactly
    which rule or governed statistical artifact supplied the operational
    number.
    """

    method: str
    method_label: str
    operational_label: str
    summary: str
    rationale: str
    provenance_statement: str
    rule_version: str | None
    deterministic_probability_display: str | None
    ml_probability_display: str | None
    ml_role: str | None
    ml_model_version: str | None
    artifact_checksum: str | None
    feature_snapshot_hash: str | None
    fallback_reason: str | None
    governance_warning: str | None
    factors: tuple[ForecastFactorView, ...]
    drivers: tuple[ForecastDriverView, ...]
    model_contributions: tuple[ModelContributionView, ...]
    citations: tuple[ForecastCitationView, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "factors", tuple(self.factors))
        object.__setattr__(self, "drivers", tuple(self.drivers))
        object.__setattr__(self, "model_contributions", tuple(self.model_contributions))
        object.__setattr__(self, "citations", tuple(self.citations))
        if not self.method.strip() or not self.method_label.strip():
            raise ValueError("A forecast explanation requires a prediction method.")
        if not self.operational_label.strip() or not self.summary.strip():
            raise ValueError("A forecast explanation requires an operational summary.")
        if not self.rationale.strip() or not self.provenance_statement.strip():
            raise ValueError("A forecast explanation requires rationale and provenance.")


@dataclass(frozen=True, slots=True)
class ActionableInsightView:
    """One bank-owned, simulator-backed candidate action."""

    code: str
    role_label: str
    text: str
    effect_model: str
    applicability_reason: str
    assumptions: tuple[str, ...]
    requires_approval: bool
    simulator_href: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        if not self.code.strip() or not self.role_label.strip() or not self.text.strip():
            raise ValueError("An actionable insight requires code, owner and text.")
        if not self.effect_model.strip() or not self.applicability_reason.strip():
            raise ValueError("An actionable insight requires effect and applicability details.")


@dataclass(frozen=True, slots=True)
class ForecastHorizonView:
    """One persisted forecast horizon, with the confidence guard applied."""

    horizon_days: int
    label: str
    state: ForecastHorizonState
    probability: Decimal | None
    confidence: Decimal | None
    crossing_date: date | None
    probability_display: str
    confidence_display: str
    crossing_display: str
    direction_label: str
    limiting_factor: str | None = None
    reason: str | None = None
    forecast_id: UUID | None = None
    explanation: ForecastExplanationView | None = None
    is_primary: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.horizon_days, bool)
            or not isinstance(self.horizon_days, int)
            or self.horizon_days < 0
        ):
            raise ValueError("Forecast horizon must be a non-negative integer.")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Forecast horizon label must be non-empty.")
        if self.state not in {"available", "suppressed", "not_computable", "unavailable"}:
            raise ValueError(f"Unsupported forecast horizon state: {self.state!r}.")
        if not self.probability_display.strip() or not self.confidence_display.strip():
            raise ValueError("Forecast horizon displays must be non-empty.")
        if not self.crossing_display.strip() or not self.direction_label.strip():
            raise ValueError("Forecast horizon crossing displays must be non-empty.")


@dataclass(frozen=True, slots=True)
class ForecastCovenantView:
    """Forecast figures and one stored daily path for a covenant."""

    row_id: str
    covenant_reference: str
    covenant_name: str
    facility_reference: str
    threshold: Decimal | None
    unit: str
    horizons: tuple[ForecastHorizonView, ...]
    ledger_figures: tuple[TrajectoryLedgerFigure, ...]
    trajectory_id: str
    trajectory_svg: Markup
    trajectory_available: bool
    trajectory_message: str
    maximum_day: int = max(FORECAST_HORIZONS)
    as_of_date: date | None = None
    updates_header: bool = False
    trajectory_driver: str | None = None
    actionable_insights: tuple[ActionableInsightView, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "horizons", tuple(self.horizons))
        object.__setattr__(self, "ledger_figures", tuple(self.ledger_figures))
        object.__setattr__(self, "actionable_insights", tuple(self.actionable_insights))
        if len(self.horizons) != len(FORECAST_HORIZONS):
            raise ValueError("A forecast covenant must expose all named horizons.")
        if not self.trajectory_id.strip() or not self.trajectory_message.strip():
            raise ValueError("Forecast trajectory identity and state must be non-empty.")
        if isinstance(self.maximum_day, bool) or not isinstance(self.maximum_day, int):
            raise ValueError("Forecast trajectory maximum day must be an integer.")
        if self.maximum_day < 0:
            raise ValueError("Forecast trajectory maximum day must be non-negative.")
        if self.as_of_date is not None and not isinstance(self.as_of_date, date):
            raise TypeError("Forecast trajectory as-of date must be a calendar date.")


@dataclass(frozen=True, slots=True)
class ForecastPanelView:
    """Forecast panel state for the borrower case file."""

    state: PanelState
    title: str
    covenants: tuple[ForecastCovenantView, ...]
    empty_title: str
    empty_message: str
    error_title: str = "Unable to load the forecast panel"
    error_message: str = "Reload the case file. If the problem continues, contact an administrator."
    degraded_message: str = "Stored forecast facts remain available."

    def __post_init__(self) -> None:
        object.__setattr__(self, "covenants", tuple(self.covenants))
        if self.state not in {"rest", "loading", "error", "empty", "degraded"}:
            raise ValueError(f"Unsupported forecast panel state: {self.state!r}.")


@dataclass(frozen=True, slots=True)
class CovenantRowView:
    """One covenant ledger row, with raw records and safe display strings."""

    row_id: str
    covenant_reference: str
    covenant_name: str
    facility_reference: str
    unit: str
    value: Decimal | None
    threshold: Decimal | None
    headroom_pct: Decimal | None
    verdict: str
    verdict_display: str
    next_test_date: date | None
    trajectory_arrow: str
    trajectory_label: str
    value_display: str
    threshold_display: str
    headroom_display: str
    next_test_display: str
    status_message: str
    detail_message: str = ""
    not_computable_reason: str | None = None
    stale_period: str | None = None
    confidence_reduction: str | None = None


EvidenceDecayState = Literal["fresh", "decaying", "decayed", "not_recorded"]
_DECAY_STATES: Final[frozenset[str]] = frozenset({"fresh", "decaying", "decayed", "not_recorded"})


@dataclass(frozen=True, slots=True)
class EvidenceMarginView:
    """One persisted evidence pattern, including whether it affects pressure."""

    id: UUID
    family: str
    evidence_type: str
    first_seen: date
    last_seen: date
    persistence_display: str
    materiality_display: str
    decay_display: str
    decay_state: EvidenceDecayState
    state: str
    counts_toward_pressure: bool
    anchor_id: str
    superseded_by_id: UUID | None = None
    supersedes_id: UUID | None = None
    not_counting_reason: str | None = None

    def __post_init__(self) -> None:
        if self.decay_state not in _DECAY_STATES:
            raise ValueError(f"Unsupported evidence decay state: {self.decay_state!r}.")
        if not self.anchor_id.strip():
            raise ValueError("An evidence margin row requires a non-empty anchor id.")
        if self.counts_toward_pressure and self.not_counting_reason is not None:
            raise ValueError("An item counting toward pressure carries no exclusion reason.")
        if not self.counts_toward_pressure and not self.not_counting_reason:
            raise ValueError("An item excluded from pressure requires a stated reason.")


@dataclass(frozen=True, slots=True)
class EvidenceFamilyGroupView:
    """One evidence family, holding every item recorded for it — decayed and
    superseded items included, so grouping never hides a row (`spec §R-11.e`)."""

    family: str
    items: tuple[EvidenceMarginView, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if not self.family.strip():
            raise ValueError("An evidence family group requires a non-empty family label.")
        if not self.items:
            raise ValueError("An evidence family group requires at least one item.")


@dataclass(frozen=True, slots=True)
class SignalFamilyView:
    """A selectable, source-backed signal family card.

    Raw events and adverse flags remain visible alongside the derived trend;
    the case file never presents a synthetic score without the records that
    produced it.  ``why_href`` intentionally points at the borrower trace,
    where ingestion, evidence, forecast, and ranking stages can be rebuilt.
    """

    family: str
    label: str
    event_count: int
    adverse_count: int
    latest_date: date | None
    current_display: str
    prior_display: str
    trend_display: str
    status: str
    sparkline_svg: Markup | None
    why_href: str

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"Unknown signal family {self.family!r}.")
        if self.event_count < 0 or self.adverse_count < 0:
            raise ValueError("Signal event counts cannot be negative.")
        if self.adverse_count > self.event_count:
            raise ValueError("Adverse signal count cannot exceed event count.")
        if (
            not self.label.strip()
            or not self.current_display.strip()
            or not self.trend_display.strip()
        ):
            raise ValueError("Signal family presentation fields must be non-empty.")
        if not self.why_href.strip():
            raise ValueError("Signal family view requires a why link.")


@dataclass(frozen=True, slots=True)
class DocumentStripView:
    """One source document available to the borrower case file."""

    id: UUID
    filename: str
    doc_type: str
    uploaded_on: date
    extraction_state: str
    href: str


@dataclass(frozen=True, slots=True)
class CaseActionsView:
    """Case actions rendered by permission — matrix-exact, per `spec §15.1`.

    A control the caller's role may not take is never constructed by
    ``build_borrower_view``, so the template has no way to render it. A
    control the role may take but that has nothing to act on yet (no
    forecast, no simulation, no case workspace) still renders, disabled,
    with the reason a user would otherwise be left to guess at.
    """

    why_href: str
    can_simulate: bool
    simulator_href: str | None
    simulate_reason: str
    can_generate_memo: bool
    memo_href: str | None
    memo_reason: str
    can_log_action: bool
    log_reason: str
    #: The case workspace an action is logged against.  `None` means this
    #: borrower has no case yet, which is the only reason the control is
    #: shown disabled — the capability itself is implemented.
    log_href: str | None = None

    def __post_init__(self) -> None:
        if not self.why_href.strip():
            raise ValueError("A case actions view requires a non-empty why href.")
        if self.can_simulate and not self.simulator_href and not self.simulate_reason.strip():
            raise ValueError("A visible, unavailable simulate action requires a reason.")
        if self.can_generate_memo and not self.memo_href and not self.memo_reason.strip():
            raise ValueError("A visible, unavailable memo action requires a reason.")
        if self.can_log_action and not self.log_href and not self.log_reason.strip():
            raise ValueError("A visible, unavailable log action requires a reason.")


@dataclass(frozen=True, slots=True)
class CaseFileView:
    """Complete T-075 screen view model."""

    borrower_reference: str
    borrower_name: str
    borrower_active: bool
    header: CaseFileHeaderView
    covenants: tuple[CovenantRowView, ...]
    covenants_panel: CaseFilePanelView
    forecast_panel: ForecastPanelView
    evidence_panel: CaseFilePanelView
    evidence: tuple[EvidenceMarginView, ...] = ()
    evidence_families: tuple[EvidenceFamilyGroupView, ...] = ()
    signal_families: tuple[SignalFamilyView, ...] = ()
    documents: tuple[DocumentStripView, ...] = ()
    documents_upload_href: str | None = None
    financial_statements_href: str | None = None
    actions: CaseActionsView | None = None
    state: CaseFileState = "ready"

    def __post_init__(self) -> None:
        object.__setattr__(self, "covenants", tuple(self.covenants))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "evidence_families", tuple(self.evidence_families))
        object.__setattr__(self, "signal_families", tuple(self.signal_families))
        object.__setattr__(self, "documents", tuple(self.documents))
        if self.state not in {"ready", "loading", "error", "degraded"}:
            raise ValueError(f"Unsupported case-file state: {self.state!r}.")


def load_borrower_case_file(
    session: Session,
    reference: str,
    *,
    scope: Scope,
    can_run_simulation: bool,
    can_generate_memo: bool,
    can_log_action: bool,
    can_upload_document: bool,
    can_ingest_financial_statements: bool = False,
) -> CaseFileView | None:
    """Load one scoped borrower by reference and build its case-file view.

    ``None`` deliberately represents both unknown and out-of-scope borrowers;
    the route maps both to the same designed 404 response. The four
    ``can_*`` flags are the caller's already-resolved permission grants
    (`spec §15.1`'s action row, matrix-exact) — this module stays decoupled
    from the security package and receives booleans, not a ``Principal``.
    """
    if not is_database_session(session):
        raise TypeError("load_borrower_case_file requires a SQLAlchemy Session.")
    if not isinstance(scope, Scope):
        raise TypeError("load_borrower_case_file requires a portfolio Scope.")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("Borrower reference must be non-empty text.")
    borrower = BorrowerRepository(session).by_reference(reference.strip(), scope=scope)
    if borrower is None:
        return None
    return build_borrower_view(
        borrower,
        session,
        scope=scope,
        can_run_simulation=can_run_simulation,
        can_generate_memo=can_generate_memo,
        can_log_action=can_log_action,
        can_upload_document=can_upload_document,
        can_ingest_financial_statements=can_ingest_financial_statements,
    )


def build_borrower_view(
    borrower: Borrower,
    session: Session,
    *,
    scope: Scope,
    can_run_simulation: bool,
    can_generate_memo: bool,
    can_log_action: bool,
    can_upload_document: bool,
    can_ingest_financial_statements: bool = False,
) -> CaseFileView:
    """Assemble a case-file view from persisted, scoped records only."""
    if not isinstance(borrower, Borrower):
        raise TypeError("build_borrower_view requires a Borrower record.")
    if not is_database_session(session):
        raise TypeError("build_borrower_view requires a SQLAlchemy Session.")
    if not isinstance(scope, Scope):
        raise TypeError("build_borrower_view requires a portfolio Scope.")
    for flag_name, flag_value in (
        ("can_run_simulation", can_run_simulation),
        ("can_generate_memo", can_generate_memo),
        ("can_log_action", can_log_action),
        ("can_upload_document", can_upload_document),
        ("can_ingest_financial_statements", can_ingest_financial_statements),
    ):
        if not isinstance(flag_value, bool):
            raise TypeError(f"build_borrower_view requires {flag_name} to be a bool.")

    latest_run = _latest_complete_run(session, borrower.id, scope)
    triage = _latest_triage_entry(session, borrower.id, latest_run, scope)
    covenant_records = _covenant_records(session, borrower.id, scope)
    selected_versions = _select_versions(covenant_records)
    version_ids = tuple(version.id for _, _, version in selected_versions if version is not None)
    tests = _latest_tests(session, version_ids, scope)
    schedules = _next_schedules(
        session,
        version_ids,
        scope,
        anchor=latest_run.as_of_date if latest_run is not None else None,
    )
    forecasts_by_key = _forecasts_for_covenants(session, version_ids, latest_run, scope)
    paths_by_version = _paths_for_covenants(session, version_ids, latest_run, scope)
    forecast_drivers = _forecast_drivers(session, forecasts_by_key, scope)
    interventions = _active_interventions(session)
    forecasts = _forecasts_for_risk(session, triage, latest_run, scope)
    evidence = _evidence_margin(session, borrower.id, scope)
    evidence_families = _group_evidence_by_family(evidence)
    signal_families = _signal_family_views(
        session,
        borrower.id,
        scope,
        as_of_date=latest_run.as_of_date if latest_run is not None else None,
    )
    documents = _document_strip(session, borrower.id, scope)

    covenant_rows = tuple(
        _covenant_row(covenant, facility, version, tests, schedules)
        for covenant, facility, version in selected_versions
    )
    covenant_rows = tuple(sorted(covenant_rows, key=lambda row: (row.covenant_name, row.row_id)))
    covenant_panel = CaseFilePanelView(
        state="rest" if covenant_rows else "empty",
        title="Covenant position",
        empty_title="No active covenants",
        empty_message=NO_COVENANTS,
    )
    default_header_version_id = cast(
        UUID | None,
        next(
            (version.id for _, _, version in selected_versions if version is not None),
            None,
        ),
    )
    forecast_panel = _forecast_panel(
        selected_versions,
        forecasts_by_key,
        paths_by_version,
        latest_run,
        forecast_drivers=forecast_drivers,
        interventions=interventions,
        can_run_simulation=can_run_simulation,
        header_version_id=(
            triage.worst_covenant_version_id
            if triage is not None and triage.worst_covenant_version_id is not None
            else default_header_version_id
        ),
    )
    evidence_panel = CaseFilePanelView(
        state="rest" if evidence else "empty",
        title="Evidence",
        empty_title="No evidence recorded",
        empty_message=NO_EVIDENCE,
    )
    header = _header_view(borrower, triage, forecasts, selected_versions)
    actions = _case_actions(
        borrower,
        forecasts,
        can_run_simulation=can_run_simulation,
        can_generate_memo=can_generate_memo,
        can_log_action=can_log_action,
        case_reference=(
            _latest_case_reference(session, borrower.id, scope) if can_log_action else None
        ),
    )
    return CaseFileView(
        borrower_reference=borrower.reference,
        borrower_name=borrower.legal_name,
        borrower_active=borrower.is_active,
        header=header,
        covenants=covenant_rows,
        covenants_panel=covenant_panel,
        forecast_panel=forecast_panel,
        evidence_panel=evidence_panel,
        evidence=evidence,
        evidence_families=evidence_families,
        signal_families=signal_families,
        documents=documents,
        documents_upload_href="/intake" if can_upload_document else None,
        financial_statements_href=(
            f"/financial-statements?borrower_ref={borrower.reference}"
            if can_ingest_financial_statements
            else None
        ),
        actions=actions,
    )


# The descriptive alias is useful to callers that use the contract's name.
build_case_file_view = build_borrower_view


def _latest_complete_run(
    session: Session,
    borrower_id: UUID,
    scope: Scope,
) -> ForecastRun | None:
    return ForecastRepository(session).latest_complete_run_for_borrower(
        borrower_id,
        scope=scope,
    )


def _latest_triage_entry(
    session: Session,
    borrower_id: UUID,
    run: ForecastRun | None,
    scope: Scope,
) -> TriageEntry | None:
    if run is None:
        return None
    statement = (
        select(TriageEntry)
        .join(Borrower, Borrower.id == TriageEntry.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            TriageEntry.run_id == run.id,
            TriageEntry.borrower_id == borrower_id,
            scope.predicate(Portfolio.path),
        )
        .order_by(TriageEntry.rank, TriageEntry.id)
        .limit(1)
    )
    return session.execute(statement).scalars().one_or_none()


def _covenant_records(
    session: Session,
    borrower_id: UUID,
    scope: Scope,
) -> tuple[tuple[Covenant, Facility, CovenantVersion | None], ...]:
    """Fetch all active borrower covenants and their available versions."""
    statement = (
        select(Covenant, Facility, CovenantVersion)
        .select_from(Covenant)
        .join(Facility, Facility.id == Covenant.facility_id)
        .join(Borrower, Borrower.id == Facility.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .outerjoin(CovenantVersion, CovenantVersion.covenant_id == Covenant.id)
        .where(
            Borrower.id == borrower_id,
            Covenant.is_active.is_(True),
            scope.predicate(Portfolio.path),
        )
        .order_by(Covenant.reference, CovenantVersion.version_no, CovenantVersion.id)
    )
    return tuple(session.execute(statement).tuples().all())


def _select_versions(
    records: Sequence[tuple[Covenant, Facility, CovenantVersion | None]],
) -> tuple[tuple[Covenant, Facility, CovenantVersion | None], ...]:
    grouped: dict[UUID, list[tuple[Covenant, Facility, CovenantVersion | None]]] = {}
    for record in records:
        grouped.setdefault(record[0].id, []).append(record)

    selected: list[tuple[Covenant, Facility, CovenantVersion | None]] = []
    for covenant_records in grouped.values():
        versions = [
            cast(tuple[Covenant, Facility, CovenantVersion], record)
            for record in covenant_records
            if record[2] is not None
        ]
        live = [record for record in versions if record[2].status == "live"]
        candidates = live or versions
        if candidates:
            selected.append(
                max(
                    candidates,
                    key=lambda record: (record[2].version_no, str(record[2].id)),
                )
            )
        else:
            # The covenant identity remains visible even if registration is
            # incomplete, so the screen can explain the missing version.
            selected.append(covenant_records[0])
    return tuple(sorted(selected, key=lambda record: (record[0].reference, str(record[0].id))))


def _latest_tests(
    session: Session,
    version_ids: Sequence[UUID],
    scope: Scope,
) -> dict[UUID, CovenantTest]:
    if not version_ids:
        return {}
    statement = _scoped_select(CovenantTest, scope).where(
        CovenantTest.covenant_version_id.in_(version_ids)
    )
    statement = statement.order_by(
        CovenantTest.covenant_version_id,
        CovenantTest.as_of_date.desc(),
        CovenantTest.computed_at.desc(),
        CovenantTest.id.desc(),
    )
    latest: dict[UUID, CovenantTest] = {}
    for test in session.execute(statement).scalars().all():
        latest.setdefault(test.covenant_version_id, test)
    return latest


def _next_schedules(
    session: Session,
    version_ids: Sequence[UUID],
    scope: Scope,
    *,
    anchor: date | None,
) -> dict[UUID, CovenantSchedule]:
    if not version_ids:
        return {}
    statement = _scoped_select(CovenantSchedule, scope).where(
        CovenantSchedule.covenant_version_id.in_(version_ids)
    )
    statement = statement.order_by(
        CovenantSchedule.covenant_version_id,
        CovenantSchedule.due_date,
        CovenantSchedule.id,
    )
    grouped: dict[UUID, list[CovenantSchedule]] = {}
    for schedule in session.execute(statement).scalars().all():
        grouped.setdefault(schedule.covenant_version_id, []).append(schedule)

    result: dict[UUID, CovenantSchedule] = {}
    for version_id, schedules in grouped.items():
        candidates = (
            [schedule for schedule in schedules if anchor is None or schedule.due_date >= anchor]
            if anchor is not None
            else schedules
        )
        if candidates:
            result[version_id] = candidates[0]
    return result


def _forecasts_for_risk(
    session: Session,
    triage: TriageEntry | None,
    run: ForecastRun | None,
    scope: Scope,
) -> Forecast | None:
    if triage is None or run is None:
        return None
    if triage.worst_covenant_version_id is None or triage.worst_horizon is None:
        return None
    statement = _scoped_select(Forecast, scope).where(
        Forecast.run_id == run.id,
        Forecast.covenant_version_id == triage.worst_covenant_version_id,
        Forecast.horizon_days == triage.worst_horizon,
    )
    return session.execute(statement).scalars().one_or_none()


def _latest_case_reference(session: Session, borrower_id: UUID, scope: Scope) -> str | None:
    """The borrower's most recent case, open ones first.

    An action is logged against a case, so the borrower workspace's control
    needs one to point at.  Open cases sort ahead of closed ones so the
    control lands on the case a user is actually working, and the query stays
    inside the caller's portfolio scope like every other read on this screen.
    """
    statement = (
        _scoped_select(Case, scope)
        .where(Case.borrower_id == borrower_id)
        .order_by(
            (Case.state == "closed").asc(),
            Case.created_at.desc(),
            Case.id.desc(),
        )
        .limit(1)
    )
    case = session.execute(statement).scalars().first()
    return case.reference if case is not None else None


def _case_actions(
    borrower: Borrower,
    worst_forecast: Forecast | None,
    *,
    can_run_simulation: bool,
    can_generate_memo: bool,
    can_log_action: bool,
    case_reference: str | None = None,
) -> CaseActionsView:
    """Build the case action row from the caller's permissions.

    "Why" needs no separate grant: reaching this screen already required
    `Permission.VIEW_BORROWER`, the same permission `why.py` requires, so it
    is always available here. The other three are constructed only when the
    caller holds the matching permission, so a role that may not take an
    action never receives the control that would let it.
    """
    return CaseActionsView(
        why_href=f"/why/borrower/{borrower.id}",
        can_simulate=can_run_simulation,
        simulator_href=(
            f"/simulator/{worst_forecast.id}"
            if can_run_simulation and worst_forecast is not None
            else None
        ),
        simulate_reason="" if worst_forecast is not None else NO_SIMULATION_FORECAST,
        can_generate_memo=can_generate_memo,
        # The C-08 action target, posted to with the borrower reference — not
        # a page to navigate to. A memo is drafted on demand; there is no
        # memo to link to until one has been.
        memo_href=("/memos" if can_generate_memo and worst_forecast is not None else None),
        memo_reason="" if worst_forecast is not None else NO_MEMO_FORECAST,
        can_log_action=can_log_action,
        # The case workspace owns the intervention catalogue and the
        # append-only action log; this control takes the caller straight to
        # it rather than duplicating that form on a second screen.
        log_href=(
            f"/cases/{quote(case_reference, safe='')}#case-actions"
            if can_log_action and case_reference
            else None
        ),
        log_reason="" if case_reference else LOG_ACTION_UNAVAILABLE,
    )


def _evidence_margin(
    session: Session,
    borrower_id: UUID,
    scope: Scope,
) -> tuple[EvidenceMarginView, ...]:
    # Family is the primary sort key so every item lands next to its group
    # in one pass; `_group_evidence_by_family` relies on that contiguity.
    statement = (
        _scoped_select(EvidenceItem, scope)
        .where(EvidenceItem.borrower_id == borrower_id)
        .order_by(
            EvidenceItem.family,
            EvidenceItem.counts_toward_pressure.desc(),
            EvidenceItem.last_seen.desc(),
            EvidenceItem.evidence_type,
            EvidenceItem.id,
        )
    )
    return tuple(_evidence_view(item) for item in session.execute(statement).scalars().all())


def _group_evidence_by_family(
    items: Sequence[EvidenceMarginView],
) -> tuple[EvidenceFamilyGroupView, ...]:
    """Group already-family-ordered rows without dropping or reordering any."""
    groups: list[EvidenceFamilyGroupView] = []
    current_family: str | None = None
    current_items: list[EvidenceMarginView] = []
    for item in items:
        if item.family != current_family:
            if current_items:
                assert current_family is not None
                groups.append(EvidenceFamilyGroupView(current_family, tuple(current_items)))
            current_family = item.family
            current_items = []
        current_items.append(item)
    if current_items:
        assert current_family is not None
        groups.append(EvidenceFamilyGroupView(current_family, tuple(current_items)))
    return tuple(groups)


_SIGNAL_LABELS: Final[Mapping[str, str]] = {
    "account_activity": "Account activity",
    "payment": "Payment behaviour",
    "utilisation": "Facility utilisation",
    "treasury": "Treasury flows",
    "concentration": "Concentration exposure",
    "industry": "Industry conditions",
    "news": "News deterioration",
}


def _signal_family_views(
    session: Session,
    borrower_id: UUID,
    scope: Scope,
    *,
    as_of_date: date | None,
) -> tuple[SignalFamilyView, ...]:
    """Build all seven signal cards from immutable, scoped raw events."""

    statement = (
        _scoped_select(SignalEvent, scope)
        .where(SignalEvent.borrower_id == borrower_id)
        .order_by(SignalEvent.family, SignalEvent.event_date, SignalEvent.id)
    )
    if as_of_date is not None:
        statement = statement.where(SignalEvent.event_date <= as_of_date)
    events = tuple(session.execute(statement).scalars().all())
    grouped: dict[str, list[SignalEvent]] = {family: [] for family in FAMILIES}
    for event in events:
        grouped.setdefault(event.family, []).append(event)

    views: list[SignalFamilyView] = []
    for family in FAMILIES:
        rows = grouped.get(family, [])
        values = tuple(
            value for value in (_signal_value(event, family) for event in rows) if value is not None
        )
        latest_date = max((event.event_date for event in rows), default=None)
        current = values[-1] if values else None
        prior = values[-min(len(values), 8)] if values else None
        trend = _signal_trend(current, prior)
        adverse_count = sum(
            1 for event in rows if bool((event.payload or {}).get("is_adverse", False))
        )
        sparkline = None
        if len(values) >= 2:
            points = tuple(
                TrajectoryPoint(day=index, value=value) for index, value in enumerate(values[-24:])
            )
            try:
                threshold = max(values) if max(values) != 0 else Decimal("1")
                sparkline = render_trajectory_sparkline_svg(
                    f"signal-{borrower_id}-{family}",
                    points,
                    threshold,
                    label=f"{_SIGNAL_LABELS.get(family, family)} trend",
                )
            except (TypeError, ValueError, InvalidOperation):
                sparkline = None
        views.append(
            SignalFamilyView(
                family=family,
                label=_SIGNAL_LABELS.get(family, family.replace("_", " ").title()),
                event_count=len(rows),
                adverse_count=adverse_count,
                latest_date=latest_date,
                current_display=_signal_display(current, family),
                prior_display=_signal_display(prior, family),
                trend_display=trend,
                status="adverse" if adverse_count else "normal",
                sparkline_svg=sparkline,
                why_href=f"/why/borrower/{borrower_id}",
            )
        )
    return tuple(views)


def _signal_value(event: SignalEvent, family: str) -> Decimal | None:
    value_field = definition_for(family).value_field
    value = (event.payload or {}).get(value_field)
    if value is None:
        value = event.magnitude
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _signal_trend(current: Decimal | None, prior: Decimal | None) -> str:
    if current is None or prior is None:
        return "Awaiting comparison"
    delta = current - prior
    if abs(delta) <= Decimal("0.001"):
        return "Stable"
    return "Worsening" if delta > 0 else "Improving"


def _signal_display(value: Decimal | None, family: str) -> str:
    if value is None:
        return "No observations"
    unit = definition_for(family).unit
    if unit == "days":
        return f"{value.quantize(Decimal('0.1'))} days"
    if unit == "ratio":
        return f"{value.quantize(Decimal('0.001'))}x"
    if unit == "score":
        return str(value.quantize(Decimal("0.001")))
    return f"{value.quantize(Decimal('0.1'))}%"


def _evidence_view(item: EvidenceItem) -> EvidenceMarginView:
    persistence = (
        f"{item.persistence_days} days / {item.event_count_window or 0} events"
        if item.persistence_days is not None
        else "Persistence has not been calculated."
    )
    materiality = (
        f"{format(item.materiality_pct, 'f')}%"
        if item.materiality_pct is not None
        else "Materiality not recorded"
    )
    decay_state = _evidence_decay_state(item.decay_factor)
    decay = (
        f"{_DECAY_STATE_LABELS[decay_state]} — retained weight {format(item.decay_factor, 'f')}"
        if item.decay_factor is not None
        else _DECAY_STATE_LABELS[decay_state]
    )
    return EvidenceMarginView(
        id=item.id,
        family=item.family.replace("_", " ").title(),
        evidence_type=item.evidence_type.replace("_", " ").title(),
        first_seen=item.first_seen,
        last_seen=item.last_seen,
        persistence_display=persistence,
        materiality_display=materiality,
        decay_display=decay,
        decay_state=decay_state,
        state=item.state,
        counts_toward_pressure=item.counts_toward_pressure,
        anchor_id=f"evidence-item-{item.id}",
        superseded_by_id=item.superseded_by_id,
        supersedes_id=item.supersedes_id,
        not_counting_reason=_evidence_pressure_reason(item),
    )


def _evidence_decay_state(decay_factor: Decimal | None) -> EvidenceDecayState:
    """Classify a stored decay factor for display, per `DecayScore.decay_state`.

    This mirrors `domain.signals.decay.DecayScore.decay_state` without
    invoking that stage: the factor is already persisted on the item, so
    this is a display classification of a stored value, not a recomputation.
    """
    if decay_factor is None:
        return "not_recorded"
    if decay_factor == Decimal("1"):
        return "fresh"
    if decay_factor == Decimal("0"):
        return "decayed"
    return "decaying"


def _evidence_pressure_reason(item: EvidenceItem) -> str | None:
    """State why an item does not count toward pressure, from stored facts only."""
    if item.counts_toward_pressure:
        return None
    if item.state == "superseded":
        return "Superseded by a later item; the successor carries pressure instead."
    if item.state == "disputed":
        return "Marked disputed; disputed evidence does not count toward pressure."
    if item.materiality_pct is None:
        return "Materiality has not been scored for this item."
    if item.materiality_pct == Decimal("0"):
        return "No projected headroom erosion was attributed to this item."
    return "Recorded materiality did not meet the pressure threshold on its last scored run."


def _document_strip(
    session: Session,
    borrower_id: UUID,
    scope: Scope,
) -> tuple[DocumentStripView, ...]:
    statement = (
        _scoped_select(Document, scope)
        .where(Document.borrower_id == borrower_id)
        .order_by(Document.created_at.desc(), Document.id.desc())
        .limit(12)
    )
    return tuple(
        DocumentStripView(
            id=document.id,
            filename=document.filename,
            doc_type=document.doc_type.replace("_", " ").title(),
            uploaded_on=document.created_at.date(),
            extraction_state=document.extraction_state.replace("_", " "),
            href=f"/documents/{document.id}/view",
        )
        for document in session.execute(statement).scalars().all()
    )


def _forecasts_for_covenants(
    session: Session,
    version_ids: Sequence[UUID],
    run: ForecastRun | None,
    scope: Scope,
) -> dict[tuple[UUID, int], Forecast]:
    if run is None or not version_ids:
        return {}
    statement = _scoped_select(Forecast, scope).where(
        Forecast.run_id == run.id,
        Forecast.covenant_version_id.in_(version_ids),
    )
    statement = statement.order_by(
        Forecast.covenant_version_id,
        Forecast.horizon_days,
        Forecast.id,
    )
    return {
        (forecast.covenant_version_id, forecast.horizon_days): forecast
        for forecast in session.execute(statement).scalars().all()
    }


def _paths_for_covenants(
    session: Session,
    version_ids: Sequence[UUID],
    run: ForecastRun | None,
    scope: Scope,
) -> dict[UUID, tuple[ForecastPath, ...]]:
    if run is None or not version_ids:
        return {}
    statement = _scoped_select(ForecastPath, scope).where(
        ForecastPath.run_id == run.id,
        ForecastPath.covenant_version_id.in_(version_ids),
    )
    statement = statement.order_by(ForecastPath.covenant_version_id, ForecastPath.day_offset)
    grouped: dict[UUID, list[ForecastPath]] = {}
    for path in session.execute(statement).scalars().all():
        grouped.setdefault(path.covenant_version_id, []).append(path)
    return {version_id: tuple(rows) for version_id, rows in grouped.items()}


def _forecast_drivers(
    session: Session,
    forecasts: Mapping[tuple[UUID, int], Forecast],
    scope: Scope,
) -> dict[UUID, tuple[ForecastDriver, ...]]:
    """Read attributed forecast drivers in one scoped batch."""

    forecast_ids = tuple(sorted({forecast.id for forecast in forecasts.values()}, key=str))
    if not forecast_ids:
        return {}
    statement = _scoped_select(ForecastDriver, scope).where(
        ForecastDriver.forecast_id.in_(forecast_ids)
    )
    statement = statement.order_by(
        ForecastDriver.forecast_id,
        ForecastDriver.share.desc(),
        ForecastDriver.name,
        ForecastDriver.id,
    )
    grouped: dict[UUID, list[ForecastDriver]] = {}
    for driver in session.execute(statement).scalars().all():
        grouped.setdefault(driver.forecast_id, []).append(driver)
    return {forecast_id: tuple(rows) for forecast_id, rows in grouped.items()}


def _active_interventions(session: Session) -> tuple[Intervention, ...]:
    """Read the active, bank-owned action catalogue in stable order."""

    statement = (
        select(Intervention)
        .where(Intervention.is_active.is_(True))
        .order_by(Intervention.role_tag, Intervention.code, Intervention.id)
    )
    return tuple(session.execute(statement).scalars().all())


def _forecast_panel(
    selected_versions: Sequence[tuple[Covenant, Facility, CovenantVersion | None]],
    forecasts: Mapping[tuple[UUID, int], Forecast],
    paths: Mapping[UUID, Sequence[ForecastPath]],
    run: ForecastRun | None,
    *,
    forecast_drivers: Mapping[UUID, Sequence[ForecastDriver]] | None = None,
    interventions: Sequence[Intervention] = (),
    can_run_simulation: bool = False,
    header_version_id: UUID | None = None,
) -> ForecastPanelView:
    cards = tuple(
        _forecast_covenant_view(
            covenant,
            facility,
            version,
            forecasts,
            paths,
            run,
            forecast_drivers=forecast_drivers or {},
            interventions=interventions,
            can_run_simulation=can_run_simulation,
            updates_header=version is not None and version.id == header_version_id,
        )
        for covenant, facility, version in selected_versions
    )
    return ForecastPanelView(
        state="rest" if cards else "empty",
        title="Forecast trajectory",
        covenants=cards,
        empty_title="No forecast available",
        empty_message=NO_FORECAST_PANEL,
    )


def _forecast_covenant_view(
    covenant: Covenant,
    facility: Facility,
    version: CovenantVersion | None,
    forecasts: Mapping[tuple[UUID, int], Forecast],
    paths: Mapping[UUID, Sequence[ForecastPath]],
    run: ForecastRun | None,
    *,
    forecast_drivers: Mapping[UUID, Sequence[ForecastDriver]],
    interventions: Sequence[Intervention],
    can_run_simulation: bool,
    updates_header: bool = False,
) -> ForecastCovenantView:
    direction = version.direction if version is not None else "max"
    direction_label = _direction_label(direction)
    action_forecast = _action_forecast(forecasts, version.id if version is not None else None)
    horizon_views_list: list[ForecastHorizonView] = []
    for horizon in FORECAST_HORIZONS:
        forecast = forecasts.get((version.id, horizon)) if version is not None else None
        horizon_views_list.append(
            _forecast_horizon_view(
                forecast,
                horizon,
                direction_label,
                drivers=(forecast_drivers.get(forecast.id, ()) if forecast is not None else ()),
                is_primary=(
                    forecast is not None
                    and action_forecast is not None
                    and forecast.id == action_forecast.id
                ),
            )
        )
    horizon_views = tuple(horizon_views_list)
    threshold = version.threshold if version is not None else None
    unit = version.unit if version is not None else ""
    ledger_figures = _trajectory_ledger_figures(threshold, unit, horizon_views)
    path_rows = tuple(paths.get(version.id, ())) if version is not None else ()
    path_points = _trajectory_points(path_rows)
    trajectory_forecast = _trajectory_forecast(
        forecasts,
        version.id if version is not None else None,
        path_rows,
    )
    trajectory_driver = (
        _dominant_driver(forecast_drivers.get(trajectory_forecast.id, ()))
        if trajectory_forecast is not None
        else None
    )
    crossing = _trajectory_crossing(
        trajectory_forecast,
        run,
        driver_label=(f"Dominant driver: {trajectory_driver}" if trajectory_driver else ""),
    )
    trajectory_id = f"forecast-trajectory-{covenant.id}"
    trajectory_svg = render_trajectory_svg(
        trajectory_id,
        path_points,
        threshold if threshold is not None else Decimal("0"),
        ledger_figures,
        crossing=crossing,
        label=f"{covenant.name} trajectory",
    )
    trajectory_available = (
        trajectory_forecast is not None and len(path_points) >= 2 and threshold is not None
    )
    if trajectory_available:
        trajectory_message = "Stored daily path rendered."
    elif trajectory_forecast is None:
        trajectory_message = NO_FORECAST_TRAJECTORY
    else:
        trajectory_message = NO_FORECAST_PATH
    maximum_day = (
        path_rows[-1].day_offset
        if path_rows
        else max(
            (forecast.horizon_days for forecast in forecasts.values() if version is not None),
            default=max(FORECAST_HORIZONS),
        )
    )
    actionable_insights = _actionable_insights(
        interventions,
        covenant_class=covenant.covenant_class,
        forecast=action_forecast,
        can_run_simulation=can_run_simulation,
    )
    return ForecastCovenantView(
        row_id=f"forecast-covenant-{covenant.id}",
        covenant_reference=covenant.reference,
        covenant_name=covenant.name,
        facility_reference=facility.reference,
        threshold=threshold,
        unit=unit,
        horizons=horizon_views,
        ledger_figures=ledger_figures,
        trajectory_id=trajectory_id,
        trajectory_svg=trajectory_svg,
        trajectory_available=trajectory_available,
        trajectory_message=trajectory_message,
        maximum_day=maximum_day,
        as_of_date=run.as_of_date if run is not None else None,
        updates_header=updates_header,
        trajectory_driver=trajectory_driver,
        actionable_insights=actionable_insights,
    )


def _forecast_horizon_view(
    forecast: Forecast | None,
    horizon: int,
    direction_label: str,
    *,
    drivers: Sequence[ForecastDriver] = (),
    is_primary: bool = False,
) -> ForecastHorizonView:
    label = f"{horizon}-day"
    if forecast is None:
        return ForecastHorizonView(
            horizon_days=horizon,
            label=label,
            state="unavailable",
            probability=None,
            confidence=None,
            crossing_date=None,
            probability_display=NO_FORECAST_FOR_HORIZON,
            confidence_display=NO_CONFIDENCE,
            crossing_display=NO_FORECAST_FOR_HORIZON,
            direction_label=direction_label,
            reason=NO_FORECAST_FOR_HORIZON,
            forecast_id=None,
        )

    reason = _forecast_reason(forecast)
    if reason is not None:
        probability_display = f"{NOT_COMPUTABLE_PREFIX}{reason}"
        state: ForecastHorizonState = "not_computable"
    elif forecast.below_confidence_floor:
        limiting_factor = _limiting_factor(forecast)
        probability_display = (
            "Suppressed — confidence is below the display floor; "
            f"{limiting_factor} is the limiting factor."
        )
        state = "suppressed"
    elif forecast.probability is None:
        probability_display = NO_PROBABILITY
        state = "unavailable"
    else:
        probability_display = _probability_display(forecast.probability)
        state = "available"

    return ForecastHorizonView(
        horizon_days=horizon,
        label=label,
        state=state,
        probability=forecast.probability if state == "available" else None,
        confidence=forecast.confidence,
        crossing_date=forecast.projected_cross_date,
        probability_display=probability_display,
        confidence_display=(
            _probability_display(forecast.confidence)
            if forecast.confidence is not None
            else NO_CONFIDENCE
        ),
        crossing_display=_crossing_display(forecast, horizon, direction_label),
        direction_label=direction_label,
        limiting_factor=_limiting_factor(forecast) if state == "suppressed" else None,
        reason=reason,
        forecast_id=forecast.id,
        explanation=_forecast_explanation(forecast, drivers),
        is_primary=is_primary,
    )


def _forecast_explanation(
    forecast: Forecast,
    drivers: Sequence[ForecastDriver],
) -> ForecastExplanationView:
    formula = forecast.formula_inputs if isinstance(forecast.formula_inputs, Mapping) else {}
    raw_source = forecast.probability_source or formula.get("probability_source")
    source = str(raw_source).strip().lower() if raw_source is not None else "deterministic"
    probability_formula = _nested_mapping(formula, "probability")
    ml_prediction = _nested_mapping(formula, "ml_prediction")
    predictor_mode = _clean_text(formula.get("predictor_mode"))

    deterministic_probability = _as_decimal(probability_formula.get("probability"))
    if deterministic_probability is None and source == "deterministic":
        deterministic_probability = forecast.probability
    ml_probability = _as_decimal(ml_prediction.get("probability"))
    if ml_probability is None:
        ml_probability = _as_decimal(formula.get("challenger_probability"))
    if ml_probability is None and source == "ml":
        ml_probability = forecast.probability

    model_version = _clean_text(ml_prediction.get("model_version"))
    checksum = _clean_text(ml_prediction.get("artifact_checksum"))
    feature_hash = _clean_text(formula.get("feature_snapshot_hash"))
    fallback_reason = forecast.fallback_reason or _clean_text(formula.get("fallback_reason"))
    governance_warning: str | None = None

    if source == "ml" and predictor_mode == "champion":
        method = "ml_champion"
        method_label = "Governed ML champion"
        ml_role = "Operational — this model supplied the displayed probability."
    elif source == "ml":
        method = "ml_operational"
        method_label = "ML operational probability"
        ml_role = "Operational — this model supplied the displayed probability."
        governance_warning = (
            "This stored forecast does not record an approved champion mode. "
            "Review its model-register approval before using it for a credit workflow."
        )
    elif ml_prediction:
        method = "deterministic_with_challenger"
        method_label = "Deterministic rule with ML challenger"
        ml_role = "Shadow comparison — it did not affect the displayed risk band or case."
    elif source == "deterministic":
        method = "deterministic"
        method_label = "Deterministic forecast rule"
        ml_role = None
    else:
        method = "recorded_unknown"
        method_label = "Recorded forecast method"
        ml_role = None
        governance_warning = (
            f"The stored probability source {source!r} is not recognised; review the run trace."
        )

    factor_views = _forecast_factor_views(probability_formula)
    driver_views = _forecast_driver_views(drivers)
    contribution_views = _model_contribution_views(ml_prediction.get("contributions"))
    citations = _forecast_citations(forecast, driver_views, formula)
    summary = _prediction_summary(
        forecast,
        source=source,
        method_label=method_label,
        model_version=model_version,
        probability_formula=probability_formula,
    )
    rationale = _prediction_rationale(forecast, driver_views)
    provenance = (
        f"Assembled from stored forecast {forecast.id}, its saved formula inputs and "
        f"{len(driver_views)} attribution record{'s' if len(driver_views) != 1 else ''}. "
        "An LLM did not calculate or rewrite this explanation; when an AI memo is requested, "
        "the TCS-backed stage-7 draft is separately labelled and shape-checked."
    )
    return ForecastExplanationView(
        method=method,
        method_label=method_label,
        operational_label=(
            "Suppressed — not used as a displayed probability"
            if forecast.below_confidence_floor
            else "Operational — used by the current risk view"
        ),
        summary=summary,
        rationale=rationale,
        provenance_statement=provenance,
        rule_version=(
            _clean_text(formula.get("scoring_rule_version"))
            or _clean_text(formula.get("trace_rule_version"))
        ),
        deterministic_probability_display=(
            _probability_display(deterministic_probability)
            if deterministic_probability is not None
            else None
        ),
        ml_probability_display=(
            _probability_display(ml_probability) if ml_probability is not None else None
        ),
        ml_role=ml_role,
        ml_model_version=model_version,
        artifact_checksum=checksum,
        feature_snapshot_hash=feature_hash,
        fallback_reason=fallback_reason,
        governance_warning=governance_warning,
        factors=factor_views,
        drivers=driver_views,
        model_contributions=contribution_views,
        citations=citations,
    )


def _forecast_factor_views(
    probability_formula: Mapping[str, object],
) -> tuple[ForecastFactorView, ...]:
    terms = probability_formula.get("terms")
    if not isinstance(terms, Mapping):
        return ()
    result: list[ForecastFactorView] = []
    for name in ("distance", "velocity", "pressure"):
        raw = terms.get(name)
        if not isinstance(raw, Mapping):
            continue
        result.append(
            ForecastFactorView(
                name=name,
                label=_FACTOR_LABELS[name],
                input_display=_number_display(raw.get("input_value")),
                normalized_display=_fraction_or_number_display(raw.get("normalized_value")),
                weight_display=_number_display(raw.get("weight")),
                contribution_display=_fraction_or_number_display(raw.get("contribution")),
            )
        )
    return tuple(result)


def _forecast_driver_views(
    drivers: Sequence[ForecastDriver],
) -> tuple[ForecastDriverView, ...]:
    result: list[ForecastDriverView] = []
    for driver in drivers:
        name = driver.name.strip()
        if not name:
            continue
        evidence_id = driver.evidence_id
        result.append(
            ForecastDriverView(
                name=name,
                label=_human_label(name, _DRIVER_LABELS),
                share=driver.share,
                share_display=_probability_display(driver.share),
                evidence_id=evidence_id,
                evidence_href=(f"#evidence-item-{evidence_id}" if evidence_id is not None else ""),
                is_other=driver.is_other,
            )
        )
    return tuple(result)


def _model_contribution_views(raw: object) -> tuple[ModelContributionView, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes | bytearray):
        return ()
    result: list[ModelContributionView] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = _clean_text(item.get("name"))
        value = _as_decimal(item.get("value"))
        if name is None or value is None:
            continue
        direction = "increases risk" if value > 0 else "reduces risk" if value < 0 else "neutral"
        result.append(
            ModelContributionView(
                name=name,
                label=_human_label(name),
                value_display=_number_display(value, signed=True),
                direction=direction,
            )
        )
    return tuple(result)


def _forecast_citations(
    forecast: Forecast,
    drivers: Sequence[ForecastDriverView],
    formula: Mapping[str, object],
) -> tuple[ForecastCitationView, ...]:
    result = [
        ForecastCitationView(
            label=f"Forecast record — {forecast.horizon_days}-day horizon",
            href=f"/why/forecast/{forecast.id}",
            source_type="forecast",
        )
    ]
    seen: set[UUID] = set()
    for driver in drivers:
        if driver.evidence_id is None or driver.evidence_id in seen:
            continue
        seen.add(driver.evidence_id)
        result.append(
            ForecastCitationView(
                label=f"Evidence — {driver.label}",
                href=driver.evidence_href,
                source_type="evidence",
            )
        )
    candidate_inputs = _nested_mapping(formula, "candidate_inputs")
    evidence_ids = candidate_inputs.get("evidence_ids")
    families = candidate_inputs.get("signal_families")
    family_values = (
        tuple(families)
        if isinstance(families, Sequence) and not isinstance(families, str | bytes | bytearray)
        else ()
    )
    if isinstance(evidence_ids, Sequence) and not isinstance(
        evidence_ids, str | bytes | bytearray
    ):
        for index, raw_id in enumerate(evidence_ids):
            try:
                evidence_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
            except (TypeError, ValueError):
                continue
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            raw_family = family_values[index] if index < len(family_values) else "evidence"
            family = _human_label(str(raw_family))
            result.append(
                ForecastCitationView(
                    label=f"Evidence input — {family}",
                    href=f"#evidence-item-{evidence_id}",
                    source_type="evidence",
                )
            )
    return tuple(result)


def _prediction_summary(
    forecast: Forecast,
    *,
    source: str,
    method_label: str,
    model_version: str | None,
    probability_formula: Mapping[str, object],
) -> str:
    displayed = (
        _probability_display(forecast.probability)
        if forecast.probability is not None and not forecast.below_confidence_floor
        else "a suppressed probability"
    )
    if source == "ml":
        model = model_version or "the recorded statistical artifact"
        return (
            f"{method_label} {model} supplied {displayed} for the "
            f"{forecast.horizon_days}-day horizon. The dated crossing still comes from the "
            "stored covenant trajectory; the model does not alter source covenant facts."
        )
    distance = _number_display(probability_formula.get("distance"))
    velocity = _number_display(probability_formula.get("velocity"))
    pressure = _number_display(probability_formula.get("pressure"))
    return (
        f"{method_label} supplied {displayed} for the {forecast.horizon_days}-day horizon by "
        f"combining threshold distance {distance}, deterioration velocity {velocity}, and "
        f"sustained-evidence pressure {pressure}."
    )


def _prediction_rationale(
    forecast: Forecast,
    drivers: Sequence[ForecastDriverView],
) -> str:
    named = tuple(driver for driver in drivers if not driver.is_other and driver.name != "neutral")
    if named:
        dominant = max(named, key=lambda driver: (driver.share, driver.name))
        driver_text = (
            f"The largest attributed driver is {dominant.label} at {dominant.share_display}."
        )
    elif drivers:
        driver_text = "The attribution is retained in residual or neutral driver records."
    else:
        driver_text = "No separate attribution row was stored for this forecast."
    confidence = (
        _probability_display(forecast.confidence)
        if forecast.confidence is not None
        else "not recorded"
    )
    crossing = (
        format_ist_date(forecast.projected_cross_date)
        if forecast.projected_cross_date is not None
        else f"not projected within {forecast.horizon_days} days"
    )
    return f"{driver_text} Confidence is {confidence}; threshold crossing is {crossing}."


def _action_forecast(
    forecasts: Mapping[tuple[UUID, int], Forecast],
    version_id: UUID | None,
) -> Forecast | None:
    if version_id is None:
        return None
    candidates = [
        forecast
        for (candidate_id, _), forecast in forecasts.items()
        if candidate_id == version_id
    ]
    displayable = [
        forecast
        for forecast in candidates
        if forecast.probability is not None and not forecast.below_confidence_floor
    ]
    if displayable:
        return max(
            displayable,
            key=lambda row: (row.probability or Decimal("-1"), -row.horizon_days, str(row.id)),
        )
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row.horizon_days, str(row.id)))


def _actionable_insights(
    interventions: Sequence[Intervention],
    *,
    covenant_class: str,
    forecast: Forecast | None,
    can_run_simulation: bool,
) -> tuple[ActionableInsightView, ...]:
    result: list[ActionableInsightView] = []
    for intervention in interventions:
        classes = intervention.applicable_covenant_classes
        if not classes:
            applicable = True
            applicability = "all covenant classes"
        else:
            try:
                applicable = is_applicable(classes, covenant_class)
            except (TypeError, ValueError):
                # Invalid reference configuration is never broadened into a
                # recommendation. The administration screen remains the place
                # to repair it.
                continue
            applicability = f"{covenant_class.replace('_', ' ')} covenants"
        if not applicable or not intervention.role_tag:
            continue
        assumptions = _intervention_assumptions(intervention.effect_parameters)
        result.append(
            ActionableInsightView(
                code=intervention.code,
                role_label=_ROLE_LABELS.get(
                    intervention.role_tag,
                    _human_label(intervention.role_tag),
                ),
                text=intervention.text,
                effect_model=_human_label(intervention.effect_model),
                applicability_reason=(
                    f"Shown because bank catalogue action {intervention.code} is active and "
                    f"applicable to {applicability}."
                ),
                assumptions=assumptions,
                requires_approval=intervention.requires_approval,
                simulator_href=(
                    f"/simulator/{forecast.id}?"
                    f"intervention_code={quote(intervention.code, safe='')}"
                    if can_run_simulation and forecast is not None
                    else None
                ),
            )
        )
    return tuple(result)


def _intervention_assumptions(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Mapping):
        return ()
    values = raw.get("_assumptions", raw.get("assumptions"))
    if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
        return ()
    return tuple(
        value.strip() for value in values if isinstance(value, str) and value.strip()
    )


def _nested_mapping(values: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = values.get(key)
    if not isinstance(value, Mapping):
        return {}
    return {str(nested_key): nested_value for nested_key, nested_value in value.items()}


def _clean_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _number_display(value: object, *, signed: bool = False) -> str:
    number = _as_decimal(value)
    if number is None:
        return "not recorded"
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if signed and number > 0:
        return f"+{text}"
    return text


def _fraction_or_number_display(value: object) -> str:
    number = _as_decimal(value)
    if number is None:
        return "not recorded"
    if Decimal("-1") <= number <= Decimal("1"):
        return _probability_display(number)
    return _number_display(number)


def _human_label(value: str, labels: Mapping[str, str] | None = None) -> str:
    normalized = value.strip().lower()
    if labels is not None and normalized in labels:
        return labels[normalized]
    return normalized.replace("_", " ").replace("-", " ").title()


def _trajectory_ledger_figures(
    threshold: Decimal | None,
    unit: str,
    horizons: Sequence[ForecastHorizonView],
) -> tuple[TrajectoryLedgerFigure, ...]:
    figures = [
        TrajectoryLedgerFigure(
            "Threshold in force",
            _number_with_unit(threshold, unit) if threshold is not None else NO_THRESHOLD,
        )
    ]
    figures.extend(
        TrajectoryLedgerFigure(
            horizon.label,
            horizon.probability_display,
            f"Confidence: {horizon.confidence_display}; Crossing: {horizon.crossing_display}",
        )
        for horizon in horizons
    )
    return tuple(figures)


def _trajectory_points(path_rows: Sequence[ForecastPath]) -> tuple[TrajectoryPoint, ...]:
    if not path_rows or any(row.projected_value is None for row in path_rows):
        return ()
    try:
        points = tuple(
            TrajectoryPoint(day=row.day_offset, value=row.projected_value)
            for row in path_rows
            if row.projected_value is not None
        )
    except (TypeError, ValueError):
        return ()
    if any(
        current.day <= previous.day for previous, current in zip(points, points[1:], strict=False)
    ):
        return ()
    return points


def _trajectory_forecast(
    forecasts: Mapping[tuple[UUID, int], Forecast],
    version_id: UUID | None,
    path_rows: Sequence[ForecastPath],
) -> Forecast | None:
    if version_id is None:
        return None
    path_horizon = path_rows[-1].day_offset if path_rows else None
    if path_horizon is not None:
        matching = forecasts.get((version_id, path_horizon))
        if matching is not None:
            return matching
    candidates = [
        forecast for (candidate_id, _), forecast in forecasts.items() if candidate_id == version_id
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda forecast: (forecast.horizon_days, str(forecast.id)),
    )


def _trajectory_crossing(
    forecast: Forecast | None,
    run: ForecastRun | None,
    *,
    driver_label: str = "",
) -> TrajectoryCrossing | None:
    if forecast is None or forecast.projected_cross_date is None:
        return None
    crossing_day = _stored_crossing_day(forecast)
    if crossing_day is None:
        as_of_date = forecast.data_as_of or (run.as_of_date if run is not None else None)
        if as_of_date is None:
            return None
        crossing_day = max(0, (forecast.projected_cross_date - as_of_date).days)
    if crossing_day > forecast.horizon_days:
        return None
    return TrajectoryCrossing(
        day=crossing_day,
        date_label=format_ist_date(forecast.projected_cross_date),
        label=driver_label,
    )


def _dominant_driver(drivers: Sequence[ForecastDriver]) -> str | None:
    """Return the most material named driver, excluding residual buckets."""

    named = tuple(
        driver for driver in drivers if driver.name.strip().lower() not in {"other", "neutral"}
    )
    if not named:
        return None
    return max(named, key=lambda driver: (driver.share, driver.name)).name.strip()


def _stored_crossing_day(forecast: Forecast) -> int | None:
    inputs = forecast.formula_inputs
    if not isinstance(inputs, Mapping):
        return None
    crossing = inputs.get("crossing")
    if not isinstance(crossing, Mapping):
        return None
    value = crossing.get("crossing_day")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _crossing_display(forecast: Forecast, horizon: int, direction_label: str) -> str:
    if forecast.projected_cross_date is not None:
        return format_ist_date(forecast.projected_cross_date)
    return f"No projected crossing in {horizon} days; direction: {direction_label}."


def _direction_label(direction: str) -> str:
    if direction == "min":
        return "toward the minimum threshold"
    return "toward the maximum threshold"


def _forecast_reason(forecast: Forecast) -> str | None:
    inputs = forecast.formula_inputs
    if not isinstance(inputs, Mapping):
        return None
    for key in ("not_computable_reason", "reason"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if inputs.get("computable") is False:
        return "the required forecast inputs are unavailable"
    return None


def _limiting_factor(forecast: Forecast) -> str:
    inputs = forecast.formula_inputs
    candidates: list[Mapping[str, object]] = []
    if isinstance(inputs, Mapping):
        candidates.append(inputs)
        for key in ("confidence", "confidence_formula"):
            nested = inputs.get(key)
            if isinstance(nested, Mapping):
                candidates.append({str(name): value for name, value in nested.items()})
    for candidate in candidates:
        value = candidate.get("limiting_factor")
        if isinstance(value, str) and value.strip() and value.strip().lower() != "none":
            return value.strip()
    return "a confidence input"


def _header_view(
    borrower: Borrower,
    triage: TriageEntry | None,
    forecast: Forecast | None,
    covenant_records: Sequence[tuple[Covenant, Facility, CovenantVersion | None]],
) -> CaseFileHeaderView:
    versions_by_id = {
        version.id: (covenant, version)
        for covenant, _, version in covenant_records
        if version is not None
    }
    worst = (
        versions_by_id.get(triage.worst_covenant_version_id)
        if triage is not None and triage.worst_covenant_version_id is not None
        else None
    )
    worst_text = _covenant_label(*worst) if worst is not None else NO_WORST_COVENANT
    worst_missing = worst is None

    if triage is None or triage.exposure is None:
        exposure_text = NO_EXPOSURE
        exposure_missing = True
    else:
        exposure_text = format_indian_currency(triage.exposure)
        exposure_missing = False

    dated_risk_text = _dated_risk_text(triage, forecast)
    dated_risk_missing = triage is None or triage.worst_covenant_version_id is None
    facts = (
        HeaderFactView("name", "Borrower", borrower.legal_name, raw_value=borrower.legal_name),
        HeaderFactView(
            "exposure",
            "Exposure",
            exposure_text,
            missing=exposure_missing,
            raw_value=triage.exposure if triage is not None else None,
        ),
        HeaderFactView(
            "worst_covenant",
            "Worst covenant",
            worst_text,
            missing=worst_missing,
        ),
        HeaderFactView("dated_risk", "Dated risk", dated_risk_text, missing=dated_risk_missing),
    )
    return CaseFileHeaderView(
        borrower_name=borrower.legal_name,
        borrower_reference=borrower.reference,
        facts=facts,
    )


def _dated_risk_text(triage: TriageEntry | None, forecast: Forecast | None) -> str:
    if triage is None or triage.worst_covenant_version_id is None:
        return NO_DATED_RISK
    if triage.probability is None or (forecast is not None and forecast.below_confidence_floor):
        return SUPPRESSED_RISK
    probability = _probability_display(triage.probability)
    if forecast is None:
        return f"{probability} — crossing date unavailable in the stored forecast."
    if forecast.projected_cross_date is None:
        return f"{probability} — {NO_PROJECTED_CROSSING.lower()}"
    return f"{probability} by {format_ist_date(forecast.projected_cross_date)}"


def _covenant_row(
    covenant: Covenant,
    facility: Facility,
    version: CovenantVersion | None,
    tests: Mapping[UUID, CovenantTest],
    schedules: Mapping[UUID, CovenantSchedule],
) -> CovenantRowView:
    if version is None:
        return CovenantRowView(
            row_id=f"covenant-row-{covenant.id}",
            covenant_reference=covenant.reference,
            covenant_name=covenant.name,
            facility_reference=facility.reference,
            unit="",
            value=None,
            threshold=None,
            headroom_pct=None,
            verdict="not_tested",
            verdict_display="Not registered",
            next_test_date=None,
            trajectory_arrow="→",
            trajectory_label="No covenant version is available",
            value_display=NO_VALUE,
            threshold_display=NO_THRESHOLD,
            headroom_display=NO_HEADROOM,
            next_test_display=NO_SCHEDULE,
            status_message="Covenant terms are not available because no version is recorded.",
        )

    test = tests.get(version.id)
    schedule = schedules.get(version.id)
    threshold = (
        test.threshold_used
        if test is not None and test.threshold_used is not None
        else version.threshold
    )
    unit = version.unit
    verdict = test.verdict if test is not None else "not_tested"
    reason = _not_computable_reason(test)
    stale_period, confidence_reduction = _stale_details(test)
    detail_message = ""
    if verdict == "stale":
        detail_message = (
            f"Last complete period: {stale_period or 'not recorded in the test'}. "
            f"Confidence reduction: {confidence_reduction or 'not recorded in the test'}."
        )
    elif reason is not None:
        detail_message = f"Reason: {reason}."
    elif test is None:
        detail_message = NO_COMPLETED_TEST

    value_display = _test_value_display(test, unit, reason)
    headroom_display = _headroom_display(test, reason)
    threshold_display = (
        _number_with_unit(threshold, unit) if threshold is not None else NO_THRESHOLD
    )
    arrow = _DIRECTION_ARROWS.get(version.direction, "→")
    direction_label = (
        "toward the minimum threshold"
        if version.direction == "min"
        else "toward the maximum threshold"
    )
    return CovenantRowView(
        row_id=f"covenant-row-{covenant.id}",
        covenant_reference=covenant.reference,
        covenant_name=covenant.name,
        facility_reference=facility.reference,
        unit=unit,
        value=test.value if test is not None else None,
        threshold=threshold,
        headroom_pct=test.headroom_pct if test is not None else None,
        verdict=verdict,
        verdict_display=_VERDICT_LABELS.get(verdict, verdict.replace("_", " ").title()),
        next_test_date=schedule.due_date if schedule is not None else None,
        trajectory_arrow=arrow,
        trajectory_label=f"Trajectory {direction_label}",
        value_display=value_display,
        threshold_display=threshold_display,
        headroom_display=headroom_display,
        next_test_display=(
            format_ist_date(schedule.due_date) if schedule is not None else NO_SCHEDULE
        ),
        status_message=(
            detail_message
            or (f"Next test is {format_ist_date(schedule.due_date)}." if schedule else NO_SCHEDULE)
        ),
        detail_message=detail_message,
        not_computable_reason=reason,
        stale_period=stale_period,
        confidence_reduction=confidence_reduction,
    )


def _covenant_label(covenant: Covenant, version: CovenantVersion) -> str:
    symbol = _DIRECTION_SYMBOLS.get(version.direction, version.direction)
    return f"{covenant.name} {symbol} {_number_with_unit(version.threshold, version.unit)}"


def _test_value_display(
    test: CovenantTest | None,
    unit: str,
    reason: str | None,
) -> str:
    if reason is not None:
        return f"{NOT_COMPUTABLE_PREFIX}{reason}"
    if test is None:
        return NO_VALUE
    if test.value is None:
        return "Value unavailable — the completed test did not record a value."
    return _number_with_unit(test.value, unit)


def _headroom_display(test: CovenantTest | None, reason: str | None) -> str:
    if reason is not None:
        return f"{NOT_COMPUTABLE_PREFIX}{reason}"
    if test is None:
        return NO_HEADROOM
    if test.headroom_pct is None:
        return "Headroom unavailable — the completed test did not record headroom."
    return f"{format(test.headroom_pct, 'f')}%"


def _number_with_unit(value: Decimal, unit: str) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{rendered}{unit}"


def _probability_display(value: Decimal) -> str:
    percent = (value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{format(percent, 'f')}%"


def _not_computable_reason(test: CovenantTest | None) -> str | None:
    if test is None:
        return None
    if test.verdict != "not_computable" and not test.not_computable_reason:
        return None
    reason = (test.not_computable_reason or "the required inputs are unavailable").strip()
    return reason or "the required inputs are unavailable"


def _stale_details(test: CovenantTest | None) -> tuple[str | None, str | None]:
    if test is None or test.verdict != "stale":
        return None, None
    inputs = test.inputs if isinstance(test.inputs, Mapping) else {}
    sources: list[Mapping[str, object]] = [inputs]
    for nested_key in ("reason_context", "staleness"):
        nested = inputs.get(nested_key)
        if isinstance(nested, Mapping):
            sources.append({str(name): value for name, value in nested.items()})

    period_value: str | None = None
    reduction_key: str | None = None
    reduction_value: str | None = None
    for source in sources:
        if period_value is None:
            _, period_value = _first_input(source, _STALE_PERIOD_KEYS[:3])
        if reduction_value is None:
            reduction_key, reduction_value = _first_input(source, _STALE_REDUCTION_KEYS)

    if period_value is None:
        for source in sources:
            _, period_value = _first_input(source, ("period_label",))
            if period_value is not None:
                break

    if period_value is None:
        stale_reason = inputs.get("stale_reason")
        if isinstance(stale_reason, str):
            prefix = "last complete period:"
            candidate = stale_reason.strip()
            if candidate.lower().startswith(prefix):
                period_value = candidate[len(prefix) :].strip() or None
    period = period_value
    reduction = reduction_value
    if reduction is not None and reduction_key is not None and reduction_key.endswith("_pct"):
        if not reduction.endswith("%"):
            reduction = f"{reduction}%"
    return period, reduction


def _first_input(
    inputs: Mapping[str, object], keys: Sequence[str]
) -> tuple[str | None, str | None]:
    for key in keys:
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
        if isinstance(value, Decimal | int) and not isinstance(value, bool):
            return key, str(value)
    return None, None


def _scoped_select(model: type[Any], scope: Scope) -> Select[Any]:
    ownership = ownership_path_for(model)
    statement = select(model)
    statement = ownership.apply(statement)
    return statement.where(scope.predicate(ownership.path_column))


__all__ = [
    "ActionableInsightView",
    "CaseActionsView",
    "CaseFileHeaderView",
    "CaseFilePanelView",
    "CaseFileState",
    "CaseFileView",
    "CovenantRowView",
    "DocumentStripView",
    "EvidenceDecayState",
    "EvidenceFamilyGroupView",
    "EvidenceMarginView",
    "SignalFamilyView",
    "FORECAST_HORIZONS",
    "ForecastCitationView",
    "ForecastCovenantView",
    "ForecastDriverView",
    "ForecastExplanationView",
    "ForecastFactorView",
    "ForecastHorizonState",
    "ForecastHorizonView",
    "ForecastPanelView",
    "HeaderFactView",
    "LOG_ACTION_UNAVAILABLE",
    "ModelContributionView",
    "NO_COMPLETED_TEST",
    "NO_COVENANTS",
    "NO_EVIDENCE",
    "NO_EXPOSURE",
    "NO_FORECAST_FOR_HORIZON",
    "NO_FORECAST_PANEL",
    "NO_FORECAST_PATH",
    "NO_FORECAST_TRAJECTORY",
    "NO_HEADROOM",
    "NO_MEMO_FORECAST",
    "NO_SCHEDULE",
    "NO_SIMULATION_FORECAST",
    "NO_THRESHOLD",
    "NO_VALUE",
    "NO_WORST_COVENANT",
    "SUPPRESSED_RISK",
    "build_borrower_view",
    "build_case_file_view",
    "load_borrower_case_file",
]
