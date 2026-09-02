"""Repository for user-created saved queue views with scoped sharing.

A saved view is a filter set stored per user with an option to share.
Sharing does not widen scope; a shared view applies only within the
recipient's portfolio permissions. Lost-access handling drops references
to portfolios no longer in the user's scope without migration.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from covenant_radar.core.errors import AuthorizationError, NotFound, ValidationError
from covenant_radar.db.models.views import SavedQueueView
from covenant_radar.db.scoping import Scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.triage.views import QueueFilters
from covenant_radar.domain.triage.views import SavedQueueView as SavedViewValue


class SavedViewRepository:
    """CRUD operations on saved queue views with scope-aware sharing."""

    def __init__(self, session: Session) -> None:
        if not is_database_session(session):
            raise TypeError("SavedViewRepository requires a SQLAlchemy Session.")
        self.session = session

    def create(
        self,
        owner_id: UUID,
        name: str,
        filters: QueueFilters | dict,
        *,
        is_shared: bool = False,
        description: str | None = None,
        now: datetime,
        request_id: str,
    ) -> SavedViewValue:
        """Create and persist one named filter set for the owner.

        Args:
            owner_id: UUID of the user creating the view
            name: Display name for the filter set
            filters: QueueFilters or mapping of filter values
            is_shared: Whether to make the view discoverable to other users
            description: Optional description of the view's purpose
            now: Current timestamp
            request_id: Request ID for audit trail

        Returns:
            A SavedView value object representing the persisted view.

        Raises:
            ValidationError: If the filters are invalid.
        """
        validated_filters = QueueFilters.from_value(filters)
        saved_view = SavedViewValue(name=name, filters=validated_filters)

        model = SavedQueueView.create(
            owner_id=owner_id,
            name=saved_view.name,
            filter_json=saved_view.to_json(),
            is_shared=is_shared,
            description=description,
            created_at=now,
            updated_at=now,
            request_id=request_id,
        )
        self.session.add(model)
        self.session.flush()

        return saved_view

    def get_by_id(self, view_id: UUID, principal_id: UUID) -> SavedViewValue | None:
        """Load one saved view by ID if accessible to the principal.

        A user can load their own views, or views shared by others.
        Returns None if the view does not exist or is not accessible.
        """
        query = select(SavedQueueView).where(
            and_(
                SavedQueueView.id == view_id,
                # Either the principal owns it, or it's shared
                (SavedQueueView.owner_id == principal_id) | (SavedQueueView.is_shared.is_(True))
            )
        )
        row = self.session.scalar(query)
        if row is None:
            return None
        return SavedViewValue.from_json(row.filter_json)

    def list_for_user(
        self,
        principal_id: UUID,
        *,
        owned_only: bool = False,
    ) -> tuple[SavedViewValue, ...]:
        """List all views accessible to the principal.

        By default returns owned views + shared views. If owned_only=True,
        returns only views created by the principal.
        """
        if owned_only:
            query = select(SavedQueueView).where(SavedQueueView.owner_id == principal_id)
        else:
            query = select(SavedQueueView).where(
                (SavedQueueView.owner_id == principal_id) | (SavedQueueView.is_shared.is_(True))
            )
        rows = self.session.scalars(query).all()
        return tuple(SavedViewValue.from_json(row.filter_json) for row in rows)

    def update(
        self,
        view_id: UUID,
        principal_id: UUID,
        *,
        name: str | None = None,
        filters: QueueFilters | dict | None = None,
        is_shared: bool | None = None,
        description: str | None = None,
        now: datetime,
        request_id: str,
    ) -> SavedViewValue:
        """Update one saved view if the principal owns it.

        Raises:
            NotFound: If the view does not exist.
            Unauthorized: If the principal does not own the view.
        """
        row = self.session.query(SavedQueueView).filter(SavedQueueView.id == view_id).first()
        if row is None:
            raise NotFound(f"Saved view {view_id} not found.")
        if row.owner_id != principal_id:
            raise AuthorizationError(f"User does not own saved view {view_id}.")

        # Apply updates
        if name is not None:
            try:
                validated_view = SavedViewValue(name=name, filters=row.filter_json)
                row.name = validated_view.name
            except ValueError as error:
                raise ValidationError(f"Invalid view name: {error}") from error

        if filters is not None:
            try:
                validated_filters = QueueFilters.from_value(filters)
                validated_view = SavedViewValue(name=row.name, filters=validated_filters)
                row.filter_json = validated_view.to_json()
            except ValueError as error:
                raise ValidationError(f"Invalid filters: {error}") from error

        if is_shared is not None:
            row.is_shared = is_shared

        if description is not None:
            row.description = description

        row.updated_at = now
        row.request_id = request_id
        self.session.flush()

        # Reload from JSON to ensure consistency
        return SavedViewValue.from_json(row.filter_json)

    def delete(self, view_id: UUID, principal_id: UUID) -> None:
        """Delete one saved view if the principal owns it.

        Raises:
            NotFound: If the view does not exist.
            Unauthorized: If the principal does not own the view.
        """
        row = self.session.query(SavedQueueView).filter(SavedQueueView.id == view_id).first()
        if row is None:
            raise NotFound(f"Saved view {view_id} not found.")
        if row.owner_id != principal_id:
            raise AuthorizationError(f"User does not own saved view {view_id}.")

        self.session.delete(row)
        self.session.flush()

    def apply_within_scope(
        self,
        view: SavedViewValue,
        scope: Scope,
    ) -> SavedViewValue:
        """Return a view with portfolio filters narrowed if the scope is empty.

        The full scoping check happens at query time in TriageRepository.
        This method only handles the case where a user has completely lost
        access (empty scope), in which case any portfolio filter is dropped.

        Returns a SavedView with narrowed filters if the scope is empty,
        or the original view otherwise. Does not mutate the input.
        """
        # If the scope is empty, drop portfolio filter
        if scope.is_empty and view.filters.portfolio is not None:
            narrowed_filters = QueueFilters(
                band=view.filters.band,
                portfolio=None,
                industry=view.filters.industry,
                assignee=view.filters.assignee,
                sma_band=view.filters.sma_band,
                case_state=view.filters.case_state,
            )
            return SavedViewValue(name=view.name, filters=narrowed_filters)

        return view


__all__ = ["SavedViewRepository"]
