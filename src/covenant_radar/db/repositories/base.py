"""SQLAlchemy repositories with mandatory portfolio predicates.

The public read methods require a :class:`~covenant_radar.db.scoping.Scope`
and compose its predicate into the SQL statement before execution.  There is
no post-query filtering and no way for a direct-id lookup to distinguish a
missing row from an out-of-scope row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Generic, Protocol, TypeVar, cast
from uuid import UUID

from sqlalchemy import Select, inspect, select
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.orm import Session

from covenant_radar.core.context import get_job_run_id, get_request_id
from covenant_radar.db.scoping import (
    AUDITOR_CALLER,
    RETENTION_JOB_CALLER,
    OwnershipPath,
    Scope,
    ScopeAuditError,
    UnscopedCaller,
    authorise_unscoped_caller,
    ownership_path_for,
)
from covenant_radar.db.session import is_database_session
from covenant_radar.ports.repository import Repository
from covenant_radar.security.rbac import Principal

ModelT = TypeVar("ModelT")
type ListType = list[Any]


class RepositoryAuditWriter(Protocol):
    """The C-60 audit boundary needed for unscoped repository reads."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one audit event in the caller's transaction."""


class RepositoryBase(Repository[ModelT, Scope], Generic[ModelT]):
    """A concrete SQLAlchemy implementation of the scope-carrying port."""

    def __init__(
        self,
        session: Session,
        model: type[ModelT],
        *,
        ownership: OwnershipPath | None = None,
        audit: RepositoryAuditWriter | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("RepositoryBase requires a SQLAlchemy Session.")
        try:
            inspect(model)
        except NoInspectionAvailable as error:
            raise TypeError(f"Repository model {model!r} is not SQLAlchemy-mapped.") from error
        self.session = session
        self.model = model
        mapped_model: Any = model
        self._id_column = mapped_model.id
        self.ownership = ownership or ownership_path_for(model)
        self.audit = audit

    def get(self, entity_id: UUID, *, scope: Scope) -> ModelT | None:
        """Return an in-scope row by id, or ``None`` for every other case."""
        if not isinstance(entity_id, UUID):
            raise TypeError("Repository ids must be UUID values.")
        statement = self._scoped_select(scope).where(self._id_column == entity_id)
        return cast(ModelT | None, self.session.execute(statement).scalars().one_or_none())

    def find(self, *, scope: Scope, **criteria: object) -> ModelT | None:
        """Return the first in-scope row matching safe model-column criteria."""
        statement = self._scoped_select(scope)
        statement = statement.where(*self._criteria(criteria)).limit(1)
        return cast(ModelT | None, self.session.execute(statement).scalars().one_or_none())

    def list(self, *, scope: Scope) -> Sequence[ModelT]:
        """Return all rows whose owning portfolio is in ``scope``."""
        statement = self._scoped_select(scope).order_by(self._id_column)
        return tuple(cast(Sequence[ModelT], self.session.execute(statement).scalars().all()))

    def add(self, entity: ModelT) -> None:
        """Stage an entity for insertion in the enclosing unit of work."""
        if not isinstance(entity, self.model):
            raise TypeError(
                f"{self.model.__name__} repository cannot stage {type(entity).__name__}."
            )
        self.session.add(entity)

    def get_unscoped(
        self,
        entity_id: UUID,
        *,
        caller: UnscopedCaller,
        principal: Principal | None = None,
        reason: str,
        request_id: str | None = None,
    ) -> ModelT | None:
        """Read by id through the audited, explicitly privileged escape hatch."""
        if not isinstance(entity_id, UUID):
            raise TypeError("Repository ids must be UUID values.")
        self._audit_unscoped(caller, principal, "get", reason, request_id)
        statement = select(self.model).where(self._id_column == entity_id)
        return cast(ModelT | None, self.session.execute(statement).scalars().one_or_none())

    def find_unscoped(
        self,
        *,
        caller: UnscopedCaller,
        principal: Principal | None = None,
        reason: str,
        request_id: str | None = None,
        **criteria: object,
    ) -> ModelT | None:
        """Find one row without portfolio filtering after mandatory audit."""
        self._audit_unscoped(caller, principal, "find", reason, request_id)
        statement = select(self.model).where(*self._criteria(criteria)).limit(1)
        return cast(ModelT | None, self.session.execute(statement).scalars().one_or_none())

    def list_unscoped(
        self,
        *,
        caller: UnscopedCaller,
        principal: Principal | None = None,
        reason: str,
        request_id: str | None = None,
    ) -> Sequence[ModelT]:
        """List rows for the auditor or retention job, with a C-60 event first."""
        self._audit_unscoped(caller, principal, "list", reason, request_id)
        statement = select(self.model).order_by(self._id_column)
        return tuple(cast(Sequence[ModelT], self.session.execute(statement).scalars().all()))

    # The verb-first aliases make the privileged nature visible at every call
    # site while retaining the noun-first names used by the repository port.
    def unscoped_get(self, *args: Any, **kwargs: Any) -> ModelT | None:
        return self.get_unscoped(*args, **kwargs)

    def unscoped_find(self, *args: Any, **kwargs: Any) -> ModelT | None:
        return self.find_unscoped(*args, **kwargs)

    def unscoped_list(self, *args: Any, **kwargs: Any) -> Sequence[ModelT]:
        return self.list_unscoped(*args, **kwargs)

    def _scoped_select(self, scope: Scope) -> Select[Any]:
        if not isinstance(scope, Scope):
            raise TypeError("Every repository read requires a covenant_radar.db.scoping.Scope.")
        statement: Select[Any] = select(self.model)
        statement = self.ownership.apply(statement)
        return statement.where(scope.predicate(self.ownership.path_column))

    def _criteria(self, criteria: dict[str, object]) -> ListType:
        try:
            mapper = inspect(self.model)
        except NoInspectionAvailable as error:
            raise TypeError(f"Repository model {self.model!r} is not SQLAlchemy-mapped.") from error
        column_names = frozenset(mapper.column_attrs.keys())
        clauses: ListType = []
        for name, value in criteria.items():
            if name not in column_names:
                raise ValueError(
                    f"Unknown repository criterion {name!r} for {self.model.__name__}."
                )
            column = getattr(self.model, name)
            clauses.append(column.is_(None) if value is None else column == value)
        return clauses

    def _audit_unscoped(
        self,
        caller: UnscopedCaller,
        principal: Principal | None,
        operation: str,
        reason: str,
        request_id: str | None,
    ) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An unscoped repository read requires a non-empty reason.")
        if len(reason) > 500:
            raise ValueError("An unscoped repository read reason is limited to 500 characters.")
        actor = authorise_unscoped_caller(self.session, caller, principal)
        if self.audit is None:
            raise ScopeAuditError(
                "Unscoped repository access is refused because its audit writer is not configured."
            )
        event_request_id = request_id or get_request_id() or get_job_run_id()
        if event_request_id is None:
            event_request_id = "system-unscoped-read"
        payload = {
            "outcome": "allowed",
            "caller": caller.value,
            "operation": operation,
            "repository": self.model.__name__,
            "reason": reason.strip(),
            "scope_bypassed": True,
        }
        try:
            self.audit.record(
                "repository_unscoped_read",
                ("repository", self.model.__name__),
                payload,
                actor=actor,
                request_id=event_request_id,
            )
        except Exception as error:
            raise ScopeAuditError(
                "Unscoped repository access is refused because the audit write failed."
            ) from error


# Names used by concrete adapters and by callers that prefer an explicit
# SQLAlchemy name over the generic repository-port name.
ScopedRepository = RepositoryBase
SqlAlchemyRepository = RepositoryBase


__all__ = [
    "RepositoryAuditWriter",
    "RepositoryBase",
    "ScopedRepository",
    "SqlAlchemyRepository",
    "AUDITOR_CALLER",
    "RETENTION_JOB_CALLER",
]
