"""Scoped repository adapter for compliance certificate requests
(`plan.md §5.6`, `T-038`, `T-039`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Final, cast
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.orm import Session

from covenant_radar.db.models.signal import CertificateRequest
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for

#: States a request has not yet reached a settled outcome in — the ones a
#: generation sweep (`T-038`) or an overdue sweep (`T-039`) may still need
#: to act on. `accepted` and `rejected` are terminal and excluded.
OPEN_CERTIFICATE_STATES: Final[frozenset[str]] = frozenset(
    {"requested", "received", "under_review", "overdue"}
)


class CertificateRequestRepository(RepositoryBase[CertificateRequest]):
    """Repository for certificate requests, scoped through the borrower's
    owning portfolio."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(
            session,
            CertificateRequest,
            ownership=ownership_path_for(CertificateRequest),
            audit=audit,
        )

    def open_requests(self, *, scope: Scope) -> Sequence[CertificateRequest]:
        """Return every in-scope request not yet at a terminal state."""
        statement: Select[tuple[CertificateRequest]] = cast(
            Select[tuple[CertificateRequest]], self._scoped_select(scope)
        )
        statement = statement.where(
            CertificateRequest.state.in_(OPEN_CERTIFICATE_STATES)
        ).order_by(CertificateRequest.due_date, CertificateRequest.id)
        return tuple(self.session.execute(statement).scalars().all())

    def for_borrower(self, borrower_id: UUID, *, scope: Scope) -> Sequence[CertificateRequest]:
        """Return every in-scope request for one borrower, newest due first."""
        statement: Select[tuple[CertificateRequest]] = cast(
            Select[tuple[CertificateRequest]], self._scoped_select(scope)
        )
        statement = statement.where(CertificateRequest.borrower_id == borrower_id).order_by(
            CertificateRequest.due_date.desc(), CertificateRequest.id
        )
        return tuple(self.session.execute(statement).scalars().all())

    def by_anchor_schedule(
        self, covenant_schedule_id: UUID, *, scope: Scope
    ) -> CertificateRequest | None:
        """Return the one in-scope request anchored on `covenant_schedule_id`."""
        return self.find(scope=scope, covenant_schedule_id=covenant_schedule_id)

    def due_before(self, cutoff: date, *, scope: Scope) -> Sequence[CertificateRequest]:
        """Return every in-scope, still-open request due strictly before
        `cutoff` — the candidate set an overdue sweep (`T-039`) reads."""
        statement: Select[tuple[CertificateRequest]] = cast(
            Select[tuple[CertificateRequest]], self._scoped_select(scope)
        )
        statement = statement.where(
            CertificateRequest.state.in_(OPEN_CERTIFICATE_STATES),
            CertificateRequest.due_date < cutoff,
        ).order_by(CertificateRequest.due_date, CertificateRequest.id)
        return tuple(self.session.execute(statement).scalars().all())


__all__ = ["OPEN_CERTIFICATE_STATES", "CertificateRequestRepository"]
