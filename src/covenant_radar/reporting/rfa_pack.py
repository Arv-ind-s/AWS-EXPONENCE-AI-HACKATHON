"""EWS/RFA pack assembly and export (`T-133`, `spec §2.1`'s Fraud Directions,
`spec §P-02`).

An RFA (Red Flagged Account) committee's own job is a classification, and
`spec §P-02` keeps that a human, regulated act: this module supplies the
evidence trail a committee needs — exposure and facility summary, covenant
position and history, the signal and evidence timeline, the forecast history
with drivers, every warning raised with its disposition, interventions
taken, documents and certificates, and an audit trail summary — and nothing
that could be mistaken for the classification itself. The rendered pack's
cover therefore always carries an advisory statement and an explicit "no
fraud determination" notice, and any model-drafted prose (the borrower's
latest memo, if one exists) is marked as such rather than left to read as
computed fact.

Unlike `reporting/crilc.py`, which splits pure report construction from its
database-facing service (`services/reporting.py`), this module owns both
halves itself: `T-133`'s file ownership is `reporting/rfa_pack.py`,
`web/templates/exports/rfa_pack.html` and this module's own test file, with
no companion service file to split into. The dataclasses below stay
persistence-neutral (no SQLAlchemy import in their own construction logic);
`RfaPackService` is the one place that turns a scoped principal's request
into the stored facts those dataclasses need, reusing the point-in-time
value objects `audit/reconstruct.py` (`T-068`) already defines — `DriverPart`,
`DispositionPart`, `MemoPart`, `PartStatus` and `PurgedReference` — so a
forecast's drivers, a warning's dispositions, a borrower's memo and a
purged source document are represented identically everywhere this product
shows them.

A pack for a borrower with a short history is produced with each empty
section's gap named and dated (`RfaPackGap`) rather than padded with
placeholder rows — a section that legitimately has nothing to show says so.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.audit.reconstruct import (
    DispositionPart,
    DriverPart,
    MemoPart,
    PartStatus,
    PurgedReference,
)
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, NotFound
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, Intervention
from covenant_radar.db.models.operations import RetentionPurgeLog
from covenant_radar.db.models.signal import CertificateRequest, EvidenceItem
from covenant_radar.db.models.workflow import ActionTaken, Case, Disposition, Memo
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.repositories.certificate import CertificateRequestRepository
from covenant_radar.db.repositories.covenant import CovenantVersionRepository
from covenant_radar.db.repositories.document import DocumentRepository
from covenant_radar.db.repositories.driver import DriverRepository
from covenant_radar.db.repositories.evidence import EvidenceRepository
from covenant_radar.db.repositories.facility import FacilityRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize

_TEMPLATE_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "web" / "templates"
_TEMPLATE_NAME: Final[str] = "exports/rfa_pack.html"
_MAX_RENDERED_HTML_BYTES: Final[int] = 4 * 1024 * 1024

_DOCUMENT_ENTITY: Final[str] = "document"
_BORROWER_SUBJECT_TYPE: Final[str] = "borrower"
_FORECAST_SUBJECT_TYPE: Final[str] = "forecast"
_CASE_SUBJECT_TYPE: Final[str] = "case"
_DOCUMENT_SUBJECT_TYPE: Final[str] = "document"
_RFA_PACK_EVENT: Final[str] = AuditEventType.RFA_PACK_EXPORTED.value

#: Stable, dated names for each pack section — used both for the "every
#: case: gaps are named and dated" rule and as the template's section titles.
SECTION_EXPOSURE: Final[str] = "exposure_and_facility_summary"
SECTION_COVENANTS: Final[str] = "covenant_position_and_history"
SECTION_SIGNALS: Final[str] = "signal_and_evidence_timeline"
SECTION_FORECASTS: Final[str] = "forecast_history"
SECTION_WARNINGS: Final[str] = "warnings_and_dispositions"
SECTION_INTERVENTIONS: Final[str] = "interventions_taken"
SECTION_DOCUMENTS: Final[str] = "documents_and_certificates"

#: `spec §P-02`: the product supplies evidence; classification stays a
#: human, regulated act. This statement is rendered on the pack's cover and
#: is never conditional on the pack's contents.
RFA_PACK_ADVISORY_STATEMENT: Final[str] = (
    "This pack is advisory. It assembles evidence already recorded for this "
    "borrower under the bank's normal recordkeeping; it does not compute, "
    "recommend, or imply an outcome, and none of its figures should be read "
    "as a conclusion the committee has not itself reached."
)
RFA_PACK_NO_FRAUD_DETERMINATION_STATEMENT: Final[str] = (
    "This pack contains no fraud determination. Classifying this account as "
    "a Red Flagged Account is a human, regulated decision made by the "
    "committee under the bank's Fraud Risk Management policy; nothing in "
    "this pack makes or implies that determination."
)


class RfaPackAuditWriter(Protocol):
    """The narrow append-only `C-60` boundary used by pack export."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's transaction."""
        ...


# --------------------------------------------------------------------------
# Persistence-neutral value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RfaPackGap:
    """A section with nothing to show, named and dated rather than padded."""

    section: str
    as_of: date
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "section", _text(self.section, "section", 100))
        object.__setattr__(self, "as_of", _calendar_date(self.as_of, "as_of"))
        object.__setattr__(self, "reason", _text(self.reason, "reason", 500))


@dataclass(frozen=True, slots=True)
class RfaPackFacility:
    """One live facility summarised for the exposure section."""

    facility_id: UUID
    reference: str
    facility_type: str
    sanctioned_limit: Decimal
    currency: str
    outstanding: Decimal | None
    drawing_power: Decimal | None
    sanction_date: date
    maturity_date: date | None
    effective_from: date

    def __post_init__(self) -> None:
        object.__setattr__(self, "facility_id", _uuid(self.facility_id, "facility_id"))
        object.__setattr__(self, "reference", _text(self.reference, "reference", 24))
        object.__setattr__(self, "facility_type", _text(self.facility_type, "facility_type", 50))
        object.__setattr__(
            self, "sanctioned_limit", _decimal(self.sanctioned_limit, "sanctioned_limit")
        )
        object.__setattr__(self, "currency", _text(self.currency, "currency", 3))
        object.__setattr__(self, "outstanding", _optional_decimal(self.outstanding, "outstanding"))
        object.__setattr__(
            self, "drawing_power", _optional_decimal(self.drawing_power, "drawing_power")
        )
        object.__setattr__(
            self, "sanction_date", _calendar_date(self.sanction_date, "sanction_date")
        )
        object.__setattr__(
            self, "maturity_date", _optional_calendar_date(self.maturity_date, "maturity_date")
        )
        object.__setattr__(
            self, "effective_from", _calendar_date(self.effective_from, "effective_from")
        )


@dataclass(frozen=True, slots=True)
class RfaPackExposureSummary:
    """The borrower's live facilities and their aggregate exposure."""

    as_of_date: date
    facilities: tuple[RfaPackFacility, ...]
    total_sanctioned: Decimal
    total_outstanding: Decimal | None
    currency: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of_date", _calendar_date(self.as_of_date, "as_of_date"))
        if not isinstance(self.facilities, tuple) or not all(
            isinstance(item, RfaPackFacility) for item in self.facilities
        ):
            raise TypeError("RfaPackExposureSummary.facilities must be a tuple of RfaPackFacility.")
        object.__setattr__(
            self, "total_sanctioned", _decimal(self.total_sanctioned, "total_sanctioned")
        )
        object.__setattr__(
            self,
            "total_outstanding",
            _optional_decimal(self.total_outstanding, "total_outstanding"),
        )
        if self.currency is not None:
            object.__setattr__(self, "currency", _text(self.currency, "currency", 3))


