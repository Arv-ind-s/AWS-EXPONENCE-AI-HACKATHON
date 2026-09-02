"""Scoped repository adapters for the covenant registry — `plan.md §5.5`,
`spec §R-05.a` (`T-031`).

`CovenantVersionRepository` deliberately exposes no method that can change a
persisted `CovenantVersion`'s terms. `add` stages a brand-new row; the one
write this task's service needs against an *existing* row is
`close_and_supersede`, which touches only `status` and `effective_to` — the
two columns `db/models/covenant.py`'s immutability trigger always allows,
tested or not. There is no `update`, `save`, or bulk-set method here for a
caller to reach for instead: that absence is `R-05.a`'s second enforcement
point, and `tests/integration/test_registry_versioning.py` proves it
alongside the trigger itself, the third.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import cast
from uuid import UUID

from sqlalchemy import Select, or_
from sqlalchemy.orm import Session

from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for


class CovenantRepository(RepositoryBase[Covenant]):
    """Repository for a covenant's stable identity, scoped through its
    facility's owning portfolio."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(session, Covenant, ownership=ownership_path_for(Covenant), audit=audit)

    def by_reference(self, reference: str, *, scope: Scope) -> Covenant | None:
        """Return one in-scope covenant by its stable human reference."""
        return self.find(scope=scope, reference=reference)


class CovenantVersionRepository(RepositoryBase[CovenantVersion]):
    """Repository for one covenant's dated terms, each version immutable
    once tested (`db/models/covenant.py`)."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(
            session, CovenantVersion, ownership=ownership_path_for(CovenantVersion), audit=audit
        )

    def for_covenant(self, covenant_id: UUID, *, scope: Scope) -> Sequence[CovenantVersion]:
        """Return every version of one in-scope covenant, oldest first."""
        statement: Select[tuple[CovenantVersion]] = cast(
            Select[tuple[CovenantVersion]], self._scoped_select(scope)
        )
        statement = statement.where(CovenantVersion.covenant_id == covenant_id).order_by(
            CovenantVersion.version_no
        )
        return tuple(self.session.execute(statement).scalars().all())

    def latest_for_covenant(self, covenant_id: UUID, *, scope: Scope) -> CovenantVersion | None:
        """Return the highest-numbered version of one in-scope covenant."""
        statement: Select[tuple[CovenantVersion]] = cast(
            Select[tuple[CovenantVersion]], self._scoped_select(scope)
        )
        statement = (
            statement.where(CovenantVersion.covenant_id == covenant_id)
            .order_by(CovenantVersion.version_no.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().one_or_none()

    def by_version_no(
        self, covenant_id: UUID, version_no: int, *, scope: Scope
    ) -> CovenantVersion | None:
        """Return one specific, in-scope version by its number."""
        return self.find(scope=scope, covenant_id=covenant_id, version_no=version_no)

    def live_at(self, facility_id: UUID, as_of: date, *, scope: Scope) -> Sequence[CovenantVersion]:
        """Return the one version in force, per covenant of `facility_id`,
        on `as_of` — never the earliest version for a date before any of
        them existed, because the half-open interval below simply matches
        nothing.

        `Covenant` is already present in the statement's `FROM` clause: the
        scoped select's own ownership join chain for `CovenantVersion`
        (`covenant_version` → `covenant` → `facility` → `borrower` →
        `portfolio`) reaches it first, so this method filters on it rather
        than joining it a second time.
        """
        statement: Select[tuple[CovenantVersion]] = cast(
            Select[tuple[CovenantVersion]], self._scoped_select(scope)
        )
        statement = statement.where(
            Covenant.facility_id == facility_id,
            CovenantVersion.effective_from <= as_of,
            or_(CovenantVersion.effective_to.is_(None), CovenantVersion.effective_to > as_of),
        ).order_by(CovenantVersion.covenant_id, CovenantVersion.version_no)
        return tuple(self.session.execute(statement).scalars().all())

    def close_and_supersede(self, version: CovenantVersion, *, effective_to: date) -> None:
        """The one write this repository allows against an existing,
        already-persisted version: close it as of `effective_to` and mark it
        superseded. Both are always permitted by the immutability trigger,
        regardless of `tested_at_least_once` — no other column is touched."""
        version.effective_to = effective_to
        version.status = "superseded"


__all__ = ["CovenantRepository", "CovenantVersionRepository"]
