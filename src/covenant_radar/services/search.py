"""Application service for scope-safe global search.

The repository owns SQL filtering and ranking.  This service owns the
request-level concerns that must not leak into persistence: per-entity
permission selection, permission-aware presentation, safe links and the
audit record required when personal-class data contributes to a result.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from urllib.parse import quote
from uuid import UUID

from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.context import get_request_id
from covenant_radar.core.errors import ExternalServiceError, ValidationError
from covenant_radar.db.repositories.search import (
    MAX_SEARCH_PAGE_SIZE,
    SearchQueryPage,
    SearchRepository,
    SearchRow,
    normalize_entity_types,
    normalize_query,
)
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.crypto import HMACFingerprinter
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

MAX_SNIPPET_LENGTH: Final[int] = 320

_ENTITY_PERMISSIONS: Final[dict[str, Permission]] = {
    "borrower": Permission.VIEW_BORROWER,
    "facility": Permission.VIEW_BORROWER,
    "covenant": Permission.VIEW_COVENANT,
    "document": Permission.VIEW_DOCUMENT,
    "memo": Permission.VIEW_MEMO,
    "case": Permission.VIEW_CASE,
    "audit_event": Permission.VIEW_AUDIT,
}
_ENTITY_LABELS: Final[dict[str, str]] = {
    "borrower": "Borrower",
    "facility": "Facility",
    "covenant": "Covenant",
    "document": "Document",
    "memo": "Memo",
    "case": "Case",
    "audit_event": "Audit event",
}


class SearchAuditWriter(Protocol):
    """The append-only audit boundary needed by personal-data search."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event to the current transaction."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A presentation-ready, already-authorized search result."""

    entity_type: str
    entity_id: UUID
    target_ref: str
    title: str
    subtitle: str | None
    snippet: str | None
    highlighted_title: Markup
    highlighted_snippet: Markup | None
    href: str
    entity_label: str
    score: int
    created_at: datetime
    match_source: str
    personal_match: bool

    @property
    def type(self) -> str:
        """Compatibility alias for callers that use ``type`` terminology."""
        return self.entity_type

    @property
    def id(self) -> UUID:
        """Compatibility alias for callers that use ``id`` terminology."""
        return self.entity_id


@dataclass(frozen=True, slots=True)
class SearchPage:
    """A bounded result page with a count from the same scoped relation."""

    query: str
    results: tuple[SearchResult, ...]
    total_count: int
    page_size: int
    offset: int
    entity_types: tuple[str, ...]
    is_recent: bool

    @property
    def rows(self) -> tuple[SearchResult, ...]:
        """Alias used by repository-shaped consumers."""
        return self.results

    @property
    def has_more(self) -> bool:
        """Whether another bounded page exists."""
        return self.offset + len(self.results) < self.total_count


class SearchService:
    """Enforce search permissions and adapt scoped rows for the browser."""

    def __init__(
        self,
        session: Session,
        *,
        audit: SearchAuditWriter | None = None,
        repository: SearchRepository | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        fingerprinter: HMACFingerprinter | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("SearchService requires a SQLAlchemy Session.")
        if audit is not None and not callable(getattr(audit, "record", None)):
            raise TypeError("SearchService audit must provide a callable record method.")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("SearchService scope_resolver must be callable.")
        if repository is not None and not isinstance(repository, SearchRepository):
            raise TypeError("SearchService repository must be a SearchRepository.")
        self.session = session
        self.audit = audit
        self.repository = repository or SearchRepository(session, fingerprinter=fingerprinter)
        self.scope_resolver = scope_resolver

    def available_entity_types(self, principal: Principal) -> tuple[str, ...]:
        """Return only entity selectors the caller may search."""
        _require_principal(principal)
        return tuple(
            entity_type
            for entity_type, permission in _ENTITY_PERMISSIONS.items()
            if principal.has(permission)
        )

    def search(
        self,
        principal: Principal,
        query: str,
        *,
        scope: Scope | None = None,
        entity_types: Iterable[str] | None = None,
        page_size: int = 50,
        offset: int = 0,
        request_id: str | None = None,
    ) -> SearchPage:
        """Search permitted entity types within the caller's portfolio scope."""
        _require_principal(principal)
        try:
            tokens, normalized_query = normalize_query(query)
            requested_types = normalize_entity_types(entity_types)
            _validate_page(page_size, offset)
        except (TypeError, ValueError) as error:
            raise ValidationError(str(error), field="search") from error

        permitted = set(self.available_entity_types(principal))
        selected_types = tuple(
            entity_type for entity_type in requested_types if entity_type in permitted
        )
        if scope is None:
            resolved_scope = self._resolve_scope(principal)
        elif isinstance(scope, Scope):
            resolved_scope = scope
        else:
            raise ValidationError("Search scope is invalid.", field="scope")
        if resolved_scope.principal_id != principal.id:
            raise ValidationError("Search scope belongs to another principal.", field="scope")

        try:
            raw_page = self.repository.query(
                normalized_query,
                scope=resolved_scope,
                entity_types=selected_types,
                include_personal=principal.has(Permission.READ_PERSONAL_DATA),
                page_size=page_size,
                offset=offset,
            )
        except (TypeError, ValueError) as error:
            raise ValidationError(str(error), field="search") from error

        if any(row.personal_match for row in raw_page.rows):
            self._audit_personal_access(
                principal,
                normalized_query,
                selected_types,
                raw_page,
                request_id=request_id,
            )
        return SearchPage(
            query=normalized_query,
            results=tuple(self._present(row, tokens=tokens) for row in raw_page.rows),
            total_count=raw_page.total_count,
            page_size=page_size,
            offset=offset,
            entity_types=selected_types,
            is_recent=not normalized_query,
        )

    def _resolve_scope(self, principal: Principal) -> Scope:
        if self.scope_resolver is not None:
            resolved = self.scope_resolver(principal)
        else:
            resolved = resolve_scope(principal, self.session)
        if not isinstance(resolved, Scope):
            raise TypeError("Search scope resolver returned an invalid Scope.")
        return resolved

    def _audit_personal_access(
        self,
        principal: Principal,
        query: str,
        entity_types: tuple[str, ...],
        page: SearchQueryPage,
        *,
        request_id: str | None,
    ) -> None:
        if self.audit is None:
            raise ExternalServiceError(
                "Personal-data search requires an available audit writer.",
                field="audit",
            )
        query_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        payload: dict[str, object] = {
            "action": "search_personal_data_accessed",
            "query_sha256": query_digest,
            "entity_types": list(entity_types),
            "result_count": page.total_count,
            "personal_page_result_count": sum(row.personal_match for row in page.rows),
        }
        self.audit.record(
            AuditEventType.MASTER_DATA_PERSONAL_DATA_ACCESSED.value,
            ("search", principal.id),
            payload,
            actor=principal.id,
            request_id=request_id or get_request_id() or "search",
        )

    @staticmethod
    def _present(row: SearchRow, *, tokens: tuple[str, ...]) -> SearchResult:
        snippet = _bounded_snippet(row.snippet or row.subtitle or row.title, tokens)
        return SearchResult(
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            target_ref=row.target_ref,
            title=row.title,
            subtitle=row.subtitle,
            snippet=snippet,
            highlighted_title=_highlight(row.title, tokens),
            highlighted_snippet=_highlight(snippet, tokens) if snippet else None,
            href=_href(row.entity_type, row.target_ref),
            entity_label=_ENTITY_LABELS.get(row.entity_type, row.entity_type),
            score=row.score,
            created_at=row.created_at,
            match_source=row.match_source,
            personal_match=row.personal_match,
        )