@dataclass(frozen=True, slots=True)
class RfaPackCovenantTest:
    """One computed test in a covenant's history — `history`, not `position`."""

    covenant_reference: str
    version_no: int
    as_of_date: date
    verdict: str
    value: Decimal | None
    threshold_used: Decimal | None
    headroom_pct: Decimal | None
    not_computable_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "covenant_reference", _text(self.covenant_reference, "covenant_reference", 20)
        )
        object.__setattr__(self, "version_no", _non_negative_int(self.version_no, "version_no"))
        object.__setattr__(self, "as_of_date", _calendar_date(self.as_of_date, "as_of_date"))
        object.__setattr__(self, "verdict", _text(self.verdict, "verdict", 20))
        object.__setattr__(self, "value", _optional_decimal(self.value, "value"))
        object.__setattr__(
            self, "threshold_used", _optional_decimal(self.threshold_used, "threshold_used")
        )
        object.__setattr__(
            self, "headroom_pct", _optional_decimal(self.headroom_pct, "headroom_pct")
        )
        object.__setattr__(
            self,
            "not_computable_reason",
            _optional_text(self.not_computable_reason, "not_computable_reason", 100),
        )


@dataclass(frozen=True, slots=True)
class RfaPackCovenantPosition:
    """One covenant's current terms plus its complete test history."""

    covenant_id: UUID
    reference: str
    name: str
    covenant_class: str
    current_version_no: int | None
    threshold: Decimal | None
    direction: str | None
    unit: str | None
    status: str | None
    effective_from: date | None
    effective_to: date | None
    history: tuple[RfaPackCovenantTest, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "covenant_id", _uuid(self.covenant_id, "covenant_id"))
        object.__setattr__(self, "reference", _text(self.reference, "reference", 20))
        object.__setattr__(self, "name", _text(self.name, "name", 300))
        object.__setattr__(self, "covenant_class", _text(self.covenant_class, "covenant_class", 50))
        object.__setattr__(
            self, "current_version_no", _optional_int(self.current_version_no, "current_version_no")
        )
        object.__setattr__(self, "threshold", _optional_decimal(self.threshold, "threshold"))
        object.__setattr__(self, "direction", _optional_text(self.direction, "direction", 4))
        object.__setattr__(self, "unit", _optional_text(self.unit, "unit", 20))
        object.__setattr__(self, "status", _optional_text(self.status, "status", 20))
        object.__setattr__(
            self, "effective_from", _optional_calendar_date(self.effective_from, "effective_from")
        )
        object.__setattr__(
            self, "effective_to", _optional_calendar_date(self.effective_to, "effective_to")
        )
        if not isinstance(self.history, tuple) or not all(
            isinstance(item, RfaPackCovenantTest) for item in self.history
        ):
            raise TypeError(
                "RfaPackCovenantPosition.history must be a tuple of RfaPackCovenantTest."
            )


@dataclass(frozen=True, slots=True)
class RfaPackEvidenceEntry:
    """One signal/evidence item on the borrower's timeline, with its sources."""

    id: UUID
    family: str
    evidence_type: str
    first_seen: date
    last_seen: date
    state: str
    materiality_pct: Decimal | None
    decay_factor: Decimal | None
    counts_toward_pressure: bool
    source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "id"))
        object.__setattr__(self, "family", _text(self.family, "family", 20))
        object.__setattr__(self, "evidence_type", _text(self.evidence_type, "evidence_type", 50))
        object.__setattr__(self, "first_seen", _calendar_date(self.first_seen, "first_seen"))
        object.__setattr__(self, "last_seen", _calendar_date(self.last_seen, "last_seen"))
        object.__setattr__(self, "state", _text(self.state, "state", 20))
        object.__setattr__(
            self, "materiality_pct", _optional_decimal(self.materiality_pct, "materiality_pct")
        )
        object.__setattr__(
            self, "decay_factor", _optional_decimal(self.decay_factor, "decay_factor")
        )
        if not isinstance(self.counts_toward_pressure, bool):
            raise TypeError("RfaPackEvidenceEntry.counts_toward_pressure must be a boolean.")
        if not isinstance(self.source_event_ids, tuple) or not all(
            isinstance(item, str) for item in self.source_event_ids
        ):
            raise TypeError("RfaPackEvidenceEntry.source_event_ids must be a tuple of strings.")


@dataclass(frozen=True, slots=True)
class RfaPackForecastEntry:
    """One forecast in the borrower's history, with its attributed drivers."""

    forecast_id: UUID
    covenant_reference: str | None
    version_no: int | None
    horizon_days: int
    probability: Decimal | None
    confidence: Decimal | None
    below_confidence_floor: bool
    direction: str | None
    projected_cross_date: date | None
    data_as_of: date | None
    recorded_at: datetime
    drivers: tuple[DriverPart, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "forecast_id", _uuid(self.forecast_id, "forecast_id"))
        object.__setattr__(
            self,
            "covenant_reference",
            _optional_text(self.covenant_reference, "covenant_reference", 20),
        )
        object.__setattr__(self, "version_no", _optional_int(self.version_no, "version_no"))
        object.__setattr__(
            self, "horizon_days", _non_negative_int(self.horizon_days, "horizon_days")
        )
        object.__setattr__(self, "probability", _optional_decimal(self.probability, "probability"))
        object.__setattr__(self, "confidence", _optional_decimal(self.confidence, "confidence"))
        if not isinstance(self.below_confidence_floor, bool):
            raise TypeError("RfaPackForecastEntry.below_confidence_floor must be a boolean.")
        object.__setattr__(self, "direction", _optional_text(self.direction, "direction", 4))
        object.__setattr__(
            self,
            "projected_cross_date",
            _optional_calendar_date(self.projected_cross_date, "projected_cross_date"),
        )
        object.__setattr__(
            self, "data_as_of", _optional_calendar_date(self.data_as_of, "data_as_of")
        )
        object.__setattr__(self, "recorded_at", _aware_datetime(self.recorded_at, "recorded_at"))
        if not isinstance(self.drivers, tuple) or not all(
            isinstance(item, DriverPart) for item in self.drivers
        ):
            raise TypeError("RfaPackForecastEntry.drivers must be a tuple of DriverPart.")


