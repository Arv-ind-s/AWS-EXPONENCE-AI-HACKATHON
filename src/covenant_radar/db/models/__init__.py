"""ORM models for every table in `plan.md §5`.

Importing a model module registers its tables on the shared `Base.metadata`
(`db/base.py`); importing this package registers every table `T-007`
through `T-009` define in one step, which is what `Base.metadata.create_all`
and Alembic's autogeneration (`T-010`) both need to see the complete
picture. `covenant`'s import also installs the `covenant_version`
immutability trigger and `audit`'s import installs the `audit_event`
chain-integrity trigger (both `after_create`/`before_drop` DDL events on
their own table, so simply importing the module is enough).
"""

from __future__ import annotations

from covenant_radar.db.models.audit import AuditEvent, ConfigVersion, ThresholdSnapshot, TraceRow
from covenant_radar.db.models.borrower import (
    Borrower,
    BorrowerContact,
    BorrowerGroup,
    RelatedParty,
)
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantException,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
    CovenantWaiver,
    RatioDefinition,
)
from covenant_radar.db.models.document import Document, DocumentPage, DocumentSpan
from covenant_radar.db.models.financial_pdf import FinancialPdfBatch
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastDriver,
    ForecastPath,
    ForecastRun,
    Intervention,
    Simulation,
    TriageEntry,
)
from covenant_radar.db.models.identity import (
    ApiKey,
    AppUser,
    Permission,
    Role,
    RolePermission,
    UserPortfolioScope,
    UserRole,
    UserSession,
)
from covenant_radar.db.models.intake import CovenantProposal
from covenant_radar.db.models.maker_checker import MakerCheckerRequest
from covenant_radar.db.models.operations import (
    Connector,
    ConnectorRun,
    DriftObservation,
    EntityMatch,
    EvaluationRun,
    FeedSource,
    JobRun,
    ModelCall,
    ModelRegistration,
    RetentionPurgeLog,
)
from covenant_radar.db.models.organisation import Organisation
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.reference import IndustryReference
from covenant_radar.db.models.signal import (
    CertificateRequest,
    EvidenceItem,
    EvidenceTransition,
    SignalEvent,
)
from covenant_radar.db.models.statements import (
    FieldProvenance,
    FinancialPeriod,
    ImportBatch,
    ImportMapping,
    QuarantineRow,
    StatementLineValue,
)
from covenant_radar.db.models.views import SavedQueueView
from covenant_radar.db.models.workflow import (
    ActionTaken,
    Case,
    CaseComment,
    CaseEvent,
    Disposition,
    Memo,
    MemoExport,
    Notification,
    NotificationPreference,
    NotificationReadState,
    OverrideRecord,
)

__all__ = [
    "ActionTaken",
    "ApiKey",
    "AppUser",
    "AuditEvent",
    "Borrower",
    "BorrowerContact",
    "BorrowerGroup",
    "Case",
    "CaseComment",
    "CaseEvent",
    "CertificateRequest",
    "ConfigVersion",
    "Connector",
    "ConnectorRun",
    "Covenant",
    "CovenantException",
    "CovenantProposal",
    "CovenantSchedule",
    "CovenantTest",
    "CovenantVersion",
    "CovenantWaiver",
    "Disposition",
    "Document",
    "DocumentPage",
    "DocumentSpan",
    "DriftObservation",
    "EntityMatch",
    "EvaluationRun",
    "EvidenceItem",
    "EvidenceTransition",
    "Facility",
    "FacilityConduct",
    "FeedSource",
    "FinancialPdfBatch",
    "FieldProvenance",
    "FinancialPeriod",
    "Forecast",
    "ForecastDriver",
    "ForecastPath",
    "ForecastRun",
    "ImportBatch",
    "ImportMapping",
    "IndustryReference",
    "Intervention",
    "JobRun",
    "MakerCheckerRequest",
    "Memo",
    "MemoExport",
    "ModelCall",
    "ModelRegistration",
    "Notification",
    "NotificationPreference",
    "NotificationReadState",
    "Organisation",
    "OverrideRecord",
    "Permission",
    "Portfolio",
    "QuarantineRow",
    "RatioDefinition",
    "RelatedParty",
    "RetentionPurgeLog",
    "Role",
    "RolePermission",
    "SavedQueueView",
    "SignalEvent",
    "Simulation",
    "StatementLineValue",
    "ThresholdSnapshot",
    "TraceRow",
    "TriageEntry",
    "UserPortfolioScope",
    "UserRole",
    "UserSession",
]
