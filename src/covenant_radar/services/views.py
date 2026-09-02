"""Use cases for saved views and recent-item navigation.

The service is deliberately the scope boundary for a view.  A shared view
contains criteria only; it never contains rows captured under the owner's
authority.  On every load, the criteria are applied to the recipient's
current scope and dangling references are removed with an explicit notice.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import (
    AuthorizationError,
    ExternalServiceError,
    NotFound,
    ValidationError,
)
from covenant_radar.db.models import (
    Borrower,
    Case,
    Covenant,
    Document,
    Facility,
    Forecast,
    Memo,
    Portfolio,
    Simulation,
)
from covenant_radar.db.repositories.view import (
    MAX_RECENT_ITEMS,
    RECENT_ITEM_OPENED_EVENT,
    ViewRecord,
    ViewRepository,
)
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.triage.views import QueueFilters
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize

_RECENT_SUBJECTS: Final[dict[str, type]] = {
    "borrower": Borrower,
    "facility": Facility,
    "covenant": Covenant,
    "document": Document,
    "memo": Memo,
    "case": Case,
    "forecast": Forecast,
    "simulation": Simulation,
}
_RECENT_PERMISSIONS: Final[dict[str, Permission]] = {
    "borrower": Permission.VIEW_BORROWER,
    "facility": Permission.VIEW_BORROWER,
    "covenant": Permission.VIEW_COVENANT,
    "document": Permission.VIEW_DOCUMENT,
    "memo": Permission.VIEW_MEMO,
    "case": Permission.VIEW_CASE,
    "forecast": Permission.VIEW_FORECAST,
    "simulation": Permission.RUN_SIMULATION,
}
_RECENT_LABELS: Final[dict[str, str]] = {
    "borrower": "Borrower",
    "facility": "Facility",
    "covenant": "Covenant",
    "document": "Document",
    "memo": "Memo",
    "case": "Case",
    "forecast": "Forecast",
    "simulation": "Simulation",
}
_DANGLING_FILTER_MESSAGE: Final[str] = "A deleted filter target was removed from this view."
_SCOPE_FILTER_MESSAGE: Final[str] = (
    "This shared view is narrowed to your current portfolio scope; some rows may be hidden."
)


class ViewAuditWriter(Protocol):
    """The optional append-only audit boundary for view activity."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an event to the caller's transaction."""


@dataclass(frozen=True, slots=True)
class AppliedView:
    """A view after recipient-scope application."""

    view: ViewRecord
    filters: Mapping[str, object]
    notice: str | None = None
    dropped_filters: tuple[str, ...] = ()

    @property
    def id(self) -> UUID:
        return self.view.id

    @property
    def name(self) -> str:
        return self.view.name

    @property
    def kind(self) -> str:
        return self.view.kind

    @property
    def is_default(self) -> bool:
        return self.view.is_default

    @property
    def queue_filters(self) -> QueueFilters:
        if self.kind != "queue":
            raise ValueError("Only queue views have queue filters.")
        return QueueFilters.from_value(self.filters)


@dataclass(frozen=True, slots=True)
class RecentItem:
    """A display-safe, currently visible recent item."""

    subject_type: str
    subject_id: UUID
    label: str
    title: str
    href: str
    opened_at: datetime

    @property
    def id(self) -> UUID:
        return self.subject_id