@dataclass(frozen=True, slots=True)
class RfaPackWarningEntry:
    """One warning raised for the borrower, with every recorded disposition.

    A "warning" is a `Forecast` that cleared T10's confidence floor
    (`below_confidence_floor is False`) — the same identity
    `services/dispositions.py` uses when it aliases the disposition subject
    type `"warning"` to `"forecast"`. A forecast below the floor was never
    shown to a desk as actionable, so it is part of `forecast_history`
    (every prediction made) without also being a "warning raised."
    """

    forecast_id: UUID
    covenant_reference: str | None
    version_no: int | None
    horizon_days: int
    raised_at: datetime
    probability: Decimal | None
    confidence: Decimal | None
    dispositions: tuple[DispositionPart, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "forecast_id", _uuid(self.forecast_id, "forecast_id"))
        object.__setattr__(
            self,
            "covenant_reference",
            _optional_text(self.covenant_reference, "covenant_reference", 20),
        )
        object.__setattr__(self, "version_no", _optional_int(self.version_no, "version_no"))
        object.__setattr__(
            self, "horizon_days", _non_negative_int(self.horizon_days, "horizon_days")
        )
        object.__setattr__(self, "raised_at", _aware_datetime(self.raised_at, "raised_at"))
        object.__setattr__(self, "probability", _optional_decimal(self.probability, "probability"))
        object.__setattr__(self, "confidence", _optional_decimal(self.confidence, "confidence"))
        if not isinstance(self.dispositions, tuple) or not all(
            isinstance(item, DispositionPart) for item in self.dispositions
        ):
            raise TypeError("RfaPackWarningEntry.dispositions must be a tuple of DispositionPart.")


@dataclass(frozen=True, slots=True)
class RfaPackInterventionEntry:
    """One intervention or free-text action recorded against the borrower."""

    id: UUID
    case_reference: str
    intervention_code: str | None
    description: str
    taken_at: datetime
    actor_id: UUID | None
    outcome: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "id"))
        object.__setattr__(self, "case_reference", _text(self.case_reference, "case_reference", 40))
        object.__setattr__(
            self,
            "intervention_code",
            _optional_text(self.intervention_code, "intervention_code", 50),
        )
        object.__setattr__(self, "description", _text(self.description, "description", 2000))
        object.__setattr__(self, "taken_at", _aware_datetime(self.taken_at, "taken_at"))
        object.__setattr__(self, "actor_id", _optional_uuid(self.actor_id, "actor_id"))
        object.__setattr__(self, "outcome", _optional_text(self.outcome, "outcome", 2000))


@dataclass(frozen=True, slots=True)
class RfaPackDocumentEntry:
    """One document held for the borrower — present, or purged and named.

    Only `PartStatus.PRESENT` and `PartStatus.PURGED` are valid here: every
    document listed was either found in storage or proven purged by a
    `RetentionPurgeLog` row naming its rule. There is no third, silent case.
    """

    status: PartStatus
    id: UUID
    filename: str | None
    doc_type: str | None
    content_hash: str | None
    retention_class: str | None
    referenced_as: str | None
    purged: PurgedReference | None

    def __post_init__(self) -> None:
        if self.status not in (PartStatus.PRESENT, PartStatus.PURGED):
            raise ValueError("RfaPackDocumentEntry.status must be PRESENT or PURGED.")
        object.__setattr__(self, "id", _uuid(self.id, "id"))
        object.__setattr__(
            self, "referenced_as", _optional_text(self.referenced_as, "referenced_as", 200)
        )
        if self.status is PartStatus.PRESENT:
            object.__setattr__(self, "filename", _text(self.filename, "filename", 500))
            object.__setattr__(self, "doc_type", _text(self.doc_type, "doc_type", 50))
            object.__setattr__(self, "content_hash", _text(self.content_hash, "content_hash", 128))
            object.__setattr__(
                self, "retention_class", _optional_text(self.retention_class, "retention_class", 50)
            )
            if self.purged is not None:
                raise ValueError("A present RfaPackDocumentEntry cannot carry a purge record.")
        else:
            if not isinstance(self.purged, PurgedReference):
                raise ValueError("A purged RfaPackDocumentEntry requires a PurgedReference.")
            if self.purged.entity_id != self.id:
                raise ValueError("The purge record must name the same document id.")
            if (
                self.filename is not None
                or self.doc_type is not None
                or self.content_hash is not None
            ):
                raise ValueError(
                    "A purged RfaPackDocumentEntry cannot carry document content fields."
                )


@dataclass(frozen=True, slots=True)
class RfaPackCertificateEntry:
    """One compliance certificate request tracked for the borrower."""

    id: UUID
    due_date: date
    state: str
    requested_at: datetime | None
    received_at: datetime | None
    document_id: UUID | None
    reviewed_by_id: UUID | None
    rejection_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "id"))
        object.__setattr__(self, "due_date", _calendar_date(self.due_date, "due_date"))
        object.__setattr__(self, "state", _text(self.state, "state", 20))
        object.__setattr__(
            self, "requested_at", _optional_aware_datetime(self.requested_at, "requested_at")
        )
        object.__setattr__(
            self, "received_at", _optional_aware_datetime(self.received_at, "received_at")
        )
        object.__setattr__(self, "document_id", _optional_uuid(self.document_id, "document_id"))
        object.__setattr__(
            self, "reviewed_by_id", _optional_uuid(self.reviewed_by_id, "reviewed_by_id")
        )
        object.__setattr__(
            self,
            "rejection_reason",
            _optional_text(self.rejection_reason, "rejection_reason", 2000),
        )


@dataclass(frozen=True, slots=True)
class RfaPackAuditSummary:
    """Counts over the audit rows that name this pack's own subjects."""

    total_events: int
    first_event_at: datetime | None
    last_event_at: datetime | None
    event_type_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "total_events", _non_negative_int(self.total_events, "total_events")
        )
        object.__setattr__(
            self, "first_event_at", _optional_aware_datetime(self.first_event_at, "first_event_at")
        )
        object.__setattr__(
            self, "last_event_at", _optional_aware_datetime(self.last_event_at, "last_event_at")
        )
        if not isinstance(self.event_type_counts, Mapping):
            raise TypeError("RfaPackAuditSummary.event_type_counts must be a mapping.")
        object.__setattr__(
            self, "event_type_counts", MappingProxyType(dict(self.event_type_counts))
        )


@dataclass(frozen=True, slots=True)
class RfaPackCover:
    """The pack's face: identity, provenance and the two required notices."""

    borrower_reference: str
    borrower_legal_name: str
    as_of_date: date
    generated_at: datetime
    prepared_by: str
    prepared_for: str
    advisory_statement: str
    no_fraud_determination_statement: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "borrower_reference", _text(self.borrower_reference, "borrower_reference", 20)
        )
        object.__setattr__(
            self, "borrower_legal_name", _text(self.borrower_legal_name, "borrower_legal_name", 300)
        )
        object.__setattr__(self, "as_of_date", _calendar_date(self.as_of_date, "as_of_date"))
        object.__setattr__(self, "generated_at", _aware_datetime(self.generated_at, "generated_at"))
        object.__setattr__(self, "prepared_by", _text(self.prepared_by, "prepared_by", 200))
        object.__setattr__(self, "prepared_for", _text(self.prepared_for, "prepared_for", 200))
        object.__setattr__(
            self, "advisory_statement", _text(self.advisory_statement, "advisory_statement", 1000)
        )
        object.__setattr__(
            self,
            "no_fraud_determination_statement",
            _text(self.no_fraud_determination_statement, "no_fraud_determination_statement", 1000),
        )


