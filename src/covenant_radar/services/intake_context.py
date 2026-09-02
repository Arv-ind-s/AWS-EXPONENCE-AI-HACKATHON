"""Assemble the stored facts stage-2 verification recomputes a proposal against.

`domain/intake/verify.py` performs no I/O: its `VerificationContext` is handed
to it fully assembled.  Without a factory, `web/routes/intake.py` falls back to
an empty context — no statement lines and `period_complete=False` — which makes
the RECOMPUTABLE check fail for every proposal, on every document, for every
borrower, and so no proposal can ever reach "Confirm covenant".

This module reads the borrower's own most recent financial period through the
caller's scope and returns the same `{line_code: value}` mapping the covenant
engine tests against, so a proposal is recomputed exactly the way the covenant
it would become will later be evaluated.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.statements import FinancialPeriod, StatementLineValue
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.intake.verify import VerificationContext
from covenant_radar.domain.ratios.definitions import FacilityFacts

__all__ = ["build_verification_context"]


def build_verification_context(
    session: Session,
    facility: Facility,
    *,
    scope: Scope,
) -> VerificationContext:
    """Build the verification context for one facility's borrower.

    Falls back to an empty statement mapping — the previous behaviour — when
    the borrower has no financial period in scope.  That is a real state (a
    borrower whose statements have not been imported yet) and the RECOMPUTABLE
    check reports it accurately; what it must not be is the only state
    reachable.
    """

    if not isinstance(session, Session):
        raise TypeError("build_verification_context requires a SQLAlchemy Session.")
    if not isinstance(facility, Facility):
        raise TypeError("build_verification_context requires a Facility.")
    if not isinstance(scope, Scope):
        raise TypeError("build_verification_context requires a Scope.")

    period = _latest_period(session, facility.borrower_id, scope=scope)
    lines: dict[str, Decimal] = {}
    if period is not None:
        lines = _statement_lines(session, period.id)

    return VerificationContext(
        statement_lines=lines,
        # An absent period is not a complete one; the check must say so rather
        # than recompute against nothing and call the answer sound.
        period_complete=bool(period is not None and period.is_complete),
        facility_facts=FacilityFacts(
            sanctioned_limit=facility.sanctioned_limit,
            outstanding=facility.outstanding,
            drawing_power=facility.drawing_power,
        ),
        facility_sanction_date=facility.sanction_date,
        facility_currency=facility.currency,
    )


def _latest_period(
    session: Session,
    borrower_id: UUID,
    *,
    scope: Scope,
) -> FinancialPeriod | None:
    """The borrower's most recent complete period, else its most recent one.

    Preferring a complete period keeps the recomputation on the soundest basis
    available; falling back to the latest period means an incomplete-statement
    borrower still gets the accurate "period failed an identity check" reason
    rather than the misleading "no definition was resolved".
    """

    statement = (
        select(FinancialPeriod)
        .join(Borrower, Borrower.id == FinancialPeriod.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            FinancialPeriod.borrower_id == borrower_id,
            scope.predicate(Portfolio.path),
        )
        .order_by(FinancialPeriod.period_end.desc(), FinancialPeriod.id.desc())
    )
    periods = tuple(session.execute(statement).scalars().all())
    if not periods:
        return None
    for period in periods:
        if period.is_complete:
            return period
    return periods[0]


def _statement_lines(session: Session, period_id: UUID) -> dict[str, Decimal]:
    rows = session.execute(
        select(StatementLineValue).where(StatementLineValue.period_id == period_id)
    ).scalars()
    return {row.line_code: row.value for row in rows}
