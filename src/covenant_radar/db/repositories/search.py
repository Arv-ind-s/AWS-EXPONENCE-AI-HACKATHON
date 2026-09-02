"""Scope-safe, portable search over the application's searchable records.

Search is deliberately implemented as a union of entity-specific statements
instead of a post-query filter.  That keeps the portfolio predicate in the
database query for every entity and, importantly, makes the count use the
same filtered relation as the result page.  The statements use ``LIKE``
predicates rather than a database-specific full-text extension so SQLite,
PostgreSQL and the offline evaluation database have identical semantics.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import (
    Select,
    String,
    Text,
    and_,
    case,
    exists,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.orm import Session

from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantException,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
    CovenantWaiver,
)
from covenant_radar.db.models.document import Document, DocumentPage
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastDriver,
    ForecastRun,
    Simulation,
    TriageEntry,
)
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import CertificateRequest, EvidenceItem, SignalEvent
from covenant_radar.db.models.workflow import Case, Memo
from covenant_radar.db.scoping import Scope, ownership_path_for
from covenant_radar.db.session import is_database_session
from covenant_radar.security.crypto import HMACFingerprinter

SEARCH_ENTITY_TYPES: Final[tuple[str, ...]] = (
    "borrower",
    "facility",
    "covenant",
    "document",
    "memo",
    "case",
    "audit_event",
)
SEARCH_ENTITY_ALIASES: Final[dict[str, str]] = {
    "borrowers": "borrower",
    "facilities": "facility",
    "covenants": "covenant",
    "documents": "document",
    "memos": "memo",
    "cases": "case",
    "audit": "audit_event",
    "audits": "audit_event",
    "audit-events": "audit_event",
    "audit_events": "audit_event",
}
MAX_SEARCH_QUERY_LENGTH: Final[int] = 200
MAX_SEARCH_PAGE_SIZE: Final[int] = 100
MAX_SEARCH_OFFSET: Final[int] = 10_000

_RESULT_COLUMNS: Final[tuple[str, ...]] = (
    "entity_type",
    "entity_id",
    "target_ref",
    "title",
    "subtitle",
    "snippet",
    "created_at",
    "score",
    "match_source",
    "personal_match",
)


@dataclass(frozen=True, slots=True)
class SearchRow:
    """One raw, already-scoped row returned by :class:`SearchRepository`."""

    entity_type: str
    entity_id: UUID
    target_ref: str
    title: str
    subtitle: str | None
    snippet: str | None
    created_at: datetime
    score: int
    match_source: str
    personal_match: bool


@dataclass(frozen=True, slots=True)
class SearchQueryPage:
    """The bounded result page and its scope-safe total."""

    rows: tuple[SearchRow, ...]
    total_count: int


class SearchRepository:
    """Read-only search adapter with mandatory portfolio scoping."""

    def __init__(
        self,
        session: Session,
        *,
        fingerprinter: HMACFingerprinter | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("SearchRepository requires a SQLAlchemy Session.")
        self.session = session
        self.fingerprinter = fingerprinter

    def query(
        self,
        query: str,
        *,
        scope: Scope,
        entity_types: Iterable[str] | None = None,
        include_personal: bool = False,
        page_size: int = 50,
        offset: int = 0,
    ) -> SearchQueryPage:
        """Return one ranked, scope-filtered page and its exact count.

        ``include_personal`` never causes encrypted columns to be searched as
        plaintext.  When a fingerprinter is configured, an exact CIN lookup
        can use the stored non-reversible fingerprint.  Other encrypted
        fields remain unavailable to SQL search by design.
        """

        tokens, normalized_query = normalize_query(query)
        normalized_types = normalize_entity_types(entity_types)
        size = _page_size(page_size)
        position = _offset(offset)
        if not isinstance(scope, Scope):
            raise TypeError("SearchRepository.query requires a Scope.")
        if not isinstance(include_personal, bool):
            raise TypeError("SearchRepository.include_personal must be a bool.")

        branches = [
            self._branch(
                entity_type,
                tokens=tokens,
                query=normalized_query,
                scope=scope,
                include_personal=include_personal,
            )
            for entity_type in normalized_types
        ]
        if not branches:
            return SearchQueryPage(rows=(), total_count=0)

        relation = union_all(*branches).subquery("scoped_search")
        count_statement = select(func.count()).select_from(relation)
        count_value = self.session.scalar(count_statement)
        if isinstance(count_value, bool) or not isinstance(count_value, int):
            raise RuntimeError("The search count returned an invalid value.")

        statement: Select[Any] = (
            select(*[relation.c[name] for name in _RESULT_COLUMNS])
            .select_from(relation)
            .order_by(
                relation.c.score.desc(),
                relation.c.created_at.desc(),
                relation.c.entity_type.asc(),
                relation.c.entity_id.asc(),
            )
            .limit(size)
            .offset(position)
        )
        rows = tuple(_search_row(row) for row in self.session.execute(statement).mappings().all())
        return SearchQueryPage(rows=rows, total_count=count_value)

    search = query

    def build_statement(
        self,
        query: str,
        *,
        scope: Scope,
        entity_types: Iterable[str] | None = None,
        include_personal: bool = False,
    ) -> Select[Any]:
        """Build the scoped query for diagnostics and explain-plan checks."""

        tokens, normalized_query = normalize_query(query)
        normalized_types = normalize_entity_types(entity_types)
        branches = [
            self._branch(
                entity_type,
                tokens=tokens,
                query=normalized_query,
                scope=scope,
                include_personal=include_personal,
            )
            for entity_type in normalized_types
        ]
        if not branches:
            return select(literal(0).label("empty_search"))
        return cast(Select[Any], union_all(*branches))

    def _branch(
        self,
        entity_type: str,
        *,
        tokens: tuple[str, ...],
        query: str,
        scope: Scope,
        include_personal: bool,
    ) -> Select[Any]:
        if entity_type == "borrower":
            return self._borrowers(tokens, query, scope, include_personal)
        if entity_type == "facility":
            return self._facilities(tokens, query, scope)
        if entity_type == "covenant":
            return self._covenants(tokens, query, scope)
        if entity_type == "document":
            return self._documents(tokens, query, scope)
        if entity_type == "memo":
            return self._memos(tokens, query, scope)
        if entity_type == "case":
            return self._cases(tokens, query, scope)
        if entity_type == "audit_event":
            return self._audit_events(tokens, query, scope, include_personal)
        raise ValueError(f"Unsupported search entity type {entity_type!r}.")

    def _borrowers(
        self,
        tokens: tuple[str, ...],
        query: str,
        scope: Scope,
        include_personal: bool,
    ) -> Select[Any]:
        public_columns = (
            Borrower.reference,
            Borrower.legal_name,
            Borrower.industry_code,
            Borrower.constitution,
        )
        personal_predicate = self._cin_match(query, tokens, include_personal)
        predicates = [_all_tokens(public_columns, tokens)]
        if personal_predicate is not None:
            predicates.append(personal_predicate)
        match = or_(*predicates)
        personal_match = (
            case((personal_predicate, True), else_=False)
            if personal_predicate is not None
            else literal(False)
        )
        score = _rank(public_columns, tokens, query)
        if personal_predicate is not None:
            score = score + case((personal_predicate, 120), else_=0)
        statement = select(
            *_result_columns(
                "borrower",
                Borrower.id,
                Borrower.reference,
                Borrower.legal_name,
                Borrower.reference,
                Borrower.industry_code,
                Borrower.created_at,
                score,
                "borrower fields",
                personal_match,
            )
        ).select_from(Borrower)
        statement = _apply_ownership(statement, Borrower, scope)
        return statement.where(match)

    def _facilities(
        self,
        tokens: tuple[str, ...],
        query: str,
        scope: Scope,
    ) -> Select[Any]:
        columns = (
            Facility.reference,
            Facility.facility_type,
            Facility.security_type,
            Borrower.legal_name,
        )
        statement = select(
            *_result_columns(
                "facility",
                Facility.id,
                Facility.reference,
                Facility.reference,
                Borrower.legal_name,
                Facility.facility_type,
                Facility.created_at,
                _rank(columns, tokens, query),
                "facility fields",
                literal(False),
            )
        ).select_from(Facility)
        statement = statement.join(Borrower, Facility.borrower_id == Borrower.id)
        statement = statement.join(Portfolio, Borrower.portfolio_id == Portfolio.id)
        return statement.where(scope.predicate(Portfolio.path), _all_tokens(columns, tokens))

    def _covenants(
        self,
        tokens: tuple[str, ...],
        query: str,
        scope: Scope,
    ) -> Select[Any]:
        columns = (
            Covenant.reference,
            Covenant.name,
            Covenant.covenant_class,
            Facility.reference,
            Borrower.legal_name,
        )
        subtitle = _concat(Facility.reference, Borrower.legal_name)
        statement = select(
            *_result_columns(
                "covenant",
                Covenant.id,
                Covenant.reference,
                Covenant.name,
                subtitle,
                Covenant.covenant_class,
                Covenant.created_at,
                _rank(columns, tokens, query),
                "covenant fields",
                literal(False),
            )
        ).select_from(Covenant)
        statement = statement.join(Facility, Covenant.facility_id == Facility.id)
        statement = statement.join(Borrower, Facility.borrower_id == Borrower.id)
        statement = statement.join(Portfolio, Borrower.portfolio_id == Portfolio.id)
        return statement.where(scope.predicate(Portfolio.path), _all_tokens(columns, tokens))

    def _documents(
        self,
        tokens: tuple[str, ...],
        query: str,
        scope: Scope,
    ) -> Select[Any]:
        metadata_columns = (Document.filename, Document.doc_type, Borrower.legal_name)
        body_match = _all_tokens((DocumentPage.text,), tokens)
        body_exists = exists(
            select(DocumentPage.id).where(
                DocumentPage.document_id == Document.id,
                body_match,
            )
        )
        metadata_match = _all_tokens(metadata_columns, tokens)
        match = or_(metadata_match, body_exists)
        first_matching_page = (
            select(DocumentPage.text)
            .where(DocumentPage.document_id == Document.id, body_match)
            .order_by(DocumentPage.page_number, DocumentPage.id)
            .limit(1)
            .correlate(Document)
            .scalar_subquery()
        )
        score = _rank(metadata_columns, tokens, query) + case((body_exists, 30), else_=0)
        statement = (
            select(
                *_result_columns(
                    "document",
                    Document.id,
                    sql_cast(Document.id, String(36)),
                    Document.filename,
                    Borrower.legal_name,
                    func.coalesce(first_matching_page, Document.doc_type),
                    Document.created_at,
                    score,
                    "document metadata or body",
                    literal(False),
                )
            )
            .select_from(Document)
            .join(Borrower, Document.borrower_id == Borrower.id)
        )
        statement = statement.join(Portfolio, Borrower.portfolio_id == Portfolio.id)
        return statement.where(scope.predicate(Portfolio.path), match)

    def _memos(
        self,
        tokens: tuple[str, ...],
        query: str,
        scope: Scope,
    ) -> Select[Any]:
        columns = (Memo.drafted_text, Borrower.legal_name, Memo.template_version)
        statement = (
            select(
                *_result_columns(
                    "memo",
                    Memo.id,
                    sql_cast(Borrower.reference, Text()),
                    Borrower.legal_name,
                    Borrower.reference,
                    Memo.drafted_text,
                    Memo.created_at,
                    _rank(columns, tokens, query),
                    "memo text",
                    literal(False),
                )
            )
            .select_from(Memo)
            .join(Borrower, Memo.borrower_id == Borrower.id)
        )
        statement = statement.join(Portfolio, Borrower.portfolio_id == Portfolio.id)
        return statement.where(scope.predicate(Portfolio.path), _all_tokens(columns, tokens))

    def _cases(
        self,
        tokens: tuple[str, ...],
        query: str,
        scope: Scope,
    ) -> Select[Any]:
        columns = (
            Case.reference,
            Case.state,
            Case.closure_reason,
            Case.closure_note,
            Borrower.legal_name,
        )
        snippet = func.coalesce(Case.closure_note, Case.closure_reason)
        statement = (
            select(
                *_result_columns(
                    "case",
                    Case.id,
                    Case.reference,
                    Case.reference,
                    Borrower.legal_name,
                    snippet,
                    Case.created_at,
                    _rank(columns, tokens, query),
                    "case fields",
                    literal(False),
                )
            )
            .select_from(Case)
            .join(Borrower, Case.borrower_id == Borrower.id)
        )
        statement = statement.join(Portfolio, Borrower.portfolio_id == Portfolio.id)
        return statement.where(scope.predicate(Portfolio.path), _all_tokens(columns, tokens))

    def _audit_events(
        self,
        tokens: tuple[str, ...],
        query: str,
        scope: Scope,
        include_personal: bool,
    ) -> Select[Any]:
        public_columns = (
            AuditEvent.event_type,
            AuditEvent.subject_type,
            sql_cast(AuditEvent.payload, Text()),
        )
        public_match = _all_tokens(public_columns, tokens)
        personal_match = _all_tokens((AuditEvent.actor_label,), tokens)
        match = or_(public_match, personal_match) if include_personal else public_match
        score = _rank(public_columns, tokens, query)
        if include_personal:
            score = score + case((personal_match, 40), else_=0)
        statement = select(
            *_result_columns(
                "audit_event",
                AuditEvent.id,
                sql_cast(AuditEvent.subject_id, String(36)),
                AuditEvent.event_type,
                AuditEvent.subject_type,
                sql_cast(AuditEvent.payload, Text()),
                AuditEvent.occurred_at,
                score,
                "audit event",
                case((personal_match, True), else_=False) if include_personal else literal(False),
            )
        ).select_from(AuditEvent)
        return statement.where(self._audit_scope(scope), match)

    def _cin_match(
        self,
        query: str,
        tokens: Sequence[str],
        include_personal: bool,
    ) -> Any | None:
        if not include_personal or self.fingerprinter is None or not query:
            return None
        values = tuple(dict.fromkeys((query, *tokens)))
        fingerprints = tuple(
            fingerprint
            for value in values
            if (fingerprint := self.fingerprinter.fingerprint(value)) is not None
        )
        if not fingerprints:
            return None
        return or_(*(Borrower.cin_fingerprint == fingerprint for fingerprint in fingerprints))

    @staticmethod
    def _audit_scope(scope: Scope) -> Any:
        """Return an allow-list predicate for polymorphic audit subjects.

        Audit subjects intentionally have no foreign key, so an audit row is
        visible only when its typed subject can be joined to an in-scope
        portfolio.  Unknown subject types fail closed.
        """

        clauses: list[Any] = []
        subject_models = (
            ("portfolio", Portfolio),
            ("borrower", Borrower),
            ("facility", Facility),
            ("covenant", Covenant),
            ("covenant_version", CovenantVersion),
            ("covenant_test", CovenantTest),
            ("document", Document),
            ("evidence_item", EvidenceItem),
            ("signal_event", SignalEvent),
            ("case", Case),
            ("memo", Memo),
            ("certificate_request", CertificateRequest),
            ("covenant_schedule", CovenantSchedule),
            ("covenant_exception", CovenantException),
            ("covenant_waiver", CovenantWaiver),
            ("forecast", Forecast),
            ("forecast_driver", ForecastDriver),
            ("simulation", Simulation),
            ("triage_entry", TriageEntry),
        )
        for subject_type, model in subject_models:
            ownership = ownership_path_for(model)
            subject_statement = select(model.id).select_from(model)
            subject_statement = ownership.apply(subject_statement).where(
                model.id == AuditEvent.subject_id,
                scope.predicate(ownership.path_column),
            )
            clauses.append(and_(AuditEvent.subject_type == subject_type, exists(subject_statement)))

        # Forecast runs are aggregate records without a direct ownership FK.
        run_statement = (
            select(ForecastRun.id)
            .select_from(ForecastRun)
            .join(Forecast, Forecast.run_id == ForecastRun.id)
            .join(CovenantVersion, Forecast.covenant_version_id == CovenantVersion.id)
            .join(Covenant, CovenantVersion.covenant_id == Covenant.id)
            .join(Facility, Covenant.facility_id == Facility.id)
            .join(Borrower, Facility.borrower_id == Borrower.id)
            .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
            .where(
                ForecastRun.id == AuditEvent.subject_id,
                scope.predicate(Portfolio.path),
            )
        )
        clauses.append(and_(AuditEvent.subject_type == "forecast_run", exists(run_statement)))
        return or_(*clauses)


def normalize_query(value: str) -> tuple[tuple[str, ...], str]:
    """Validate, trim and tokenize user input for the SQL search boundary."""

    if not isinstance(value, str):
        raise TypeError("Search query must be text.")
    normalized = " ".join(value.split())
    if len(normalized) > MAX_SEARCH_QUERY_LENGTH:
        raise ValueError(f"Search query must be at most {MAX_SEARCH_QUERY_LENGTH} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("Search query contains a control character.")
    return tuple(normalized.casefold().split()), normalized


def normalize_entity_types(values: Iterable[str] | None) -> tuple[str, ...]:
    """Normalize type filters without allowing an unknown table selector."""

    if values is None:
        return SEARCH_ENTITY_TYPES
    if isinstance(values, str):
        values = (values,)
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("Search entity type filters must be text.")
        for candidate in value.split(","):
            normalized = candidate.strip().casefold()
            if not normalized:
                continue
            normalized = SEARCH_ENTITY_ALIASES.get(normalized, normalized)
            if normalized not in SEARCH_ENTITY_TYPES:
                raise ValueError(f"Unsupported search entity type {candidate.strip()!r}.")
            if normalized not in result:
                result.append(normalized)
    return tuple(result)


def _apply_ownership(statement: Select[Any], model: type[Any], scope: Scope) -> Select[Any]:
    ownership = ownership_path_for(model)
    return ownership.apply(statement).where(scope.predicate(ownership.path_column))


def _result_columns(
    entity_type: str,
    entity_id: Any,
    target_ref: Any,
    title: Any,
    subtitle: Any,
    snippet: Any,
    created_at: Any,
    score: Any,
    match_source: str,
    personal_match: Any,
) -> tuple[Any, ...]:
    return (
        literal(entity_type, type_=String()).label("entity_type"),
        sql_cast(entity_id, String(36)).label("entity_id"),
        sql_cast(target_ref, Text()).label("target_ref"),
        sql_cast(title, Text()).label("title"),
        sql_cast(subtitle, Text()).label("subtitle"),
        sql_cast(snippet, Text()).label("snippet"),
        created_at.label("created_at"),
        score.label("score"),
        literal(match_source, type_=String()).label("match_source"),
        personal_match.label("personal_match"),
    )


def _concat(*columns: Any) -> Any:
    expression: Any = func.coalesce(columns[0], literal(""))
    for column in columns[1:]:
        expression = expression + literal(" ") + func.coalesce(column, literal(""))
    return expression


def _all_tokens(columns: Sequence[Any], tokens: Sequence[str]) -> Any:
    if not tokens:
        return literal(True)
    return and_(*(_any_column(columns, token) for token in tokens))


def _any_column(columns: Sequence[Any], token: str) -> Any:
    pattern = f"%{_escape_like(token)}%"
    return or_(*(column.ilike(pattern, escape="\\") for column in columns))


def _rank(columns: Sequence[Any], tokens: Sequence[str], query: str) -> Any:
    if not tokens:
        return literal(0)
    score: Any = literal(0)
    exact_pattern = f"%{_escape_like(query)}%"
    for column in columns:
        score = score + case((column.ilike(exact_pattern, escape="\\"), 25), else_=0)
        for token in tokens:
            pattern = f"%{_escape_like(token)}%"
            score = score + case((column.ilike(pattern, escape="\\"), 5), else_=0)
    return score


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_row(row: Any) -> SearchRow:
    entity_id = row["entity_id"]
    try:
        parsed_id = entity_id if isinstance(entity_id, UUID) else UUID(str(entity_id))
    except (TypeError, ValueError) as error:
        raise RuntimeError("The search returned an invalid entity id.") from error
    target_ref = row["target_ref"]
    title = row["title"]
    created_at = row["created_at"]
    if not isinstance(target_ref, str) or not target_ref:
        raise RuntimeError("The search returned an invalid target reference.")
    if not isinstance(title, str) or not title:
        raise RuntimeError("The search returned an invalid result title.")
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise RuntimeError("The search returned an invalid result timestamp.")
    score = row["score"]
    if isinstance(score, bool) or not isinstance(score, int):
        score = int(score or 0)
    return SearchRow(
        entity_type=str(row["entity_type"]),
        entity_id=parsed_id,
        target_ref=target_ref,
        title=title,
        subtitle=cast(str | None, row["subtitle"]),
        snippet=cast(str | None, row["snippet"]),
        created_at=created_at,
        score=score,
        match_source=str(row["match_source"]),
        personal_match=bool(row["personal_match"]),
    )


def _page_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Search page_size must be an integer.")
    if not 1 <= value <= MAX_SEARCH_PAGE_SIZE:
        raise ValueError(f"Search page_size must be between 1 and {MAX_SEARCH_PAGE_SIZE}.")
    return value


def _offset(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Search offset must be an integer.")
    if not 0 <= value <= MAX_SEARCH_OFFSET:
        raise ValueError(f"Search offset must be between 0 and {MAX_SEARCH_OFFSET}.")
    return value


__all__ = [
    "MAX_SEARCH_OFFSET",
    "MAX_SEARCH_PAGE_SIZE",
    "MAX_SEARCH_QUERY_LENGTH",
    "SEARCH_ENTITY_ALIASES",
    "SEARCH_ENTITY_TYPES",
    "SearchQueryPage",
    "SearchRepository",
    "SearchRow",
    "normalize_entity_types",
    "normalize_query",
]