@dataclass(frozen=True, slots=True)
class RfaPack:
    """The complete, assembled pack — every section `spec §2.1` requires."""

    cover: RfaPackCover
    exposure: RfaPackExposureSummary
    covenants: tuple[RfaPackCovenantPosition, ...]
    evidence: tuple[RfaPackEvidenceEntry, ...]
    forecasts: tuple[RfaPackForecastEntry, ...]
    warnings: tuple[RfaPackWarningEntry, ...]
    interventions: tuple[RfaPackInterventionEntry, ...]
    documents: tuple[RfaPackDocumentEntry, ...]
    certificates: tuple[RfaPackCertificateEntry, ...]
    memo: MemoPart
    audit_summary: RfaPackAuditSummary
    gaps: tuple[RfaPackGap, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.cover, RfaPackCover):
            raise TypeError("RfaPack.cover must be an RfaPackCover.")
        if not isinstance(self.exposure, RfaPackExposureSummary):
            raise TypeError("RfaPack.exposure must be an RfaPackExposureSummary.")
        _tuple_of(self.covenants, RfaPackCovenantPosition, "covenants")
        _tuple_of(self.evidence, RfaPackEvidenceEntry, "evidence")
        _tuple_of(self.forecasts, RfaPackForecastEntry, "forecasts")
        _tuple_of(self.warnings, RfaPackWarningEntry, "warnings")
        _tuple_of(self.interventions, RfaPackInterventionEntry, "interventions")
        _tuple_of(self.documents, RfaPackDocumentEntry, "documents")
        _tuple_of(self.certificates, RfaPackCertificateEntry, "certificates")
        if not isinstance(self.memo, MemoPart):
            raise TypeError("RfaPack.memo must be a MemoPart.")
        if not isinstance(self.audit_summary, RfaPackAuditSummary):
            raise TypeError("RfaPack.audit_summary must be an RfaPackAuditSummary.")
        _tuple_of(self.gaps, RfaPackGap, "gaps")

    def gap_for(self, section: str) -> RfaPackGap | None:
        """Return the recorded gap for `section`, if that section is empty."""
        for gap in self.gaps:
            if gap.section == section:
                return gap
        return None


@dataclass(frozen=True, slots=True)
class RfaPackExportResult:
    """One rendered, audited export of an `RfaPack`."""

    pack: RfaPack
    html: str
    content_hash: str
    generated_at: datetime
    audit_event: object


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


