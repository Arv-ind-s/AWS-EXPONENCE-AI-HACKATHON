"""Factory exports for version-one API routers."""

from covenant_radar.api.v1.routers.audit_events import create_audit_events_router
from covenant_radar.api.v1.routers.borrowers import create_borrowers_router
from covenant_radar.api.v1.routers.cases import create_cases_router
from covenant_radar.api.v1.routers.covenant_tests import create_covenant_tests_router
from covenant_radar.api.v1.routers.covenants import create_covenants_router
from covenant_radar.api.v1.routers.evidence import create_evidence_router
from covenant_radar.api.v1.routers.explain import create_explain_router
from covenant_radar.api.v1.routers.facilities import create_facilities_router
from covenant_radar.api.v1.routers.forecast import create_forecast_router
from covenant_radar.api.v1.routers.ingest import (
    create_ingest_router,
    create_signal_ingest_router,
)
from covenant_radar.api.v1.routers.memos import create_memos_router
from covenant_radar.api.v1.routers.simulations import create_simulations_router

__all__ = [
    "create_audit_events_router",
    "create_borrowers_router",
    "create_cases_router",
    "create_covenant_tests_router",
    "create_covenants_router",
    "create_evidence_router",
    "create_explain_router",
    "create_facilities_router",
    "create_forecast_router",
    "create_ingest_router",
    "create_memos_router",
    "create_signal_ingest_router",
    "create_simulations_router",
]
