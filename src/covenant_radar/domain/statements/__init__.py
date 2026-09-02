"""The statement domain: the normalised chart of accounts and the
balance-sheet/profit-and-loss identities it checks at normalisation
(`plan.md §5.3`, `T-024`)."""

from __future__ import annotations

from covenant_radar.domain.statements.chart import (
    DEFAULT_CHART_PATH,
    DEFAULT_IDENTITY_TOLERANCE,
    SIGN_CONVENTIONS,
    STATEMENTS,
    Chart,
    ChartError,
    DerivationTerm,
    LineDiscrepancy,
    LineFlag,
    NormalisationResult,
    StatementLineDefinition,
    default_chart,
)
from covenant_radar.domain.statements.identities import (
    BALANCE_SHEET_IDENTITY,
    PROFIT_AND_LOSS_IDENTITY,
    STATEMENT_IDENTITIES,
    Identity,
    IdentityCheck,
)

__all__ = [
    "BALANCE_SHEET_IDENTITY",
    "DEFAULT_CHART_PATH",
    "DEFAULT_IDENTITY_TOLERANCE",
    "PROFIT_AND_LOSS_IDENTITY",
    "SIGN_CONVENTIONS",
    "STATEMENTS",
    "STATEMENT_IDENTITIES",
    "Chart",
    "ChartError",
    "DerivationTerm",
    "Identity",
    "IdentityCheck",
    "LineDiscrepancy",
    "LineFlag",
    "NormalisationResult",
    "StatementLineDefinition",
    "default_chart",
]