class RfaPackService:
    """Assemble, render and audit one borrower's RFA pack.

    The service never commits — one call runs inside the caller's existing
    transaction, exactly like every other service in this application.
    """

    def __init__(
        self,
        session: Session,
        *,
        audit: RfaPackAuditWriter,
        clock: Clock | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        request_id: str | None = None,
        template_directory: Path | str = _TEMPLATE_ROOT,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("RfaPackService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("RfaPackService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("RfaPackService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("RfaPackService scope_resolver must be callable.")
        directory = Path(template_directory).expanduser().resolve()
        template_path = directory / _TEMPLATE_NAME
        if not template_path.is_file():
            raise FileNotFoundError(f"RFA pack export template does not exist: {template_path}")

        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )
        self.template_directory = directory
        self.environment = Environment(
            loader=FileSystemLoader(str(directory)),
            autoescape=select_autoescape(("html", "xml")),
            undefined=StrictUndefined,
        )
        self.borrowers = BorrowerRepository(session)
        self.facilities = FacilityRepository(session)
        self.evidence = EvidenceRepository(session)
        self.drivers = DriverRepository(session)
        self.documents = DocumentRepository(session)
        self.certificates = CertificateRequestRepository(session)
        self.covenant_versions = CovenantVersionRepository(session)

    # ---- public API --------------------------------------------------

    def assemble(
        self,
        principal: Principal,
        borrower_id: UUID,
        *,
        as_of_date: date | None = None,
        prepared_for: str,
        scope: Scope | None = None,
    ) -> RfaPack:
        """Assemble one borrower's complete, persistence-neutral RFA pack."""

        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.VIEW_AUDIT)
        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        resolved_scope = self._validated_scope(principal, scope)
        prepared_for_text = _text(prepared_for, "prepared_for", 200)

        borrower = self.borrowers.get(borrower_id, scope=resolved_scope)
        if borrower is None:
            raise NotFound(f"Borrower {borrower_id} was not found within the current scope.")
        resolved_as_of = (
            _calendar_date(as_of_date, "as_of_date")
            if as_of_date is not None
            else self._now().date()
        )

        gaps: list[RfaPackGap] = []

        facility_rows = self.facilities.for_borrower(
            borrower_id, scope=resolved_scope, current_only=True
        )
        exposure = _exposure_summary(facility_rows, resolved_as_of)
        if not facility_rows:
            gaps.append(
                RfaPackGap(
                    section=SECTION_EXPOSURE,
                    as_of=resolved_as_of,
                    reason=(
                        f"No live facilities are recorded for {borrower.reference} as of "
                        f"{resolved_as_of.isoformat()}."
                    ),
                )
            )

        facility_ids = tuple(row.id for row in facility_rows)
        covenant_rows = self._covenants_for_facilities(facility_ids, resolved_scope)
        covenant_positions = tuple(
            self._covenant_position(row, resolved_scope) for row in covenant_rows
        )
        if not covenant_positions:
            gaps.append(
                RfaPackGap(
                    section=SECTION_COVENANTS,
                    as_of=resolved_as_of,
                    reason=(
                        f"No covenants are registered against {borrower.reference}'s live "
                        f"facilities as of {resolved_as_of.isoformat()}."
                    ),
                )
            )

        evidence_rows = self.evidence.for_borrower(
            borrower_id, scope=resolved_scope, include_superseded=True
        )
        evidence_entries = tuple(_evidence_entry(row) for row in evidence_rows)
        if not evidence_entries:
            gaps.append(
                RfaPackGap(
                    section=SECTION_SIGNALS,
                    as_of=resolved_as_of,
                    reason=(
                        f"No signal or evidence items have been recorded for {borrower.reference}."
                    ),
                )
            )

        forecast_rows = self._forecasts_for_borrower(borrower_id, scope=resolved_scope)
        version_labels = self._forecast_labels(
            tuple({row.covenant_version_id for row in forecast_rows}), scope=resolved_scope
        )
        forecast_entries = tuple(
            self._forecast_entry(row, version_labels, scope=resolved_scope) for row in forecast_rows
        )
        if not forecast_entries:
            gaps.append(
                RfaPackGap(
                    section=SECTION_FORECASTS,
                    as_of=resolved_as_of,
                    reason=f"No forecasts have been recorded for {borrower.reference}.",
                )
            )

        warning_rows = tuple(row for row in forecast_rows if not row.below_confidence_floor)
        warning_entries = tuple(self._warning_entry(row, version_labels) for row in warning_rows)
        if not warning_entries:
            reason = (
                "No forecast for this borrower cleared the confidence floor, so no warning "
                "was raised."
                if forecast_entries
                else (
                    f"No forecasts have been recorded for {borrower.reference}, "
                    "so no warning was raised."
                )
            )
            gaps.append(RfaPackGap(section=SECTION_WARNINGS, as_of=resolved_as_of, reason=reason))

        intervention_rows = self._interventions_for_borrower(borrower_id, scope=resolved_scope)
        intervention_entries = tuple(self._intervention_entry(row) for row in intervention_rows)
        if not intervention_entries:
            gaps.append(
                RfaPackGap(
                    section=SECTION_INTERVENTIONS,
                    as_of=resolved_as_of,
                    reason=(
                        f"No interventions have been recorded against {borrower.reference}'s cases."
                    ),
                )
            )

        document_entries, certificate_entries = self._documents_and_certificates(
            borrower_id, covenant_rows, resolved_scope
        )
        if not document_entries and not certificate_entries:
            gaps.append(
                RfaPackGap(
                    section=SECTION_DOCUMENTS,
                    as_of=resolved_as_of,
                    reason=(
                        "No documents or compliance certificates are recorded for "
                        f"{borrower.reference}."
                    ),
                )
            )

        memo_row = self._latest_memo(borrower_id, scope=resolved_scope)
        memo_part = _memo_part(memo_row)

        subjects: list[tuple[str, UUID]] = [(_BORROWER_SUBJECT_TYPE, borrower_id)]
        subjects.extend((_FORECAST_SUBJECT_TYPE, row.id) for row in forecast_rows)
        subjects.extend(
            (_CASE_SUBJECT_TYPE, case_id) for case_id in {row.case_id for row in intervention_rows}
        )
        subjects.extend((_DOCUMENT_SUBJECT_TYPE, entry.id) for entry in document_entries)
        audit_summary = self._audit_summary(subjects)

        cover = RfaPackCover(
            borrower_reference=borrower.reference,
            borrower_legal_name=borrower.legal_name,
            as_of_date=resolved_as_of,
            generated_at=self._now(),
            prepared_by=_principal_label(principal),
            prepared_for=prepared_for_text,
            advisory_statement=RFA_PACK_ADVISORY_STATEMENT,
            no_fraud_determination_statement=RFA_PACK_NO_FRAUD_DETERMINATION_STATEMENT,
        )

        return RfaPack(
            cover=cover,
            exposure=exposure,
            covenants=covenant_positions,
            evidence=evidence_entries,
            forecasts=forecast_entries,
            warnings=warning_entries,
            interventions=intervention_entries,
            documents=document_entries,
            certificates=certificate_entries,
            memo=memo_part,
            audit_summary=audit_summary,
            gaps=tuple(gaps),
        )

    def export(
        self,
        principal: Principal,
        borrower_id: UUID,
        *,
        as_of_date: date | None = None,
        prepared_for: str,
        scope: Scope | None = None,
        generated_at: datetime | None = None,
        request_id: str | None = None,
    ) -> RfaPackExportResult:
        """Assemble, render and audit one exportable RFA pack bundle."""

        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.EXPORT_EVIDENCE)
        pack = self.assemble(
            principal, borrower_id, as_of_date=as_of_date, prepared_for=prepared_for, scope=scope
        )
        html = self._render_html(pack)
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
        instant = (
            self._now() if generated_at is None else _aware_datetime(generated_at, "generated_at")
        )
        effective_request_id = request_id or self.request_id

        event = self.audit.record(
            _RFA_PACK_EVENT,
            (_BORROWER_SUBJECT_TYPE, borrower_id),
            {
                "borrower_id": str(borrower_id),
                "as_of_date": pack.cover.as_of_date.isoformat(),
                "prepared_for": pack.cover.prepared_for,
                "assembled_by": str(principal.id),
                "section_counts": {
                    "facilities": len(pack.exposure.facilities),
                    "covenants": len(pack.covenants),
                    "evidence": len(pack.evidence),
                    "forecasts": len(pack.forecasts),
                    "warnings": len(pack.warnings),
                    "interventions": len(pack.interventions),
                    "documents": len(pack.documents),
                    "certificates": len(pack.certificates),
                },
                "gap_count": len(pack.gaps),
                "content_hash": content_hash,
            },
            actor=principal.id,
            request_id=effective_request_id,
        )
        return RfaPackExportResult(
            pack=pack, html=html, content_hash=content_hash, generated_at=instant, audit_event=event
        )

    # ---- assembly helpers ----------------------------------------------

    def _render_html(self, pack: RfaPack) -> str:
        context = _template_context(pack)
        html = self.environment.get_template(_TEMPLATE_NAME).render(**context)
        if len(html.encode("utf-8")) > _MAX_RENDERED_HTML_BYTES:
            raise ValueError("RFA pack export exceeds the maximum rendered document size.")
        return html

    def _covenants_for_facilities(
        self, facility_ids: Sequence[UUID], scope: Scope
    ) -> tuple[Covenant, ...]:
        if not facility_ids:
            return ()
        ownership = ownership_path_for(Covenant)
        statement = ownership.apply(select(Covenant)).where(
            scope.predicate(ownership.path_column), Covenant.facility_id.in_(facility_ids)
        )
        statement = statement.order_by(Covenant.reference)
        return tuple(self.session.execute(statement).scalars().all())

    def _tests_for_version(
        self, covenant_version_id: UUID, scope: Scope
    ) -> tuple[CovenantTest, ...]:
        ownership = ownership_path_for(CovenantTest)
        statement = ownership.apply(select(CovenantTest)).where(
            scope.predicate(ownership.path_column),
            CovenantTest.covenant_version_id == covenant_version_id,
        )
        statement = statement.order_by(CovenantTest.as_of_date, CovenantTest.id)
        return tuple(self.session.execute(statement).scalars().all())

    def _covenant_position(self, covenant: Covenant, scope: Scope) -> RfaPackCovenantPosition:
        versions = self.covenant_versions.for_covenant(covenant.id, scope=scope)
        latest = versions[-1] if versions else None
        history: list[RfaPackCovenantTest] = []
        for version in versions:
            for test in self._tests_for_version(version.id, scope):
                history.append(
                    RfaPackCovenantTest(
                        covenant_reference=covenant.reference,
                        version_no=version.version_no,
                        as_of_date=test.as_of_date,
                        verdict=test.verdict,
                        value=test.value,
                        threshold_used=test.threshold_used,
                        headroom_pct=test.headroom_pct,
                        not_computable_reason=test.not_computable_reason,
                    )
                )
        history.sort(key=lambda item: (item.as_of_date, item.version_no))
        return RfaPackCovenantPosition(
            covenant_id=covenant.id,
            reference=covenant.reference,
            name=covenant.name,
            covenant_class=covenant.covenant_class,
            current_version_no=latest.version_no if latest is not None else None,
            threshold=latest.threshold if latest is not None else None,
            direction=latest.direction if latest is not None else None,
            unit=latest.unit if latest is not None else None,
            status=latest.status if latest is not None else None,
            effective_from=latest.effective_from if latest is not None else None,
            effective_to=latest.effective_to if latest is not None else None,
            history=tuple(history),
        )

    def _forecasts_for_borrower(self, borrower_id: UUID, *, scope: Scope) -> tuple[Forecast, ...]:
        ownership = ownership_path_for(Forecast)
        statement = ownership.apply(select(Forecast)).where(
            scope.predicate(ownership.path_column), Borrower.id == borrower_id
        )
        statement = statement.order_by(Forecast.created_at, Forecast.id)
        return tuple(self.session.execute(statement).scalars().all())

    def _forecast_labels(
        self, version_ids: Sequence[UUID], *, scope: Scope
    ) -> dict[UUID, tuple[str, int]]:
        if not version_ids:
            return {}
        ownership = ownership_path_for(CovenantVersion)
        statement = ownership.apply(select(CovenantVersion, Covenant.reference)).where(
            scope.predicate(ownership.path_column), CovenantVersion.id.in_(version_ids)
        )
        rows = self.session.execute(statement).all()
        return {version.id: (reference, version.version_no) for version, reference in rows}

    def _forecast_entry(
        self, row: Forecast, labels: Mapping[UUID, tuple[str, int]], *, scope: Scope
    ) -> RfaPackForecastEntry:
        label = labels.get(row.covenant_version_id)
        driver_rows = self.drivers.for_forecast(row.id, scope=scope)
        drivers = tuple(
            DriverPart(
                name=item.name,
                share=item.share,
                evidence_id=item.evidence_id,
                is_other=item.is_other,
            )
            for item in driver_rows
        )
        return RfaPackForecastEntry(
            forecast_id=row.id,
            covenant_reference=label[0] if label is not None else None,
            version_no=label[1] if label is not None else None,
            horizon_days=row.horizon_days,
            probability=row.probability,
            confidence=row.confidence,
            below_confidence_floor=row.below_confidence_floor,
            direction=row.direction,
            projected_cross_date=row.projected_cross_date,
            data_as_of=row.data_as_of,
            recorded_at=row.created_at,
            drivers=drivers,
        )

    def _warning_entry(
        self, row: Forecast, labels: Mapping[UUID, tuple[str, int]]
    ) -> RfaPackWarningEntry:
        label = labels.get(row.covenant_version_id)
        disposition_rows = tuple(
            self.session.execute(
                select(Disposition)
                .where(
                    Disposition.subject_type == _FORECAST_SUBJECT_TYPE,
                    Disposition.subject_id == row.id,
                )
                .order_by(Disposition.created_at, Disposition.id)
            )
            .scalars()
            .all()
        )
        dispositions = tuple(
            DispositionPart(
                id=item.id,
                outcome=item.outcome,
                reason_code=item.reason_code,
                note=item.note,
                actor_id=item.actor_id,
                recorded_at=item.created_at,
            )
            for item in disposition_rows
        )
        return RfaPackWarningEntry(
            forecast_id=row.id,
            covenant_reference=label[0] if label is not None else None,
            version_no=label[1] if label is not None else None,
            horizon_days=row.horizon_days,
            raised_at=row.created_at,
            probability=row.probability,
            confidence=row.confidence,
            dispositions=dispositions,
        )

    def _interventions_for_borrower(
        self, borrower_id: UUID, *, scope: Scope
    ) -> tuple[ActionTaken, ...]:
        ownership = ownership_path_for(ActionTaken)
        statement = ownership.apply(select(ActionTaken)).where(
            scope.predicate(ownership.path_column), Case.borrower_id == borrower_id
        )
        statement = statement.order_by(ActionTaken.taken_at, ActionTaken.id)
        return tuple(self.session.execute(statement).scalars().all())

    def _intervention_entry(self, row: ActionTaken) -> RfaPackInterventionEntry:
        case = self.session.get(Case, row.case_id)
        case_reference = case.reference if case is not None else str(row.case_id)
        intervention_code: str | None = None
        description = row.free_text
        if row.intervention_id is not None:
            playbook = self.session.get(Intervention, row.intervention_id)
            if playbook is not None:
                intervention_code = playbook.code
                if not description:
                    description = playbook.text
        if not description:
            description = "(No description recorded.)"
        return RfaPackInterventionEntry(
            id=row.id,
            case_reference=case_reference,
            intervention_code=intervention_code,
            description=description,
            taken_at=row.taken_at,
            actor_id=row.actor_id,
            outcome=row.outcome,
        )

    def _documents_and_certificates(
        self, borrower_id: UUID, covenant_rows: Sequence[Covenant], scope: Scope
    ) -> tuple[tuple[RfaPackDocumentEntry, ...], tuple[RfaPackCertificateEntry, ...]]:
        document_rows = self.documents.for_borrower(borrower_id, scope=scope)
        entries: list[RfaPackDocumentEntry] = [
            RfaPackDocumentEntry(
                status=PartStatus.PRESENT,
                id=row.id,
                filename=row.filename,
                doc_type=row.doc_type,
                content_hash=row.content_hash,
                retention_class=row.retention_class,
                referenced_as=None,
                purged=None,
            )
            for row in document_rows
        ]
        seen_ids = {entry.id for entry in entries}

        referenced: list[tuple[UUID, str]] = []
        for covenant in covenant_rows:
            for version in self.covenant_versions.for_covenant(covenant.id, scope=scope):
                if version.source_document_id is not None:
                    referenced.append(
                        (
                            version.source_document_id,
                            f"Source document for {covenant.reference} v{version.version_no}",
                        )
                    )
        certificate_rows = self.certificates.for_borrower(borrower_id, scope=scope)
        for certificate in certificate_rows:
            if certificate.document_id is not None:
                referenced.append(
                    (certificate.document_id, f"Certificate due {certificate.due_date.isoformat()}")
                )

        for document_id, referenced_as in referenced:
            if document_id in seen_ids:
                continue
            seen_ids.add(document_id)
            document = self.documents.get(document_id, scope=scope)
            if document is not None:
                entries.append(
                    RfaPackDocumentEntry(
                        status=PartStatus.PRESENT,
                        id=document.id,
                        filename=document.filename,
                        doc_type=document.doc_type,
                        content_hash=document.content_hash,
                        retention_class=document.retention_class,
                        referenced_as=referenced_as,
                        purged=None,
                    )
                )
                continue
            purge = self._purge_reference(_DOCUMENT_ENTITY, document_id)
            if purge is not None:
                entries.append(
                    RfaPackDocumentEntry(
                        status=PartStatus.PURGED,
                        id=document_id,
                        filename=None,
                        doc_type=None,
                        content_hash=None,
                        retention_class=None,
                        referenced_as=referenced_as,
                        purged=purge,
                    )
                )
            # A referenced id that is neither resolvable nor explained by a
            # purge record is a data-integrity gap this pack cannot honestly
            # name (no rule, no date) — it is left out rather than guessed at.

        certificates = tuple(_certificate_entry(row) for row in certificate_rows)
        return tuple(entries), certificates

    def _purge_reference(self, entity: str, entity_id: UUID) -> PurgedReference | None:
        rows = (
            self.session.execute(
                select(RetentionPurgeLog)
                .where(RetentionPurgeLog.entity == entity)
                .order_by(RetentionPurgeLog.executed_at.desc(), RetentionPurgeLog.id.desc())
            )
            .scalars()
            .all()
        )
        target = str(entity_id)
        for row in rows:
            criteria = row.criteria or {}
            matched = criteria.get("entity_id") or criteria.get("document_id") or criteria.get("id")
            if matched is not None and str(matched) == target:
                rule = str(criteria.get("rule") or f"{entity} retention purge")
                return PurgedReference(
                    entity=entity,
                    entity_id=entity_id,
                    rule=rule,
                    purged_at=row.executed_at,
                    purged_count=row.purged_count,
                )
        return None

    def _latest_memo(self, borrower_id: UUID, *, scope: Scope) -> Memo | None:
        ownership = ownership_path_for(Memo)
        statement = ownership.apply(select(Memo)).where(
            scope.predicate(ownership.path_column), Memo.borrower_id == borrower_id
        )
        statement = statement.order_by(Memo.created_at.desc(), Memo.id.desc()).limit(1)
        return self.session.execute(statement).scalars().first()

    def _audit_summary(self, subjects: Sequence[tuple[str, UUID]]) -> RfaPackAuditSummary:
        pairs = tuple(dict.fromkeys(subjects))
        if not pairs:
            return RfaPackAuditSummary(
                total_events=0, first_event_at=None, last_event_at=None, event_type_counts={}
            )
        statement = (
            select(AuditEvent)
            .where(tuple_(AuditEvent.subject_type, AuditEvent.subject_id).in_(pairs))
            .order_by(AuditEvent.sequence)
        )
        rows = tuple(self.session.execute(statement).scalars().all())
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.event_type] = counts.get(row.event_type, 0) + 1
        return RfaPackAuditSummary(
            total_events=len(rows),
            first_event_at=rows[0].occurred_at if rows else None,
            last_event_at=rows[-1].occurred_at if rows else None,
            event_type_counts=counts,
        )

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        resolved = self.scope_resolver(principal) if scope is None else scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied scope does not belong to the authenticated principal."
            )
        return resolved

    def _now(self) -> datetime:
        instant = self.clock.now()
        if (
            not isinstance(instant, datetime)
            or instant.tzinfo is None
            or instant.utcoffset() is None
        ):
            raise ValueError("RfaPackService clock must return a timezone-aware datetime.")
        return instant


