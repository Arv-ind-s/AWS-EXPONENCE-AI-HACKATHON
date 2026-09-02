"""Regulatory and management reporting (`spec §R-31`).

`T-132` supplies the CRILC-shaped monthly extract and weekly default
report; the RFA pack (`T-133`) and board MIS (`T-134`) land in this same
package under their own tasks, sharing `services/reporting.py`'s
orchestration boundary.
"""

from __future__ import annotations

from covenant_radar.reporting.crilc import (
    CRILC_AGGREGATE_EXPOSURE_THRESHOLD,
    CrilcBorrowerFacts,
    CrilcConductFacts,
    CrilcException,
    CrilcFacilityFacts,
    CrilcLayout,
    CrilcLayoutField,
    CrilcReconciliation,
    CrilcReport,
    CrilcReportType,
    build_crilc_report,
)
from covenant_radar.reporting.mis import (
    MisChartPoint,
    MisConnectorEntry,
    MisConnectorSection,
    MisDeliveryOutcome,
    MisDistributionSection,
    MisEscalationSection,
    MisLeadTimeSection,
    MisMetric,
    MisMigrationSection,
    MisModelPerformanceSection,
    MisPeriod,
    MisReport,
    MisReportDeliveryService,
    MisReportExportResult,
    MisReportService,
    build_board_mis_job_handler,
    previous_calendar_month,
)

__all__ = [
    "CRILC_AGGREGATE_EXPOSURE_THRESHOLD",
    "CrilcBorrowerFacts",
    "CrilcConductFacts",
    "CrilcException",
    "CrilcFacilityFacts",
    "CrilcLayout",
    "CrilcLayoutField",
    "CrilcReconciliation",
    "CrilcReport",
    "CrilcReportType",
    "MisChartPoint",
    "MisConnectorEntry",
    "MisConnectorSection",
    "MisDeliveryOutcome",
    "MisDistributionSection",
    "MisEscalationSection",
    "MisLeadTimeSection",
    "MisMetric",
    "MisMigrationSection",
    "MisModelPerformanceSection",
    "MisPeriod",
    "MisReport",
    "MisReportDeliveryService",
    "MisReportExportResult",
    "MisReportService",
    "build_board_mis_job_handler",
    "build_crilc_report",
    "previous_calendar_month",
]
