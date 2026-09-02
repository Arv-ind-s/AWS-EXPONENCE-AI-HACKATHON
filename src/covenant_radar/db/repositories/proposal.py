"""Scoped repository for `CovenantProposal` rows — `plan.md §8`'s `T-096`.

Deliberately exposes no bulk-status-change method: the confirm-refusal
invariant (`spec §16.1`) lives entirely in `services/intake.py`'s
`IntakeService.submit`, and this repository does not offer a shortcut that
could bypass it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.orm import Session

from covenant_radar.db.models.intake import CovenantProposal
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for


class ProposalRepository(RepositoryBase[CovenantProposal]):
    """Repository for one facility's intake proposals, scoped through it."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(
            session, CovenantProposal, ownership=ownership_path_for(CovenantProposal), audit=audit
        )

    def for_document(self, document_id: UUID, *, scope: Scope) -> Sequence[CovenantProposal]:
        """Return every in-scope proposal already recorded for one document,
        oldest first — the set `IntakeService.propose_from_document` shows
        back instead of re-extracting when a document is resubmitted."""
        statement: Select[tuple[CovenantProposal]] = cast(
            Select[tuple[CovenantProposal]], self._scoped_select(scope)
        )
        statement = statement.where(CovenantProposal.document_id == document_id).order_by(
            CovenantProposal.created_at, CovenantProposal.id
        )
        return tuple(self.session.execute(statement).scalars().all())

    def for_facility_content_hash(
        self, facility_id: UUID, content_hash: str, *, scope: Scope
    ) -> Sequence[CovenantProposal]:
        """Return every in-scope, document-less proposal already recorded
        for one facility and exact clause-text hash."""
        statement: Select[tuple[CovenantProposal]] = cast(
            Select[tuple[CovenantProposal]], self._scoped_select(scope)
        )
        statement = statement.where(
            CovenantProposal.facility_id == facility_id,
            CovenantProposal.content_hash == content_hash,
            CovenantProposal.document_id.is_(None),
        ).order_by(CovenantProposal.created_at, CovenantProposal.id)
        return tuple(self.session.execute(statement).scalars().all())

    def by_id_for_update(self, proposal_id: UUID, *, scope: Scope) -> CovenantProposal | None:
        """Lock one in-scope proposal for a correct/abandon/submit write."""
        statement: Select[tuple[CovenantProposal]] = cast(
            Select[tuple[CovenantProposal]], self._scoped_select(scope)
        )
        statement = statement.where(CovenantProposal.id == proposal_id).with_for_update()
        return self.session.execute(statement).scalars().one_or_none()


__all__ = ["ProposalRepository"]