# --------------------------------------------------------------------------
# Module-level construction and rendering helpers
# --------------------------------------------------------------------------


def _exposure_summary(
    facility_rows: Sequence[Facility], as_of_date: date
) -> RfaPackExposureSummary:
    entries = tuple(
        RfaPackFacility(
            facility_id=row.id,
            reference=row.reference,
            facility_type=row.facility_type,
            sanctioned_limit=row.sanctioned_limit,
            currency=row.currency,
            outstanding=row.outstanding,
            drawing_power=row.drawing_power,
            sanction_date=row.sanction_date,
            maturity_date=row.maturity_date,
            effective_from=row.effective_from,
        )
        for row in facility_rows
    )
    total_sanctioned = sum((row.sanctioned_limit for row in facility_rows), Decimal("0"))
    outstanding_values = [row.outstanding for row in facility_rows]
    total_outstanding = (
        sum((value for value in outstanding_values if value is not None), Decimal("0"))
        if outstanding_values and all(value is not None for value in outstanding_values)
        else None
    )
    currencies = {row.currency for row in facility_rows}
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    return RfaPackExposureSummary(
        as_of_date=as_of_date,
        facilities=entries,
        total_sanctioned=total_sanctioned,
        total_outstanding=total_outstanding,
        currency=currency,
    )


