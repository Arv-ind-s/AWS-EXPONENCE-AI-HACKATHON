"""Persistence adapter for saved views and recent-item history.

Saved views are filters, never materialised result sets.  The repository keeps
the persisted filter document versioned and validates it on both write and
read, so a malformed row cannot become an authorization bypass.  Access to a
shared row is checked against the recipient's current user and role grants;
the row is then filtered again by the caller's portfolio scope in the service
layer.

Recent items are represented by append-only ``recent_item_opened`` audit
events.  This keeps the history durable without introducing a second mutable
activity table, and means a lost scope can be filtered silently when the list
is read.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.errors import Conflict, ExternalServiceError, NotFound, ValidationError
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.identity import AppUser, Role, UserRole
from covenant_radar.db.models.views import SavedQueueView as SavedQueueViewRow
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.triage.views import QueueFilters
from covenant_radar.security.rbac import Principal, PrincipalKind

SAVED_VIEW_SCHEMA_VERSION: Final[int] = 1
RECENT_ITEM_OPENED_EVENT: Final[str] = "recent_item_opened"
MAX_RECENT_ITEMS: Final[int] = 100
MAX_VIEW_FILTER_BYTES: Final[int] = 64 * 1024
MAX_VIEW_DESCRIPTION: Final[int] = 500
MAX_VIEW_NAME: Final[int] = 100
_VIEW_KINDS: Final[frozenset[str]] = frozenset({"queue", "case", "search"})
_SHARE_ROLES: Final[frozenset[str]] = frozenset({"administrator", "admin"})


@dataclass(frozen=True, slots=True)
class ViewRecord:
    """A persisted view with its validated, JSON-compatible definition."""

    id: UUID
    owner_id: UUID
    name: str
    kind: str
    filters: Mapping[str, object]
    is_shared: bool
    shared_user_ids: tuple[UUID, ...]
    shared_role_codes: tuple[str, ...]
    share_all: bool
    is_default: bool
    description: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.owner_id, UUID):
            raise TypeError("A view id and owner_id must be UUID values.")
        if self.kind not in _VIEW_KINDS:
            raise ValueError(f"View kind must be one of {', '.join(sorted(_VIEW_KINDS))}.")
        if not isinstance(self.filters, Mapping):
            raise TypeError("View filters must be a mapping.")
        if self.version < 1:
            raise ValueError("View version must be positive.")

    @property
    def queue_filters(self) -> QueueFilters:
        """Return the queue filter value object for a queue view."""
        if self.kind != "queue":
            raise ValueError("Only queue views have queue filters.")
        return QueueFilters.from_value(self.filters)


@dataclass(frozen=True, slots=True)
class RecentItemRecord:
    """One recent subject reference before presentation and scope filtering."""

    subject_type: str
    subject_id: UUID
    opened_at: datetime


class ViewRepository:
    """SQLAlchemy adapter for views and audit-backed recent items."""

    def __init__(self, session: Session, *, clock: Clock | None = None) -> None:
        if not is_database_session(session):
            raise TypeError("ViewRepository requires a SQLAlchemy Session.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("ViewRepository clock must expose now().")
        self.session = session
        self.clock = clock or SystemClock()

    def create(
        self,
        owner_id: UUID,
        name: str,
        filters: Mapping[str, object] | QueueFilters,
        *,
        kind: str = "queue",
        is_shared: bool = False,
        share_all: bool = False,
        shared_user_ids: Iterable[UUID] = (),
        shared_role_codes: Iterable[str] = (),
        is_default: bool = False,
        description: str | None = None,
        now: datetime,
        request_id: str,
    ) -> ViewRecord:
        """Validate and stage a new view in the caller's transaction."""
        _require_user_id(owner_id, "owner_id")
        normalized_kind = _kind(kind)
        normalized_filters = _normalise_filters(filters, normalized_kind)
        normalized_name = _text(name, "name", MAX_VIEW_NAME)
        normalized_description = _optional_text(description, "description", MAX_VIEW_DESCRIPTION)
        normalized_user_ids = _user_ids(shared_user_ids)
        normalized_roles = _role_codes(shared_role_codes)
        self._validate_share_targets(owner_id, normalized_user_ids, normalized_roles)
        shared = bool(is_shared or share_all or normalized_user_ids or normalized_roles)
        share_all = bool(share_all or (shared and not normalized_user_ids and not normalized_roles))
        normalized_now = _aware_utc(now, "now")
        document = _encode_document(
            name=normalized_name,
            kind=normalized_kind,
            filters=normalized_filters,
            shared_user_ids=normalized_user_ids,
            shared_role_codes=normalized_roles,
            share_all=share_all,
            is_default=is_default,
        )
        row = SavedQueueViewRow.create(
            owner_id=owner_id,
            name=normalized_name,
            filter_json=document,
            is_shared=shared,
            description=normalized_description,
            created_at=normalized_now,
            updated_at=normalized_now,
            request_id=_request_id(request_id),
        )
        row.created_by_id = owner_id
        row.updated_by_id = owner_id
        self.session.add(row)
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as error:
            raise Conflict("The saved view could not be persisted.") from error
        if is_default:
            self._clear_other_defaults(owner_id, row.id, normalized_now, request_id)
        return _record(row)

    def get(self, view_id: UUID, principal: Principal) -> ViewRecord | None:
        """Return a view only when the caller is an owner or valid recipient."""
        _require_principal(principal)
        row = self.session.get(SavedQueueViewRow, _uuid(view_id, "view_id"))
        if row is None:
            return None
        self._transfer_deactivated_owner(row)
        record = _record(row)
        return record if self._accessible(record, principal) else None

    def list_for_user(self, principal: Principal) -> tuple[ViewRecord, ...]:
        """List discoverable views without exposing inaccessible rows."""
        _require_principal(principal)
        statement: Select[tuple[SavedQueueViewRow]] = select(SavedQueueViewRow).where(
            or_(
                SavedQueueViewRow.owner_id == principal.id,
                SavedQueueViewRow.is_shared.is_(True),
            )
        )
        rows = tuple(
            self.session.execute(
                statement.order_by(
                    SavedQueueViewRow.name,
                    SavedQueueViewRow.created_at,
                    SavedQueueViewRow.id,
                )
            ).scalars()
        )
        result: list[ViewRecord] = []
        for row in rows:
            self._transfer_deactivated_owner(row)
            record = _record(row)
            if self._accessible(record, principal):
                result.append(record)
        return tuple(result)

    def update(
        self,
        view_id: UUID,
        principal: Principal,
        *,
        expected_version: int,
        name: str | None = None,
        filters: Mapping[str, object] | QueueFilters | None = None,
        kind: str | None = None,
        description: str | None = None,
        now: datetime,
        request_id: str,
    ) -> ViewRecord:
        """Update an owned view with optimistic concurrency."""
        _require_principal(principal)
        row = self._owned_row(view_id, principal)
        _check_version(row, expected_version)
        current = _record(row)
        next_kind = _kind(kind or current.kind)
        next_name = _text(name, "name", MAX_VIEW_NAME) if name is not None else current.name
        next_filters = (
            _normalise_filters(filters, next_kind)
            if filters is not None
            else _normalise_filters(current.filters, next_kind)
        )
        next_description = (
            _optional_text(description, "description", MAX_VIEW_DESCRIPTION)
            if description is not None
            else current.description
        )
        normalized_now = _aware_utc(now, "now")
        row.name = next_name
        row.filter_json = _encode_document(
            name=next_name,
            kind=next_kind,
            filters=next_filters,
            shared_user_ids=current.shared_user_ids,
            shared_role_codes=current.shared_role_codes,
            share_all=current.share_all,
            is_default=current.is_default,
        )
        row.description = next_description
        row.updated_at = normalized_now
        row.updated_by_id = principal.id
        row.request_id = _request_id(request_id)
        row.version += 1
        self.session.flush()
        return _record(row)

    def share(
        self,
        view_id: UUID,
        principal: Principal,
        *,
        user_ids: Iterable[UUID] = (),
        role_codes: Iterable[str] = (),
        share_all: bool = False,
        now: datetime,
        request_id: str,
    ) -> ViewRecord:
        """Replace sharing recipients for an owned view."""
        _require_principal(principal)
        row = self._owned_row(view_id, principal)
        current = _record(row)
        normalized_user_ids = _user_ids(user_ids)
        normalized_roles = _role_codes(role_codes)
        self._validate_share_targets(principal.id, normalized_user_ids, normalized_roles)
        shared = bool(share_all or normalized_user_ids or normalized_roles)
        normalized_now = _aware_utc(now, "now")
        row.is_shared = shared
        row.filter_json = _encode_document(
            name=current.name,
            kind=current.kind,
            filters=current.filters,
            shared_user_ids=normalized_user_ids,
            shared_role_codes=normalized_roles,
            share_all=bool(share_all),
            is_default=current.is_default,
        )
        row.updated_at = normalized_now
        row.updated_by_id = principal.id
        row.request_id = _request_id(request_id)
        row.version += 1
        self.session.flush()
        return _record(row)

    def set_default(
        self,
        view_id: UUID,
        principal: Principal,
        *,
        now: datetime,
        request_id: str,
    ) -> ViewRecord:
        """Make one owned view the caller's default atomically in the unit of work."""
        _require_principal(principal)
        row = self._owned_row(view_id, principal)
        normalized_now = _aware_utc(now, "now")
        self._clear_other_defaults(principal.id, row.id, normalized_now, request_id)
        current = _record(row)
        row.filter_json = _replace_default(current, True)
        row.updated_at = normalized_now
        row.updated_by_id = principal.id
        row.request_id = _request_id(request_id)
        row.version += 1
        self.session.flush()
        return _record(row)

    def default_for_user(self, principal: Principal) -> ViewRecord | None:
        """Return the accessible default view, if one has been selected."""
        views = self.list_for_user(principal)
        defaults = tuple(view for view in views if view.is_default)
        return defaults[0] if defaults else None

    def delete(self, view_id: UUID, principal: Principal) -> None:
        """Delete an owned view."""
        _require_principal(principal)
        row = self._owned_row(view_id, principal)
        self.session.delete(row)
        self.session.flush()

    def recent_events(
        self, user_id: UUID, *, limit: int = MAX_RECENT_ITEMS
    ) -> tuple[RecentItemRecord, ...]:
        """Read recent-item audit subjects for one user in stable order."""
        _require_user_id(user_id, "user_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RECENT_ITEMS
        ):
            raise ValidationError(f"limit must be between 1 and {MAX_RECENT_ITEMS}.", field="limit")
        statement = (
            select(AuditEvent)
            .where(
                AuditEvent.actor_id == user_id,
                AuditEvent.event_type == RECENT_ITEM_OPENED_EVENT,
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.sequence.desc())
            .limit(limit)
        )
        result: list[RecentItemRecord] = []
        seen: set[tuple[str, UUID]] = set()
        for event in self.session.execute(statement).scalars():
            subject_type, subject_id = _recent_subject(event)
            key = (subject_type, subject_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                RecentItemRecord(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    opened_at=_aware_utc(event.occurred_at, "occurred_at"),
                )
            )
        return tuple(result)

    def _owned_row(self, view_id: UUID, principal: Principal) -> SavedQueueViewRow:
        row = self.session.get(SavedQueueViewRow, _uuid(view_id, "view_id"))
        if row is None or row.owner_id != principal.id:
            raise NotFound("Saved view was not found.")
        self._transfer_deactivated_owner(row)
        if row.owner_id != principal.id:
            raise NotFound("Saved view was not found.")
        return row

    def _accessible(self, view: ViewRecord, principal: Principal) -> bool:
        if view.owner_id == principal.id:
            return True
        if not view.is_shared:
            return False
        if view.share_all:
            return True
        if principal.id in view.shared_user_ids:
            return True
        if principal.kind is not PrincipalKind.USER or not view.shared_role_codes:
            return False
        role_codes = self.session.execute(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .join(AppUser, AppUser.id == UserRole.user_id)
            .where(
                UserRole.user_id == principal.id,
                AppUser.is_active.is_(True),
                Role.code.in_(view.shared_role_codes),
            )
        ).scalars()
        return any(True for _ in role_codes)

    def _validate_share_targets(
        self,
        owner_id: UUID,
        user_ids: Sequence[UUID],
        role_codes: Sequence[str],
    ) -> None:
        if user_ids:
            found = set(
                self.session.execute(
                    select(AppUser.id).where(AppUser.id.in_(user_ids), AppUser.is_active.is_(True))
                ).scalars()
            )
            if found != set(user_ids):
                raise ValidationError("Every shared user must be an active user.", field="user_ids")
            if owner_id in found:
                raise ValidationError(
                    "A view owner does not need to be a share recipient.", field="user_ids"
                )
        if role_codes:
            found_roles = set(
                self.session.execute(select(Role.code).where(Role.code.in_(role_codes))).scalars()
            )
            if found_roles != set(role_codes):
                raise ValidationError("Every shared role must exist.", field="role_codes")

    def _transfer_deactivated_owner(self, row: SavedQueueViewRow) -> None:
        owner = self.session.get(AppUser, row.owner_id)
        if owner is None or owner.is_active:
            return
        administrator = self.session.scalar(
            select(AppUser)
            .join(UserRole, UserRole.user_id == AppUser.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(AppUser.is_active.is_(True), Role.code.in_(_SHARE_ROLES))
            .order_by(AppUser.id)
            .limit(1)
        )
        if administrator is None:
            raise ExternalServiceError(
                "The saved view owner is inactive and no active administrator is available "
                "to receive ownership."
            )
        row.owner_id = administrator.id
        row.updated_by_id = administrator.id
        row.updated_at = _aware_utc(self.clock.now(), "clock.now()")
        row.version += 1
        self.session.flush()

    def _clear_other_defaults(
        self,
        owner_id: UUID,
        selected_id: UUID,
        now: datetime,
        request_id: str,
    ) -> None:
        rows = self.session.execute(
            select(SavedQueueViewRow)
            .where(
                SavedQueueViewRow.owner_id == owner_id,
                SavedQueueViewRow.id != selected_id,
            )
            .with_for_update()
        ).scalars()
        for row in rows:
            current = _record(row)
            if not current.is_default:
                continue
            row.filter_json = _replace_default(current, False)
            row.updated_at = now
            row.updated_by_id = owner_id
            row.request_id = _request_id(request_id)
            row.version += 1


SavedViewRepository = ViewRepository


def _record(row: SavedQueueViewRow) -> ViewRecord:
    try:
        document = _decode_document(row.filter_json, fallback_name=row.name)
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
        raise ExternalServiceError("The saved view contains an invalid filter document.") from error
    return ViewRecord(
        id=row.id,
        owner_id=row.owner_id,
        name=cast(str, document["name"]),
        kind=cast(str, document["kind"]),
        filters=cast(Mapping[str, object], document["filters"]),
        is_shared=bool(row.is_shared),
        shared_user_ids=cast(tuple[UUID, ...], document["shared_user_ids"]),
        shared_role_codes=cast(tuple[str, ...], document["shared_role_codes"]),
        share_all=bool(document["share_all"]) and bool(row.is_shared),
        is_default=bool(document["is_default"]),
        description=row.description,
        version=row.version,
        created_at=_aware_utc(row.created_at, "created_at"),
        updated_at=_aware_utc(row.updated_at, "updated_at"),
    )


def _decode_document(value: str, *, fallback_name: str) -> dict[str, object]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_VIEW_FILTER_BYTES:
        raise ValueError("filter document is too large")
    decoded = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(decoded, Mapping):
        raise ValueError("filter document must be an object")

    # T-074's original representation is deliberately accepted unchanged.
    if set(decoded) == {"name", "filters"}:
        name = _text(decoded.get("name", fallback_name), "name", MAX_VIEW_NAME)
        filters = decoded.get("filters")
        if not isinstance(filters, Mapping):
            raise ValueError("legacy filters must be an object")
        QueueFilters.from_value(filters)
        return {
            "name": name,
            "kind": "queue",
            "filters": dict(filters),
            "shared_user_ids": (),
            "shared_role_codes": (),
            # The original T-074 model only had the `is_shared` flag.  Such
            # rows are organisation-visible, which is the only safe
            # interpretation available without recipient metadata.
            "share_all": True,
            "is_default": False,
        }

    expected = {
        "schema_version",
        "name",
        "kind",
        "filters",
        "shared_user_ids",
        "shared_role_codes",
        "share_all",
        "is_default",
    }
    if set(decoded) != expected or decoded["schema_version"] != SAVED_VIEW_SCHEMA_VERSION:
        raise ValueError("filter document schema is unsupported")
    name = _text(decoded["name"], "name", MAX_VIEW_NAME)
    kind = _kind(decoded["kind"])
    filters = _normalise_filters(decoded["filters"], kind)
    user_ids = _user_ids(decoded["shared_user_ids"])
    role_codes = _role_codes(decoded["shared_role_codes"])
    share_all = _strict_bool(decoded["share_all"], "share_all")
    is_default = _strict_bool(decoded["is_default"], "is_default")
    return {
        "name": name,
        "kind": kind,
        "filters": filters,
        "shared_user_ids": user_ids,
        "shared_role_codes": role_codes,
        "share_all": share_all,
        "is_default": is_default,
    }


def _encode_document(
    *,
    name: str,
    kind: str,
    filters: Mapping[str, object],
    shared_user_ids: Sequence[UUID],
    shared_role_codes: Sequence[str],
    share_all: bool,
    is_default: bool,
) -> str:
    document = {
        "schema_version": SAVED_VIEW_SCHEMA_VERSION,
        "name": name,
        "kind": kind,
        "filters": dict(filters),
        "shared_user_ids": [str(value) for value in shared_user_ids],
        "shared_role_codes": list(shared_role_codes),
        "share_all": share_all,
        "is_default": is_default,
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_VIEW_FILTER_BYTES:
        raise ValidationError("View filters are too large.", field="filters")
    return encoded


def _replace_default(view: ViewRecord, is_default: bool) -> str:
    return _encode_document(
        name=view.name,
        kind=view.kind,
        filters=view.filters,
        shared_user_ids=view.shared_user_ids,
        shared_role_codes=view.shared_role_codes,
        share_all=view.share_all,
        is_default=is_default,
    )


def _normalise_filters(
    filters: Mapping[str, object] | QueueFilters | object,
    kind: str,
) -> dict[str, object]:
    if kind == "queue":
        try:
            return QueueFilters.from_value(filters).to_dict()
        except (TypeError, ValueError) as error:
            raise ValidationError(f"Invalid queue filters: {error}", field="filters") from error
    if not isinstance(filters, Mapping):
        raise ValidationError("Filters must be an object.", field="filters")
    allowed = {
        "case": frozenset({"state", "assignee_id", "borrower_id", "portfolio_id", "case_id"}),
        "search": frozenset({"query", "entity_types"}),
    }[kind]
    if any(not isinstance(key, str) for key in filters):
        raise ValidationError("Filter field names must be strings.", field="filters")
    unknown = sorted(set(filters) - allowed)
    if unknown:
        raise ValidationError(f"Unknown {kind} filter field {unknown[0]!r}.", field="filters")
    result: dict[str, object] = {}
    for key, value in filters.items():
        if not isinstance(key, str):
            raise ValidationError("Filter field names must be strings.", field="filters")
        result[key] = _json_value(value, f"filters.{key}")
    if kind == "case" and "state" in result:
        if result["state"] not in {"open", "in_progress", "monitoring", "escalated", "closed"}:
            raise ValidationError("case state is not supported.", field="filters.state")
    if kind == "search" and "query" in result:
        if not isinstance(result["query"], str) or len(result["query"]) > 200:
            raise ValidationError(
                "search query must be at most 200 characters.", field="filters.query"
            )
    if kind == "search" and "entity_types" in result:
        values = result["entity_types"]
        if (
            not isinstance(values, list)
            or len(values) > 10
            or not all(isinstance(item, str) for item in values)
        ):
            raise ValidationError(
                "search entity_types must be a list of at most 10 strings.",
                field="filters.entity_types",
            )
    return result


def _recent_subject(event: AuditEvent) -> tuple[str, UUID]:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    subject_type = payload.get("subject_type")
    subject_id = payload.get("subject_id")
    if not isinstance(subject_type, str) or not subject_type.strip():
        raise ExternalServiceError("A recent-item audit event has no subject type.")
    try:
        parsed_id = UUID(str(subject_id)) if subject_id is not None else event.subject_id
    except (TypeError, ValueError) as error:
        raise ExternalServiceError(
            "A recent-item audit event has an invalid subject id."
        ) from error
    return subject_type.strip(), parsed_id


def _json_value(value: object, field: str, *, depth: int = 0) -> object:
    if depth > 3:
        raise ValidationError("Filter nesting is too deep.", field=field)
    if value is None or isinstance(value, bool | int | float | str):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError("Filter numbers must be finite.", field=field)
        if isinstance(value, str) and len(value) > 200:
            raise ValidationError("Filter text is too long.", field=field)
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValidationError("Filter field names must be strings.", field=field)
        return {
            key: _json_value(item, f"{field}.{key}", depth=depth + 1) for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        if len(value) > 100:
            raise ValidationError("Filter lists are too long.", field=field)
        return [_json_value(item, field, depth=depth + 1) for item in value]
    raise ValidationError("Filter values must be JSON-compatible.", field=field)


def _require_principal(principal: Principal) -> None:
    if not isinstance(principal, Principal):
        raise TypeError("Saved views require an authenticated Principal.")
    if principal.kind is not PrincipalKind.USER:
        raise ValidationError("Saved views belong to users, not API keys.", field="principal")


def _require_user_id(value: object, field: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID.")
    return value


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{field} must be a valid UUID.", field=field) from error


def _kind(value: object) -> str:
    if not isinstance(value, str) or value not in _VIEW_KINDS:
        raise ValidationError(
            f"kind must be one of {', '.join(sorted(_VIEW_KINDS))}.", field="kind"
        )
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValidationError(
            f"{field} must be non-empty text of at most {maximum} characters.", field=field
        )
    clean = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ValidationError(f"{field} contains an invalid control character.", field=field)
    return clean


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum)


def _user_ids(values: Iterable[UUID] | object) -> tuple[UUID, ...]:
    if isinstance(values, str | bytes) or not isinstance(values, Iterable):
        raise ValidationError("shared_user_ids must be an iterable of UUIDs.", field="user_ids")
    try:
        parsed = tuple(sorted({_uuid(value, "user_id") for value in values}))
    except TypeError as error:
        raise ValidationError("shared_user_ids must be an iterable.", field="user_ids") from error
    if len(parsed) > 100:
        raise ValidationError("A view can be shared with at most 100 users.", field="user_ids")
    return parsed


def _role_codes(values: Iterable[str] | object) -> tuple[str, ...]:
    if isinstance(values, str | bytes) or not isinstance(values, Iterable):
        raise ValidationError(
            "shared_role_codes must be an iterable of strings.", field="role_codes"
        )
    try:
        parsed = tuple(sorted({_text(value, "role_code", 50) for value in values}))
    except TypeError as error:
        raise ValidationError(
            "shared_role_codes must be an iterable.", field="role_codes"
        ) from error
    if len(parsed) > 20:
        raise ValidationError("A view can be shared with at most 20 roles.", field="role_codes")
    return parsed


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _check_version(row: SavedQueueViewRow, expected_version: int) -> None:
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ValidationError(
            "expected_version must be a positive integer.", field="expected_version"
        )
    if row.version != expected_version:
        raise Conflict(
            f"Saved view changed since version {expected_version}; "
            f"current version is {row.version}."
        )


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


def _request_id(value: object) -> str:
    return _text(value, "request_id", 40)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r} is not allowed.")
        result[key] = item
    return result


__all__ = [
    "MAX_RECENT_ITEMS",
    "RECENT_ITEM_OPENED_EVENT",
    "RecentItemRecord",
    "SAVED_VIEW_SCHEMA_VERSION",
    "SavedViewRepository",
    "ViewRecord",
    "ViewRepository",
]
