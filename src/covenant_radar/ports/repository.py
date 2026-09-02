"""The `Repository[T]` port: a read that cannot forget the caller's scope.

`get`, `find` and `list` take `scope` as a required, keyword-only argument
with no default, so a method that could return a row outside the caller's
authority does not exist: a caller who omits it fails at the call, both
under a type checker and at run time — Python itself raises `TypeError`
for a missing required keyword-only argument — rather than the method
quietly running an unscoped query. This module fixes that shape; the
concrete, SQLAlchemy-backed base with real scope-predicate composition is
`db/repositories/base.py` (`T-016`), once `Scope` itself exists.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Generic, TypeVar
from uuid import UUID

ModelT = TypeVar("ModelT")
ScopeT = TypeVar("ScopeT")


class Repository(ABC, Generic[ModelT, ScopeT]):
    """The read/write surface every concrete repository implements.

    Generic over the entity type `ModelT` and the caller's scope type
    `ScopeT`, so a reference-data repository with no notion of portfolio
    scope and a business-entity repository scoped by portfolio path both
    satisfy the same contract with their own, distinct `ScopeT`.
    """

    @abstractmethod
    def get(self, entity_id: UUID, *, scope: ScopeT) -> ModelT | None:
        """Return the entity by id, or `None` if it does not exist or
        falls outside `scope`. Never distinguishes the two to the caller —
        that distinction is an authorization detail the service layer
        decides how to expose, not a fact this method leaks."""

    @abstractmethod
    def find(self, *, scope: ScopeT, **criteria: object) -> ModelT | None:
        """Return the first entity within `scope` matching `criteria`."""

    @abstractmethod
    def list(self, *, scope: ScopeT) -> Sequence[ModelT]:
        """Return every entity within `scope`."""

    @abstractmethod
    def add(self, entity: ModelT) -> None:
        """Stage `entity` for insertion in the caller's unit of work."""