class ViewService:
    """Coordinate durable view definitions and scoped recent navigation."""

    def __init__(
        self,
        session: Session,
        *,
        repository: ViewRepository | None = None,
        audit: ViewAuditWriter | None = None,
        clock: Clock | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        request_id: str | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("ViewService requires a SQLAlchemy Session.")
        if repository is not None and not isinstance(repository, ViewRepository):
            raise TypeError("ViewService repository must be a ViewRepository.")
        if audit is not None and not callable(getattr(audit, "record", None)):
            raise TypeError("ViewService audit must expose record(...).")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("ViewService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("ViewService scope_resolver must be callable.")
        if repository is not None and repository.session is not session:
            raise ValueError("ViewService repository must use the same session.")
        self.session = session
        self.repository = repository or ViewRepository(session, clock=clock)
        self.audit = audit
        self.clock = clock or SystemClock()
        self.scope_resolver = scope_resolver
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())

    def create(
        self,
        principal: Principal,
        *,
        name: str,
        filters: Mapping[str, object] | QueueFilters,
        kind: str = "queue",
        shared_user_ids: Iterable[UUID] = (),
        shared_role_codes: Iterable[str] = (),
        share_all: bool = False,
        is_default: bool = False,
        description: str | None = None,
        request_id: str | None = None,
    ) -> ViewRecord:
        """Persist a named filter definition owned by the caller."""
        self._require_view_permission(principal)
        self._require_user_principal(principal)
        normalized_user_ids = tuple(shared_user_ids)
        normalized_role_codes = tuple(shared_role_codes)
        row = self.repository.create(
            principal.id,
            name,
            filters,
            kind=kind,
            is_shared=share_all or bool(normalized_user_ids) or bool(normalized_role_codes),
            share_all=share_all,
            shared_user_ids=normalized_user_ids,
            shared_role_codes=normalized_role_codes,
            is_default=is_default,
            description=description,
            now=self._now(),
            request_id=self._write_request_id(request_id),
        )
        self._audit(
            "saved_view_created",
            row.id,
            {"name": row.name, "kind": row.kind, "is_default": row.is_default},
            principal,
            request_id=request_id,
        )
        return row

    def list_views(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
    ) -> tuple[AppliedView, ...]:
        """List owned and shared views already narrowed to caller scope."""
        self._require_view_permission(principal)
        resolved_scope = self._scope(principal, scope)
        return tuple(
            self.apply(view, principal, resolved_scope)
            for view in self.repository.list_for_user(principal)
        )

    def get_view(
        self,
        principal: Principal,
        view_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> AppliedView:
        """Load one accessible view and apply the current recipient scope."""
        self._require_view_permission(principal)
        resolved_scope = self._scope(principal, scope)
        view = self.repository.get(view_id, principal)
        if view is None:
            raise NotFound("Saved view was not found.")
        return self.apply(view, principal, resolved_scope)

    def apply(self, view: ViewRecord, principal: Principal, scope: Scope) -> AppliedView:
        """Apply a filter definition within a recipient's current scope."""
        self._require_view_permission(principal)
        if not isinstance(view, ViewRecord):
            raise TypeError("apply requires a ViewRecord.")
        if not isinstance(scope, Scope) or scope.principal_id != principal.id:
            raise ValidationError("View scope belongs to another principal.", field="scope")
        filters = dict(view.filters)
        dropped: list[str] = []
        notices: list[str] = []
        if view.kind == "queue":
            filters, queue_notice, queue_dropped = self._apply_queue_filters(filters, scope)
            if queue_notice:
                notices.append(queue_notice)
            dropped.extend(queue_dropped)
        else:
            filters, generic_notice, generic_dropped = self._apply_target_filters(
                filters, view.kind, scope
            )
            if generic_notice:
                notices.append(generic_notice)
            dropped.extend(generic_dropped)
        return AppliedView(
            view=view,
            filters=filters,
            notice=" ".join(dict.fromkeys(notices)) or None,
            dropped_filters=tuple(dropped),
        )

    def default_view(
        self,
        principal: Principal,
        *,
        kind: str = "queue",
        scope: Scope | None = None,
        request_id: str | None = None,
    ) -> AppliedView:
        """Return the caller's selected default, creating an explicit empty one."""
        self._require_view_permission(principal)
        self._require_user_principal(principal)
        resolved_scope = self._scope(principal, scope)
        current = self.repository.default_for_user(principal)
        if current is None or current.kind != kind:
            current = self.create(
                principal,
                name="All borrowers" if kind == "queue" else f"All {kind} items",
                filters={},
                kind=kind,
                is_default=True,
                request_id=request_id,
            )
        return self.apply(current, principal, resolved_scope)

    def update(
        self,
        principal: Principal,
        view_id: UUID,
        *,
        expected_version: int,
        name: str | None = None,
        filters: Mapping[str, object] | QueueFilters | None = None,
        kind: str | None = None,
        description: str | None = None,
        request_id: str | None = None,
    ) -> ViewRecord:
        """Update an owned definition without allowing stale overwrites."""
        self._require_view_permission(principal)
        self._require_user_principal(principal)
        row = self.repository.update(
            view_id,
            principal,
            expected_version=expected_version,
            name=name,
            filters=filters,
            kind=kind,
            description=description,
            now=self._now(),
            request_id=self._write_request_id(request_id),
        )
        self._audit(
            "saved_view_updated",
            row.id,
            {"name": row.name, "kind": row.kind},
            principal,
            request_id=request_id,
        )
        return row

    def set_default(
        self,
        principal: Principal,
        view_id: UUID,
        *,
        request_id: str | None = None,
    ) -> ViewRecord:
        """Select one owned view as the caller's default."""
        self._require_view_permission(principal)
        self._require_user_principal(principal)
        row = self.repository.set_default(
            view_id,
            principal,
            now=self._now(),
            request_id=self._write_request_id(request_id),
        )
        self._audit(
            "saved_view_default_changed",
            row.id,
            {"is_default": True},
            principal,
            request_id=request_id,
        )
        return row

    def share(
        self,
        principal: Principal,
        view_id: UUID,
        *,
        user_ids: Iterable[UUID] = (),
        role_codes: Iterable[str] = (),
        share_all: bool = False,
        request_id: str | None = None,
    ) -> ViewRecord:
        """Replace the owner-controlled user/role sharing policy."""
        self._require_view_permission(principal)
        self._require_user_principal(principal)
        row = self.repository.share(
            view_id,
            principal,
            user_ids=user_ids,
            role_codes=role_codes,
            share_all=share_all,
            now=self._now(),
            request_id=self._write_request_id(request_id),
        )
        self._audit(
            "saved_view_shared",
            row.id,
            {
                "shared_user_count": len(row.shared_user_ids),
                "shared_role_count": len(row.shared_role_codes),
                "share_all": row.share_all,
            },
            principal,
            request_id=request_id,
        )
        return row

    def delete(self, principal: Principal, view_id: UUID, *, request_id: str | None = None) -> None:
        """Delete one owned saved view."""
        self._require_view_permission(principal)
        self._require_user_principal(principal)
        self.repository.delete(view_id, principal)
        self._audit("saved_view_deleted", view_id, {}, principal, request_id=request_id)

    def record_recent_item(
        self,
        principal: Principal,
        *,
        subject_type: str,
        subject_id: UUID,
        scope: Scope | None = None,
        request_id: str | None = None,
    ) -> None:
        """Record one authorized navigation target without storing its content."""
        self._require_view_permission(principal)
        self._require_user_principal(principal)
        model = _subject_model(subject_type)
        entity_id = _uuid(subject_id, "subject_id")
        resolved_scope = self._scope(principal, scope)
        if not self._subject_visible(model, entity_id, principal, resolved_scope):
            raise NotFound("Recent item was not found.")
        if self.audit is None:
            raise ExternalServiceError(
                "Recent-item history is unavailable because audit recording is not configured."
            )
        resolved_request_id = self._write_request_id(request_id)
        self.audit.record(
            RECENT_ITEM_OPENED_EVENT,
            ("recent_item", entity_id),
            {"subject_type": subject_type, "subject_id": str(entity_id)},
            actor=principal.id,
            request_id=resolved_request_id,
        )

    def recent_items(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        limit: int = MAX_RECENT_ITEMS,
    ) -> tuple[RecentItem, ...]:
        """Return only recent subjects still visible to the caller."""
        self._require_view_permission(principal)
        resolved_scope = self._scope(principal, scope)
        records = self.repository.recent_events(principal.id, limit=limit)
        result: list[RecentItem] = []
        for record in records:
            model = _RECENT_SUBJECTS.get(record.subject_type)
            if model is None or not principal.has(_RECENT_PERMISSIONS[record.subject_type]):
                continue
            row = self._visible_row(model, record.subject_id, principal, resolved_scope)
            if row is None:
                continue
            title = _subject_title(record.subject_type, row)
            result.append(
                RecentItem(
                    subject_type=record.subject_type,
                    subject_id=record.subject_id,
                    label=_RECENT_LABELS[record.subject_type],
                    title=title,
                    href=_subject_href(record.subject_type, row),
                    opened_at=record.opened_at,
                )
            )
        return tuple(result)

    # Compatibility-facing names used by callers that prefer saved-view terms.
    save = create
    list_saved_views = list_views
    get_saved_view = get_view
    load = get_view
    recent = recent_items

    def _scope(self, principal: Principal, scope: Scope | None) -> Scope:
        if scope is None:
            resolved = (
                self.scope_resolver(principal)
                if self.scope_resolver
                else resolve_scope(principal, self.session)
            )
        else:
            resolved = scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise ValidationError("View scope belongs to another principal.", field="scope")
        return resolved

    def _apply_queue_filters(
        self, filters: Mapping[str, object], scope: Scope
    ) -> tuple[dict[str, object], str | None, tuple[str, ...]]:
        queue_filters = QueueFilters.from_value(filters)
        portfolio = queue_filters.portfolio
        if portfolio is None:
            return queue_filters.to_dict(), None, ()
        target = self._portfolio_target(portfolio)
        if target is None:
            narrowed = QueueFilters(
                band=queue_filters.band,
                portfolio=None,
                industry=queue_filters.industry,
                assignee=queue_filters.assignee,
                sma_band=queue_filters.sma_band,
                case_state=queue_filters.case_state,
            )
            return narrowed.to_dict(), _DANGLING_FILTER_MESSAGE, ("portfolio",)
        if not _path_in_scope(target.path, scope):
            return queue_filters.to_dict(), _SCOPE_FILTER_MESSAGE, ()
        return queue_filters.to_dict(), None, ()

    def _apply_target_filters(
        self, filters: Mapping[str, object], kind: str, scope: Scope
    ) -> tuple[dict[str, object], str | None, tuple[str, ...]]:
        result = dict(filters)
        dropped: list[str] = []
        notices: list[str] = []
        target_models: dict[str, type] = {
            "portfolio_id": Portfolio,
            "borrower_id": Borrower,
            "case_id": Case,
        }
        for field, model in target_models.items():
            if field not in result:
                continue
            try:
                target_id = _uuid(result[field], f"filters.{field}")
            except ValidationError:
                raise
            if self.session.get(model, target_id) is None:
                result.pop(field)
                dropped.append(field)
                notices.append(_DANGLING_FILTER_MESSAGE)
            elif not self._subject_visible(model, target_id, _principal_for_scope(scope), scope):
                notices.append(_SCOPE_FILTER_MESSAGE)
        return result, " ".join(dict.fromkeys(notices)) or None, tuple(dropped)

    def _portfolio_target(self, value: UUID | str) -> Portfolio | None:
        if isinstance(value, UUID):
            return self.session.get(Portfolio, value)
        return self.session.scalar(
            select(Portfolio).where(
                (Portfolio.code == value)
                | (Portfolio.path == value)
                | (Portfolio.path == f"{value}/")
            )
        )

    def _subject_visible(
        self, model: type, entity_id: UUID, principal: Principal, scope: Scope
    ) -> bool:
        return self._visible_row(model, entity_id, principal, scope) is not None

    def _visible_row(
        self, model: Any, entity_id: UUID, principal: Principal, scope: Scope
    ) -> Any | None:
        permission = next(
            (value for key, value in _RECENT_PERMISSIONS.items() if _RECENT_SUBJECTS[key] is model),
            Permission.VIEW_QUEUE,
        )
        if not principal.has(permission):
            return None
        ownership = ownership_path_for(model)
        statement: Select[Any] = select(model).where(model.id == entity_id)
        statement = ownership.apply(statement).where(scope.predicate(ownership.path_column))
        return self.session.execute(statement).scalars().one_or_none()

    def _require_view_permission(self, principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise TypeError("Saved views require an authenticated Principal.")
        authorize(principal, Permission.VIEW_QUEUE)

    @staticmethod
    def _require_user_principal(principal: Principal) -> None:
        if principal.kind is not PrincipalKind.USER:
            raise AuthorizationError(
                "Saved views are available to browser users only.", field="principal"
            )

    def _audit(
        self,
        event_type: str,
        subject_id: UUID,
        payload: Mapping[str, object],
        principal: Principal,
        *,
        request_id: str | None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            event_type,
            ("saved_view", subject_id),
            payload,
            actor=principal.id,
            request_id=self._write_request_id(request_id),
        )

    def _now(self) -> datetime:
        value = self.clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ViewService clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)

    def _write_request_id(self, value: str | None) -> str:
        return _request_id(value or get_request_id() or self.request_id)


SavedViewService = ViewService
ViewsService = ViewService


def _subject_model(subject_type: str) -> type:
    if not isinstance(subject_type, str) or subject_type not in _RECENT_SUBJECTS:
        raise ValidationError("subject_type is not supported.", field="subject_type")
    return _RECENT_SUBJECTS[subject_type]


def _subject_title(subject_type: str, row: Any) -> str:
    for field in ("reference", "legal_name", "filename", "name"):
        value = getattr(row, field, None)
        if isinstance(value, str) and value:
            return value
    return f"{_RECENT_LABELS[subject_type]} {row.id}"


def _subject_href(subject_type: str, row: Any) -> str:
    value = getattr(row, "reference", None) or str(row.id)
    encoded = quote(str(value), safe="")
    paths = {
        "borrower": f"/borrowers/{encoded}",
        "facility": f"/facilities/{encoded}",
        "covenant": f"/covenants/{encoded}",
        "document": f"/documents/{encoded}/view",
        "memo": f"/borrowers/{encoded}",
        "case": f"/cases/{encoded}",
        "forecast": f"/forecasts/{encoded}",
        "simulation": f"/simulator/{encoded}",
    }
    return paths[subject_type]


def _principal_for_scope(scope: Scope) -> Principal:
    """Build a minimally-authorized principal for internal scope predicates."""
    return Principal.user(scope.principal_id, tuple(_RECENT_PERMISSIONS.values()))


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{field} must be a valid UUID.", field=field) from error


def _path_in_scope(path: str, scope: Scope) -> bool:
    normalized = path if path.endswith("/") else f"{path}/"
    return normalized in scope.exact_paths or any(
        normalized.startswith(prefix) for prefix in scope.descendant_paths
    )


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40:
        raise ValueError("View request_id must be between 1 and 40 characters.")
    return value.strip()


__all__ = [
    "AppliedView",
    "RecentItem",
    "SavedViewService",
    "ViewAuditWriter",
    "ViewService",
    "ViewsService",
]