def _require_principal(principal: Principal) -> None:
    if not isinstance(principal, Principal):
        raise TypeError("Search requires an authenticated Principal.")


def _validate_page(page_size: int, offset: int) -> None:
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise ValueError("Search page_size must be an integer.")
    if not 1 <= page_size <= MAX_SEARCH_PAGE_SIZE:
        raise ValueError(f"Search page_size must be between 1 and {MAX_SEARCH_PAGE_SIZE}.")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError("Search offset must be an integer.")
    if offset < 0 or offset > 10_000:
        raise ValueError("Search offset must be between 0 and 10000.")


def _bounded_snippet(value: str | None, tokens: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) <= MAX_SNIPPET_LENGTH or not tokens:
        return text[:MAX_SNIPPET_LENGTH]
    lower = text.casefold()
    positions = [lower.find(token.casefold()) for token in tokens]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - 80)
    end = min(len(text), start + MAX_SNIPPET_LENGTH)
    if end - start < MAX_SNIPPET_LENGTH:
        start = max(0, end - MAX_SNIPPET_LENGTH)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _highlight(value: str, tokens: tuple[str, ...]) -> Markup:
    escaped = str(escape(value))
    if not tokens:
        return Markup(escaped)
    alternatives = "|".join(re.escape(token) for token in sorted(tokens, key=len, reverse=True))
    pattern = re.compile(alternatives, flags=re.IGNORECASE)
    return Markup(pattern.sub(lambda match: f"<mark>{match.group(0)}</mark>", escaped))


def _href(entity_type: str, target_ref: str) -> str:
    encoded = quote(target_ref, safe="")
    if entity_type == "document":
        return f"/documents/{encoded}/view"
    if entity_type == "audit_event":
        return f"/audit?subject_id={encoded}"
    if entity_type == "case":
        return f"/cases/{encoded}"
    if entity_type in {"borrower", "memo"}:
        return f"/borrowers/{encoded}"
    if entity_type == "facility":
        return f"/facilities/{encoded}"
    if entity_type == "covenant":
        return f"/covenants/{encoded}"
    # This branch is defensive: repository entity types are closed above.
    return f"/search?type={quote(entity_type, safe='')}&q={encoded}"


__all__ = [
    "MAX_SNIPPET_LENGTH",
    "SearchAuditWriter",
    "SearchPage",
    "SearchResult",
    "SearchService",
]