def _evidence_entry(row: EvidenceItem) -> RfaPackEvidenceEntry:
    return RfaPackEvidenceEntry(
        id=row.id,
        family=row.family,
        evidence_type=row.evidence_type,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        state=row.state,
        materiality_pct=row.materiality_pct,
        decay_factor=row.decay_factor,
        counts_toward_pressure=row.counts_toward_pressure,
        source_event_ids=tuple(row.source_event_ids or ()),
    )


def _certificate_entry(row: CertificateRequest) -> RfaPackCertificateEntry:
    return RfaPackCertificateEntry(
        id=row.id,
        due_date=row.due_date,
        state=row.state,
        requested_at=row.requested_at,
        received_at=row.received_at,
        document_id=row.document_id,
        reviewed_by_id=row.reviewed_by_id,
        rejection_reason=row.rejection_reason,
    )


def _memo_part(memo: Memo | None) -> MemoPart:
    if memo is None:
        return MemoPart.not_generated()
    return MemoPart.present(
        id=memo.id,
        template_version=memo.template_version,
        prompt_version=memo.prompt_version,
        drafted_text=memo.drafted_text,
        check_verdict=memo.check_verdict,
        generated_by_id=memo.generated_by_id,
        generated_at=memo.created_at,
    )


def _principal_label(principal: Principal) -> str:
    if principal.kind is PrincipalKind.API_KEY:
        return f"api-key:{principal.id}"
    return f"user:{principal.id}"


def _display(value: object) -> str:
    """Render one field value as safe, human-readable template text."""

    if value is None:
        return "Not available from the recorded evidence."
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, PartStatus):
        return value.value
    return str(value)


def _facility_rows(entry: RfaPackFacility) -> list[tuple[str, str]]:
    return [
        ("Facility reference", _display(entry.reference)),
        ("Type", _display(entry.facility_type)),
        ("Sanctioned limit", f"{_display(entry.sanctioned_limit)} {entry.currency}"),
        ("Outstanding", _display(entry.outstanding)),
        ("Drawing power", _display(entry.drawing_power)),
        ("Sanction date", _display(entry.sanction_date)),
        ("Maturity date", _display(entry.maturity_date)),
        ("Effective from", _display(entry.effective_from)),
    ]


def _covenant_rows(entry: RfaPackCovenantPosition) -> list[tuple[str, str]]:
    if entry.history:
        history_text = "; ".join(
            f"{test.as_of_date.isoformat()} v{test.version_no}: {test.verdict}"
            + (f" ({_display(test.value)})" if test.value is not None else "")
            for test in entry.history
        )
    else:
        history_text = "No tests recorded."
    return [
        ("Covenant reference", _display(entry.reference)),
        ("Name", _display(entry.name)),
        ("Class", _display(entry.covenant_class)),
        ("Current version", _display(entry.current_version_no)),
        ("Threshold", _display(entry.threshold)),
        ("Direction", _display(entry.direction)),
        ("Unit", _display(entry.unit)),
        ("Status", _display(entry.status)),
        ("Effective from", _display(entry.effective_from)),
        ("Effective to", _display(entry.effective_to)),
        ("Test history", history_text),
    ]


def _evidence_rows(entry: RfaPackEvidenceEntry) -> list[tuple[str, str]]:
    sources = (
        ", ".join(entry.source_event_ids)
        if entry.source_event_ids
        else "Not available from the recorded evidence."
    )
    return [
        ("Family", _display(entry.family)),
        ("Type", _display(entry.evidence_type)),
        ("First seen", _display(entry.first_seen)),
        ("Last seen", _display(entry.last_seen)),
        ("State", _display(entry.state)),
        ("Materiality %", _display(entry.materiality_pct)),
        ("Decay factor", _display(entry.decay_factor)),
        ("Counts toward pressure", _display(entry.counts_toward_pressure)),
        ("Source events", sources),
    ]


def _forecast_rows(entry: RfaPackForecastEntry) -> list[tuple[str, str]]:
    driver_text = (
        "; ".join(f"{item.name} ({item.share * 100:.1f}%)" for item in entry.drivers)
        if entry.drivers
        else "Not available from the recorded evidence."
    )
    return [
        ("Covenant", _display(entry.covenant_reference)),
        ("Version", _display(entry.version_no)),
        ("Horizon (days)", _display(entry.horizon_days)),
        ("Probability", _display(entry.probability)),
        ("Confidence", _display(entry.confidence)),
        ("Below confidence floor", _display(entry.below_confidence_floor)),
        ("Direction", _display(entry.direction)),
        ("Projected crossing date", _display(entry.projected_cross_date)),
        ("Data as of", _display(entry.data_as_of)),
        ("Recorded at", _display(entry.recorded_at)),
        ("Drivers", driver_text),
    ]


