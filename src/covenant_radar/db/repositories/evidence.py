"""Scoped persistence adapters for evidence items and transitions.

Evidence rows are historical ledger records.  This repository deliberately
does not expose ``delete`` or a bulk-delete escape hatch.  Derived fields may
be extended by a scoring run, while state changes are accompanied by an
append-only :class:`EvidenceTransition` row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import EvidenceItem, EvidenceTransition
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for
from covenant_radar.domain.signals.evidence import (
    EvidenceFacts,
    EvidenceScore,
    EvidenceTransitionFacts,
)
from covenant_radar.domain.signals.supersession import (
    SupersessionResult,
    point_in_time,
)


class EvidenceRepository(RepositoryBase[EvidenceItem]):
    """Repository for evidence items, always read through portfolio scope."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(
            session,
            EvidenceItem,
            ownership=ownership_path_for(EvidenceItem),
            audit=audit,
        )

    def by_identity(
        self,
        *,
        borrower_id: UUID,
        facility_id: UUID | None,
        family: str,
        evidence_type: str,
        scope: Scope,
    ) -> EvidenceItem | None:
        """Return the one in-scope row for the immutable evidence identity."""

        return self.find(
            scope=scope,
            borrower_id=borrower_id,
            facility_id=facility_id,
            family=family,
            evidence_type=evidence_type,
        )

    # Compatibility names keep the identity lookup discoverable to callers
    # that use the repository port's ``find`` vocabulary.
    find_by_identity = by_identity

    def for_borrower(
        self,
        borrower_id: UUID,
        *,
        scope: Scope,
        include_superseded: bool = True,
    ) -> Sequence[EvidenceItem]:
        """Return all ledger rows for a borrower, including inactive facilities."""

        statement: Select[tuple[EvidenceItem]] = cast(
            Select[tuple[EvidenceItem]], self._scoped_select(scope)
        )
        statement = statement.where(EvidenceItem.borrower_id == borrower_id)
        if not include_superseded:
            statement = statement.where(EvidenceItem.state != "superseded")
        statement = statement.order_by(
            EvidenceItem.family,
            EvidenceItem.evidence_type,
            EvidenceItem.first_seen,
            EvidenceItem.id,
        )
        return tuple(self.session.execute(statement).scalars().all())

    list_for_borrower = for_borrower

    def save_score(
        self,
        score: EvidenceScore,
        *,
        scope: Scope,
        occurred_at: datetime,
        request_id: str,
        actor_id: UUID | None = None,
        force_new: bool = False,
    ) -> EvidenceItem:
        """Insert or extend one score without committing the caller's unit.

        Existing rows are selected with a row lock on databases that support
        it.  The caller is expected to score a complete event history, so the
        score already contains the authoritative source-id union and counts.
        No state is resurrected here: a score for a superseded row must carry
        the superseded state produced by the domain stage.
        """

        if not isinstance(score, EvidenceScore):
            raise TypeError("save_score requires an EvidenceScore.")
        now = _aware_utc(occurred_at)
        _request_id(request_id)
        if actor_id is not None and not isinstance(actor_id, UUID):
            raise TypeError("actor_id must be a UUID or None.")
        if not isinstance(force_new, bool):
            raise TypeError("force_new must be a boolean.")
        self._assert_borrower_in_scope(score.borrower_id, scope)
        # A score that already carries a persisted id names the exact row to
        # update.  Resolving it by identity instead is wrong once an identity
        # holds more than one row -- a superseded predecessor alongside its
        # successor -- because the identity query returns the active row and
        # the caller's historical row would be silently rewritten (or, as the
        # id guard below catches, refused outright).  Identity resolution
        # remains the correct fallback for a score minted without an id, which
        # is how an ordinary new observation merges into its existing item.
        if force_new:
            item = None
        elif score.id is not None:
            item = self._by_id_for_update(score.id, scope)
        else:
            item = self._by_identity_for_update(score, scope)
        if item is None:
            item = EvidenceItem(
                id=score.id or new_id(),
                borrower_id=score.borrower_id,
                facility_id=score.facility_id,
                family=score.family,
                evidence_type=score.evidence_type,
                first_seen=score.first_seen,
                last_seen=score.last_seen,
                persistence_days=score.persistence_days,
                event_count_window=score.event_count_window,
                materiality_pct=score.materiality_pct,
                decay_factor=score.decay_factor,
                state=score.state,
                counts_toward_pressure=score.counts_toward_pressure,
                superseded_by_id=score.superseded_by_id,
                supersedes_id=score.supersedes_id,
                source_event_ids=list(score.source_event_ids),
                last_scored_at=now,
                created_at=now,
                updated_at=now,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                request_id=request_id,
            )
            self.session.add(item)
            self.session.flush()
        else:
            if score.id is not None and score.id != item.id:
                raise ValueError("Evidence score id does not match the existing identity row.")
            if item.state == "superseded" and score.state != "superseded":
                raise ValueError("A superseded evidence item cannot be resurrected.")
            item.first_seen = score.first_seen
            item.last_seen = score.last_seen
            item.persistence_days = score.persistence_days
            item.event_count_window = score.event_count_window
            item.materiality_pct = score.materiality_pct
            item.decay_factor = score.decay_factor
            item.state = score.state
            item.counts_toward_pressure = score.counts_toward_pressure
            item.superseded_by_id = score.superseded_by_id
            item.supersedes_id = score.supersedes_id
            item.source_event_ids = list(score.source_event_ids)
            item.last_scored_at = now
            item.updated_at = now
            item.updated_by_id = actor_id
            item.request_id = request_id
            item.version += 1

        transition = score.transition
        if transition is not None:
            self._record_transition_once(
                item.id,
                transition,
                now=now,
                request_id=request_id,
                actor_id=actor_id,
            )
        return item

    persist_score = save_score

    def save_supersession(
        self,
        revision: SupersessionResult,
        *,
        scope: Scope,
        occurred_at: datetime,
        request_id: str,
        actor_id: UUID | None = None,
    ) -> tuple[EvidenceItem, EvidenceItem]:
        """Persist one supersession as one atomic repository operation.

        The successor is inserted first because its ``supersedes_id`` points
        to the already-existing predecessor.  The predecessor is then
        updated with the forward link and terminal state.  Both writes and
        the append-only transition share the caller's transaction; a failed
        flush is therefore rolled back by the owning service/unit of work.
        """

        if not isinstance(revision, SupersessionResult):
            raise TypeError("save_supersession requires a SupersessionResult.")
        predecessor = revision.superseded
        successor = revision.successor
        if predecessor.id is None or successor.id is None:
            raise ValueError("A persisted supersession requires ids on both items.")
        self._assert_borrower_in_scope(predecessor.borrower_id, scope)
        if predecessor.borrower_id != successor.borrower_id:
            raise ValueError("A supersession cannot cross borrowers.")
        predecessor_row = self._by_id_for_update(predecessor.id, scope)
        if predecessor_row is None:
            raise ValueError("The superseded evidence item is absent or outside the scope.")
        if predecessor_row.state == "superseded":
            if predecessor_row.superseded_by_id == successor.id:
                existing_successor = self._by_id_for_update(successor.id, scope)
                if existing_successor is not None:
                    return predecessor_row, existing_successor
            raise ValueError("The superseded evidence item has already been revised.")
        if predecessor_row.state == "disputed":
            raise ValueError("Disputed evidence cannot be superseded without resolution.")
        if predecessor_row.superseded_by_id is not None:
            raise ValueError("The predecessor already has a successor.")

        existing_successor = self._by_id_for_update(successor.id, scope)
        if existing_successor is not None:
            if existing_successor.supersedes_id != predecessor.id:
                raise ValueError("The successor id is already used by another evidence item.")
            successor_row = existing_successor
        else:
            successor_row = self.save_score(
                successor,
                scope=scope,
                occurred_at=occurred_at,
                request_id=request_id,
                actor_id=actor_id,
                force_new=True,
            )

        # Apply only the revision fields to the locked predecessor.  The
        # domain result is already validated, but these checks protect the
        # database row if another caller changed it between reads.
        if predecessor_row.id != predecessor.id:
            raise ValueError("The persisted predecessor id does not match the revision.")
        predecessor_row.state = "superseded"
        predecessor_row.counts_toward_pressure = False
        predecessor_row.superseded_by_id = successor_row.id
        predecessor_row.updated_at = _aware_utc(occurred_at)
        predecessor_row.updated_by_id = actor_id
        predecessor_row.request_id = request_id
        predecessor_row.version += 1
        self._record_transition_once(
            predecessor_row.id,
            revision.transition,
            now=_aware_utc(occurred_at),
            request_id=request_id,
            actor_id=actor_id,
        )
        self.session.flush()
        return predecessor_row, successor_row

    apply_supersession = save_supersession

    def for_borrower_as_of(
        self,
        borrower_id: UUID,
        as_of: date,
        *,
        scope: Scope,
    ) -> Sequence[EvidenceFacts]:
        """Return the scoped evidence state reconstructed on ``as_of``."""

        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise TypeError("as_of must be a calendar date.")
        rows = tuple(self.for_borrower(borrower_id, scope=scope, include_superseded=True))
        if not rows:
            return ()
        transitions = EvidenceTransitionRepository(self.session).for_borrower(
            borrower_id,
            scope=scope,
        )
        return tuple(point_in_time(rows, transitions, as_of))

    read_as_of = for_borrower_as_of

    def _by_identity_for_update(self, score: EvidenceScore, scope: Scope) -> EvidenceItem | None:
        statement: Select[tuple[EvidenceItem]] = cast(
            Select[tuple[EvidenceItem]], self._scoped_select(scope)
        )
        statement = (
            statement.where(
                EvidenceItem.borrower_id == score.borrower_id,
                EvidenceItem.facility_id.is_(None)
                if score.facility_id is None
                else EvidenceItem.facility_id == score.facility_id,
                EvidenceItem.family == score.family,
                EvidenceItem.evidence_type == score.evidence_type,
            )
            .order_by(
                (EvidenceItem.state != "superseded").desc(),
                EvidenceItem.last_seen.desc(),
                EvidenceItem.id,
            )
            .with_for_update()
            .limit(1)
        )
        return self.session.execute(statement).scalars().one_or_none()

    def _by_id_for_update(self, evidence_id: UUID, scope: Scope) -> EvidenceItem | None:
        if not isinstance(evidence_id, UUID):
            raise TypeError("Evidence ids must be UUID values.")
        statement: Select[tuple[EvidenceItem]] = cast(
            Select[tuple[EvidenceItem]], self._scoped_select(scope)
        )
        statement = statement.where(EvidenceItem.id == evidence_id).with_for_update()
        return self.session.execute(statement).scalars().one_or_none()

    def _assert_borrower_in_scope(self, borrower_id: UUID, scope: Scope) -> None:
        statement = (
            select(Borrower.id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(Borrower.id == borrower_id, scope.predicate(Portfolio.path))
            .limit(1)
        )
        if self.session.execute(statement).scalar_one_or_none() is None:
            raise ValueError("Evidence borrower is absent or outside the supplied portfolio scope.")

    def _record_transition_once(
        self,
        evidence_id: UUID,
        transition: EvidenceTransitionFacts,
        *,
        now: datetime,
        request_id: str,
        actor_id: UUID | None,
    ) -> None:
        if transition.evidence_id not in {None, evidence_id}:
            raise ValueError("Evidence transition id does not match the scored item.")
        duplicate = self.session.scalar(
            select(EvidenceTransition.id).where(
                EvidenceTransition.evidence_id == evidence_id,
                EvidenceTransition.from_state == transition.from_state,
                EvidenceTransition.to_state == transition.to_state,
                EvidenceTransition.occurred_on == transition.occurred_on,
                EvidenceTransition.rule == transition.rule,
            )
        )
        if duplicate is not None:
            return
        self.session.add(
            EvidenceTransition(
                evidence_id=evidence_id,
                from_state=transition.from_state,
                to_state=transition.to_state,
                occurred_on=transition.occurred_on,
                rule=transition.rule,
                threshold_snapshot_id=transition.threshold_snapshot_id,
                created_at=now,
                updated_at=now,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                request_id=request_id,
            )
        )


class EvidenceTransitionRepository(RepositoryBase[EvidenceTransition]):
    """Scoped read adapter for the append-only transition trail."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(
            session,
            EvidenceTransition,
            ownership=ownership_path_for(EvidenceTransition),
            audit=audit,
        )

    def for_evidence(self, evidence_id: UUID, *, scope: Scope) -> Sequence[EvidenceTransition]:
        statement: Select[tuple[EvidenceTransition]] = cast(
            Select[tuple[EvidenceTransition]], self._scoped_select(scope)
        )
        statement = statement.where(EvidenceTransition.evidence_id == evidence_id).order_by(
            EvidenceTransition.occurred_on,
            EvidenceTransition.created_at,
            EvidenceTransition.id,
        )
        return tuple(self.session.execute(statement).scalars().all())

    def for_borrower(self, borrower_id: UUID, *, scope: Scope) -> Sequence[EvidenceTransition]:
        """Return all transition rows for one scoped borrower's ledger."""

        if not isinstance(borrower_id, UUID):
            raise TypeError("Borrower ids must be UUID values.")
        statement: Select[tuple[EvidenceTransition]] = cast(
            Select[tuple[EvidenceTransition]], self._scoped_select(scope)
        )
        statement = statement.where(EvidenceItem.borrower_id == borrower_id).order_by(
            EvidenceTransition.occurred_on,
            EvidenceTransition.created_at,
            EvidenceTransition.id,
        )
        return tuple(self.session.execute(statement).scalars().all())


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware.")
    return value.astimezone(UTC)


def _request_id(value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 40 or not value.strip():
        raise ValueError("request_id must be non-blank text of at most 40 characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("request_id contains a control character.")


__all__ = ["EvidenceRepository", "EvidenceTransitionRepository"]
