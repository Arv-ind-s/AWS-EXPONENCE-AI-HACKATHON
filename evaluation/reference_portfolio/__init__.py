"""Deterministic synthetic data used by the evaluation build.

The reference portfolio is deliberately kept outside the application domain
and persistence layers.  It is an input producer: callers receive typed
records and may load the records through the adapter in ``generator`` when a
database-backed evaluation is required.
"""

from __future__ import annotations

from evaluation.reference_portfolio.financials import FinancialPeriodRecord
from evaluation.reference_portfolio.generator import (
    DEFAULT_REFERENCE_CONFIG,
    BorrowerGroupRecord,
    BorrowerRecord,
    ContactRecord,
    FacilityRecord,
    ReferencePortfolio,
    ReferencePortfolioConfig,
    ReferencePortfolioError,
    ReferencePortfolioGenerator,
    RelatedPartyRecord,
    deterministic_reference_hashes,
    generate_reference_portfolio,
    load_reference_portfolio,
)

__all__ = [
    "DEFAULT_REFERENCE_CONFIG",
    "BorrowerGroupRecord",
    "BorrowerRecord",
    "ContactRecord",
    "FinancialPeriodRecord",
    "FacilityRecord",
    "ReferencePortfolio",
    "ReferencePortfolioConfig",
    "ReferencePortfolioError",
    "ReferencePortfolioGenerator",
    "RelatedPartyRecord",
    "deterministic_reference_hashes",
    "generate_reference_portfolio",
    "load_reference_portfolio",
]