def _warning_rows(entry: RfaPackWarningEntry) -> list[tuple[str, str]]:
    if entry.dispositions:
        disposition_text = "; ".join(
            f"{item.recorded_at.date().isoformat()}: {item.outcome}"
            + (f" ({item.reason_code})" if item.reason_code else "")
            for item in entry.dispositions
        )
    else:
        disposition_text = "No disposition recorded yet."
    return [
        ("Covenant", _display(entry.covenant_reference)),
        ("Version", _display(entry.version_no)),
        ("Horizon (days)", _display(entry.horizon_days)),
        ("Raised at", _display(entry.raised_at)),
        ("Probability", _display(entry.probability)),
        ("Confidence", _display(entry.confidence)),
        ("Disposition", disposition_text),
    ]


def _intervention_rows(entry: RfaPackInterventionEntry) -> list[tuple[str, str]]:
    return [
        ("Case", _display(entry.case_reference)),
        ("Intervention", _display(entry.intervention_code)),
        ("Description", _display(entry.description)),
        ("Taken at", _display(entry.taken_at)),
        ("Outcome", _display(entry.outcome)),
    ]


def _document_rows(entry: RfaPackDocumentEntry) -> list[tuple[str, str]]:
    if entry.status is PartStatus.PURGED:
        assert entry.purged is not None  # invariant enforced by RfaPackDocumentEntry
        return [
            ("Status", "Purged"),
            ("Referenced as", _display(entry.referenced_as)),
            ("Retention rule", entry.purged.rule),
            ("Purged at", _display(entry.purged.purged_at)),
        ]
    return [
        ("Filename", _display(entry.filename)),
        ("Type", _display(entry.doc_type)),
        ("Content hash", _display(entry.content_hash)),
        ("Retention class", _display(entry.retention_class)),
        ("Referenced as", _display(entry.referenced_as)),
    ]


def _certificate_rows(entry: RfaPackCertificateEntry) -> list[tuple[str, str]]:
    return [
        ("Due date", _display(entry.due_date)),
        ("State", _display(entry.state)),
        ("Requested at", _display(entry.requested_at)),
        ("Received at", _display(entry.received_at)),
        ("Rejection reason", _display(entry.rejection_reason)),
    ]


def _template_context(pack: RfaPack) -> dict[str, object]:
    cover = pack.cover
    metadata = (
        ("Borrower", f"{cover.borrower_legal_name} ({cover.borrower_reference})"),
        ("As of", cover.as_of_date.isoformat()),
        ("Generated at (UTC)", _display(cover.generated_at)),
        ("Prepared by", cover.prepared_by),
        ("Prepared for", cover.prepared_for),
    )
    exposure_meta = (
        (
            "Total sanctioned",
            f"{_display(pack.exposure.total_sanctioned)} {pack.exposure.currency or ''}".strip(),
        ),
        ("Total outstanding", _display(pack.exposure.total_outstanding)),
        ("Live facility count", str(len(pack.exposure.facilities))),
    )
    audit_meta = (
        ("Total audit events for this pack's subjects", str(pack.audit_summary.total_events)),
        ("First event", _display(pack.audit_summary.first_event_at)),
        ("Last event", _display(pack.audit_summary.last_event_at)),
    )
    collections = (
        {
            "title": "Facilities",
            "entries": tuple(_facility_rows(item) for item in pack.exposure.facilities),
        },
        {
            "title": "Covenant position and history",
            "entries": tuple(_covenant_rows(item) for item in pack.covenants),
        },
        {
            "title": "Signal and evidence timeline",
            "entries": tuple(_evidence_rows(item) for item in pack.evidence),
        },
        {
            "title": "Forecast history",
            "entries": tuple(_forecast_rows(item) for item in pack.forecasts),
        },
        {
            "title": "Warnings raised and their disposition",
            "entries": tuple(_warning_rows(item) for item in pack.warnings),
        },
        {
            "title": "Interventions taken",
            "entries": tuple(_intervention_rows(item) for item in pack.interventions),
        },
        {"title": "Documents", "entries": tuple(_document_rows(item) for item in pack.documents)},
        {
            "title": "Compliance certificates",
            "entries": tuple(_certificate_rows(item) for item in pack.certificates),
        },
    )
    gaps = tuple(
        {"section": gap.section, "as_of": gap.as_of.isoformat(), "reason": gap.reason}
        for gap in pack.gaps
    )
    memo_is_drafted = pack.memo.status is PartStatus.PRESENT
    memo_paragraphs: tuple[str, ...] = ()
    if memo_is_drafted and pack.memo.drafted_text:
        memo_paragraphs = tuple(
            part.strip() for part in pack.memo.drafted_text.split("\n\n") if part.strip()
        )
    return {
        "metadata": metadata,
        "exposure_meta": exposure_meta,
        "audit_meta": audit_meta,
        "collections": collections,
        "gaps": gaps,
        "memo_is_drafted": memo_is_drafted,
        "memo_template_version": pack.memo.template_version,
        "memo_paragraphs": memo_paragraphs,
        "advisory_statement": cover.advisory_statement,
        "no_fraud_determination_statement": cover.no_fraud_determination_statement,
    }


# --------------------------------------------------------------------------
# Shared validators
# --------------------------------------------------------------------------


def _tuple_of(value: object, item_type: type, field_name: str) -> None:
    if not isinstance(value, tuple) or not all(isinstance(item, item_type) for item in value):
        raise TypeError(f"RfaPack.{field_name} must be a tuple of {item_type.__name__}.")


def _text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text.")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters.")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in cleaned):
        raise ValueError(f"{field_name} contains a control character.")
    return cleaned


def _optional_text(value: object, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, maximum)


def _uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID.")
    return value


def _optional_uuid(value: object, field_name: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, field_name)


def _calendar_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field_name} must be a calendar date.")
    return value


def _optional_calendar_date(value: object, field_name: str) -> date | None:
    if value is None:
        return None
    return _calendar_date(value, field_name)


def _aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be a timezone-aware datetime.")
    return value


def _optional_aware_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _aware_datetime(value, field_name)


def _decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{field_name} must be a finite Decimal.")
    return value


def _optional_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field_name)


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer.")
    return value


__all__ = [
    "RFA_PACK_ADVISORY_STATEMENT",
    "RFA_PACK_NO_FRAUD_DETERMINATION_STATEMENT",
    "SECTION_COVENANTS",
    "SECTION_DOCUMENTS",
    "SECTION_EXPOSURE",
    "SECTION_FORECASTS",
    "SECTION_INTERVENTIONS",
    "SECTION_SIGNALS",
    "SECTION_WARNINGS",
    "RfaPack",
    "RfaPackAuditSummary",
    "RfaPackAuditWriter",
    "RfaPackCertificateEntry",
    "RfaPackCover",
    "RfaPackCovenantPosition",
    "RfaPackCovenantTest",
    "RfaPackDocumentEntry",
    "RfaPackEvidenceEntry",
    "RfaPackExportResult",
    "RfaPackExposureSummary",
    "RfaPackFacility",
    "RfaPackForecastEntry",
    "RfaPackGap",
    "RfaPackInterventionEntry",
    "RfaPackService",
    "RfaPackWarningEntry",
]
