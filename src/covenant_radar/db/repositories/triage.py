"""Scoped, run-consistent repository for the portfolio triage queue.

The queue deliberately has a different read shape from the write model.  A
single SQL statement selects the globally newest complete run and then joins
only rows whose borrower is inside the caller's materialised-path scope.  It
also projects the latest case state and assignee with scalar subqueries, so a
borrower can never be duplicated by historical case rows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import Select, and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from covenant_radar.core.errors import Conflict, ValidationError
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.forecast import ForecastRun, TriageEntry
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import SignalEvent
from covenant_radar.db.models.workflow import Case
from covenant_radar.db.scoping import Scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.triage.views import (
    RELOAD_REQUIRED_MESSAGE,
    QueueEntry,
    QueueFilters,
    QueuePage,
    QueueSummary,
)

COMPLETE_RUN_STATE: Final[str] = "complete"
TRIAGE_QUEUE_INDEX: Final[str] = "ix_triage_entry_run_id_rank"
DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_SIZE: Final[int] = 200
_CURSOR_VERSION: Final[int] = 1
_CURSOR_SECRET_ENV: Final[str] = "COVENANT_RADAR_QUEUE_CURSOR_SECRET"
_PROCESS_CURSOR_SECRET: Final[bytes] = secrets.token_bytes(32)
_CURSOR_MAX_LENGTH: Final[int] = 512


class InvalidQueueCursor(ValueError):
    """Raised when a cursor is malformed or fails authentication."""


@dataclass(frozen=True, slots=True)
class QueueCursor:
    """Authenticated seek position bound to one forecast run and filter set."""

    run_id: UUID
    rank: int
    borrower_id: UUID
    filters_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID) or not isinstance(self.borrower_id, UUID):
            raise TypeError("QueueCursor ids must be UUID values.")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("QueueCursor rank must be a positive integer.")
        if self.filters_digest is not None:
            if (
                not isinstance(self.filters_digest, str)
                or len(self.filters_digest) != 64
                or any(character not in "0123456789abcdef" for character in self.filters_digest)
            ):
                raise ValueError("QueueCursor filters_digest must be a lowercase SHA-256 digest.")

    def encode(self, secret: bytes | str | None = None) -> str:
        """Return an opaque, tamper-evident URL-safe token."""
        payload = {
            "v": _CURSOR_VERSION,
            "run_id": str(self.run_id),
            "rank": self.rank,
            "borrower_id": str(self.borrower_id),
        }
        if self.filters_digest is not None:
            payload["filters_digest"] = self.filters_digest
        body = _urlsafe(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        signature = hmac.new(_cursor_secret(secret), body, hashlib.sha256).digest()
        return f"{body.decode('ascii')}.{_urlsafe(signature).decode('ascii')}"

    @classmethod
    def decode(cls, token: str, secret: bytes | str | None = None) -> QueueCursor:
        """Verify and decode a cursor without trusting any client fields."""
        if not isinstance(token, str) or not 1 <= len(token) <= _CURSOR_MAX_LENGTH:
            raise InvalidQueueCursor("Queue cursor is malformed.")
        parts = token.split(".")
        if len(parts) != 2:
            raise InvalidQueueCursor("Queue cursor is malformed.")
        try:
            encoded_body = parts[0].encode("ascii")
            body = _urlsafe_decode(parts[0])
            supplied_signature = _urlsafe_decode(parts[1])
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise InvalidQueueCursor("Queue cursor is malformed.") from error
        expected_signature = hmac.new(_cursor_secret(secret), encoded_body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidQueueCursor("Queue cursor authentication failed.")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidQueueCursor("Queue cursor payload is malformed.") from error
        if not isinstance(payload, dict) or set(payload) - {
            "v",
            "run_id",
            "rank",
            "borrower_id",
            "filters_digest",
        }:
            raise InvalidQueueCursor("Queue cursor payload is malformed.")
        if payload.get("v") != _CURSOR_VERSION:
            raise InvalidQueueCursor("Queue cursor version is unsupported.")
        try:
            run_id = UUID(payload["run_id"])
            borrower_id = UUID(payload["borrower_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidQueueCursor("Queue cursor ids are malformed.") from error
        try:
            return cls(
                run_id=run_id,
                rank=payload.get("rank"),
                borrower_id=borrower_id,
                filters_digest=payload.get("filters_digest"),
            )
        except (TypeError, ValueError) as error:
            raise InvalidQueueCursor("Queue cursor fields are malformed.") from error

    from_token = decode


class TriageRepository:
    """Read the latest complete triage run with mandatory portfolio scope."""

    def __init__(self, session: Session, *, cursor_secret: bytes | str | None = None) -> None:
        if not is_database_session(session):
            raise TypeError("TriageRepository requires a SQLAlchemy Session.")
        self.session = session
        self._cursor_secret = _cursor_secret(cursor_secret)

    def query(
        self,
        scope: Scope,
        filters: QueueFilters | Mapping[str, object] | None = None,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        limit: int | None = None,
        cursor: str | QueueCursor | None = None,
        band: str | None = None,
        portfolio: UUID | str | None = None,
        portfolio_id: UUID | str | None = None,
        industry: str | None = None,
        industry_code: str | None = None,
        assignee: UUID | str | None = None,
        assignee_id: UUID | str | None = None,
        sma_band: str | None = None,
        case_state: str | None = None,
        case_status: str | None = None,
        signal_family: str | None = None,
    ) -> QueuePage:
        """Return one run-consistent, scoped queue page.

        ``limit`` is accepted as a transport-facing synonym for
        ``page_size``.  All filtering and pagination occurs in the SQL
        statement; no Python-side scope or filter pass is used.
        """
        if not isinstance(scope, Scope):
            raise TypeError("TriageRepository.query requires a Scope.")
        size = _page_size(page_size, limit)
        queue_filters = _filters_with_keywords(
            filters,
            band=band,
            portfolio=portfolio,
            portfolio_id=portfolio_id,
            industry=industry,
            industry_code=industry_code,
            assignee=assignee,
            assignee_id=assignee_id,
            sma_band=sma_band,
            case_state=case_state,
            case_status=case_status,
            signal_family=signal_family,
        )
        position = self._decode_cursor(cursor)
        expected_digest = _filters_digest(queue_filters)
        if position is not None and position.filters_digest not in (None, expected_digest):
            raise Conflict(RELOAD_REQUIRED_MESSAGE)

        statement = self.build_statement(
            scope,
            queue_filters,
            cursor=position,
            limit=size + 1,
        )
        rows = tuple(self.session.execute(statement).mappings().all())
        if not rows:
            if position is not None:
                raise Conflict(RELOAD_REQUIRED_MESSAGE)
            return QueuePage.no_complete_run()

        run_id = _required_uuid(rows[0]["queue_run_id"], "queue_run_id")
        if position is not None and position.run_id != run_id:
            raise Conflict(RELOAD_REQUIRED_MESSAGE)
        as_of_date = cast(date, rows[0]["queue_as_of_date"])
        if rows[0]["triage_entry_id"] is None:
            return QueuePage(run_id=run_id, as_of_date=as_of_date, entries=())

        has_more = len(rows) > size
        page_rows = rows[:size]
        entries = tuple(_queue_entry(row) for row in page_rows)
        next_cursor = None
        if has_more:
            last = entries[-1]
            next_cursor = QueueCursor(
                run_id=run_id,
                rank=last.rank,
                borrower_id=last.borrower_id,
                filters_digest=expected_digest,
            ).encode(self._cursor_secret)
        return QueuePage(
            run_id=run_id,
            as_of_date=as_of_date,
            entries=entries,
            next_cursor=next_cursor,
        )

    queue = query
    list_queue = query

    def build_statement(
        self,
        scope: Scope,
        filters: QueueFilters | Mapping[str, object] | None = None,
        *,
        cursor: QueueCursor | None = None,
        limit: int | None = DEFAULT_PAGE_SIZE + 1,
    ) -> Select[Any]:
        """Build the query for explain plans and read-only diagnostics."""
        if not isinstance(scope, Scope):
            raise TypeError("TriageRepository.build_statement requires a Scope.")
        if isinstance(cursor, str):
            raise TypeError("build_statement requires a decoded QueueCursor.")
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_PAGE_SIZE + 1
        ):
            raise ValueError(f"Queue query limit must be between 1 and {MAX_PAGE_SIZE + 1}.")
        queue_filters = QueueFilters.from_value(filters)
        latest = (
            select(
                ForecastRun.id.label("run_id"),
                ForecastRun.as_of_date.label("as_of_date"),
            )
            .where(
                ForecastRun.state == COMPLETE_RUN_STATE,
                # `spec §R-28.a`: a run that completed scoring but was never
                # ranked has no entries to serve.  Binding to it anyway would
                # blank the queue for the whole book; skipping to the newest
                # run that *was* ranked keeps the prior day's results serving,
                # which is what a halted pipeline is supposed to leave behind.
                exists(
                    select(1)
                    .select_from(TriageEntry)
                    .where(TriageEntry.run_id == ForecastRun.id)
                    .correlate(ForecastRun)
                ),
            )
            .order_by(
                ForecastRun.as_of_date.desc(),
                ForecastRun.finished_at.desc().nullslast(),
                ForecastRun.id.desc(),
            )
            .limit(1)
            .cte("latest_complete_run")
        )
        case_state = _latest_case_value(Case.state)
        assignee_id = _latest_case_value(Case.assignee_id)
        # The queue's row actions address a case by its stable human
        # reference, which is also what `BulkService` accepts for a
        # non-UUID selection, so one column serves the link and the
        # selection without putting a case UUID in the markup.
        case_reference = _latest_case_value(Case.reference)
        triage_join = [
            TriageEntry.run_id == latest.c.run_id,
            _scope_exists(scope),
        ]
        if queue_filters.band is not None:
            triage_join.append(TriageEntry.band == queue_filters.band)
        if queue_filters.sma_band is not None:
            triage_join.append(TriageEntry.sma_band == queue_filters.sma_band)
        if queue_filters.industry is not None:
            triage_join.append(
                exists(
                    select(1)
                    .select_from(Borrower)
                    .where(
                        Borrower.id == TriageEntry.borrower_id,
                        Borrower.industry_code == queue_filters.industry,
                    )
                    .correlate(TriageEntry)
                )
            )
        if queue_filters.portfolio is not None:
            triage_join.append(_portfolio_matches(queue_filters.portfolio))
        if queue_filters.case_state is not None:
            triage_join.append(
                case_state.is_(None)
                if queue_filters.case_state == "none"
                else case_state == queue_filters.case_state
            )
        if queue_filters.assignee is not None:
            triage_join.append(assignee_id == queue_filters.assignee)
        if queue_filters.signal_family is not None:
            triage_join.append(
                exists(
                    select(1)
                    .select_from(SignalEvent)
                    .where(
                        SignalEvent.borrower_id == TriageEntry.borrower_id,
                        SignalEvent.family == queue_filters.signal_family,
                    )
                    .correlate(TriageEntry)
                )
            )
        if cursor is not None:
            triage_join.append(
                or_(
                    TriageEntry.rank > cursor.rank,
                    and_(
                        TriageEntry.rank == cursor.rank,
                        TriageEntry.borrower_id > cursor.borrower_id,
                    ),
                )
            )

        statement: Select[Any] = (
            select(
                latest.c.run_id.label("queue_run_id"),
                latest.c.as_of_date.label("queue_as_of_date"),
                TriageEntry.id.label("triage_entry_id"),
                TriageEntry.run_id.label("triage_run_id"),
                TriageEntry.borrower_id.label("borrower_id"),
                TriageEntry.worst_covenant_version_id.label("worst_covenant_version_id"),
                TriageEntry.worst_horizon.label("worst_horizon"),
                TriageEntry.probability.label("probability"),
                TriageEntry.confidence.label("confidence"),
                TriageEntry.exposure.label("exposure"),
                TriageEntry.urgency.label("urgency"),
                TriageEntry.band.label("band"),
                TriageEntry.sma_band.label("sma_band"),
                TriageEntry.what_changed.label("what_changed"),
                TriageEntry.rank.label("rank"),
                Borrower.reference.label("borrower_reference"),
                Borrower.legal_name.label("legal_name"),
                Borrower.portfolio_id.label("portfolio_id"),
                Borrower.industry_code.label("industry_code"),
                Portfolio.code.label("portfolio_code"),
                Portfolio.path.label("portfolio_path"),
                case_state.label("case_state"),
                assignee_id.label("assignee_id"),
                case_reference.label("case_reference"),
            )
            .select_from(latest)
            .outerjoin(TriageEntry, and_(*triage_join))
            .outerjoin(Borrower, Borrower.id == TriageEntry.borrower_id)
            .outerjoin(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .order_by(TriageEntry.rank.asc(), TriageEntry.borrower_id.asc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        return statement

    def summary(
        self,
        scope: Scope,
        filters: QueueFilters | Mapping[str, object] | None = None,
    ) -> QueueSummary:
        """Return one aggregate over every scoped row in the latest run.

        The paginated queue and this snapshot share the same read statement;
        only the page limit is removed before aggregation. This keeps summary
        counts, filters and authorization semantics in lockstep.
        """

        if not isinstance(scope, Scope):
            raise TypeError("TriageRepository.summary requires a Scope.")
        queue_rows = (
            self.build_statement(scope, filters, limit=None)
            .order_by(None)
            .subquery("scoped_queue_summary")
        )
        meaningful_change = and_(
            queue_rows.c.triage_entry_id.is_not(None),
            queue_rows.c.what_changed.is_not(None),
            ~func.lower(queue_rows.c.what_changed).like("no change%"),
        )
        statement = select(
            func.count(queue_rows.c.triage_entry_id),
            func.coalesce(func.sum(case((queue_rows.c.band == "act", 1), else_=0)), 0),
            func.coalesce(func.sum(case((queue_rows.c.band == "amber", 1), else_=0)), 0),
            func.coalesce(func.sum(case((queue_rows.c.band == "watch", 1), else_=0)), 0),
            func.coalesce(func.sum(case((meaningful_change, 1), else_=0)), 0),
            func.sum(queue_rows.c.exposure),
        ).select_from(queue_rows)
        total, act, amber, watch, what_changed, exposure_total = self.session.execute(
            statement
        ).one()
        return QueueSummary(
            total=int(total or 0),
            act=int(act or 0),
            amber=int(amber or 0),
            watch=int(watch or 0),
            what_changed=int(what_changed or 0),
            exposure_total=cast(Decimal | None, exposure_total),
        )

    summarize = summary

    def explain(
        self,
        scope: Scope,
        filters: QueueFilters | Mapping[str, object] | None = None,
    ) -> tuple[str, ...]:
        """Return the database's read-only plan for the queue query."""
        dialect = self.session.get_bind().dialect
        compiled = self.build_statement(scope, filters).compile(
            dialect=dialect,
            compile_kwargs={"render_postcompile": True},
        )
        explain_sql = (
            f"EXPLAIN QUERY PLAN {compiled.string}"
            if dialect.name == "sqlite"
            else f"EXPLAIN {compiled.string}"
        )
        parameters: object = compiled.params
        if compiled.positional:
            parameters = tuple(compiled.params[name] for name in compiled.positiontup)
        rows = self.session.connection().exec_driver_sql(explain_sql, parameters).all()
        return tuple(" ".join(str(value) for value in row) for row in rows)

    def _decode_cursor(self, cursor: str | QueueCursor | None) -> QueueCursor | None:
        if cursor is None:
            return None
        if isinstance(cursor, QueueCursor):
            return cursor
        try:
            return QueueCursor.decode(cursor, self._cursor_secret)
        except (InvalidQueueCursor, TypeError, ValueError) as error:
            raise ValidationError(
                "Queue cursor is invalid; reload the queue.", field="cursor"
            ) from error


SqlAlchemyTriageRepository = TriageRepository


def _scope_exists(scope: Scope) -> Any:
    return (
        select(1)
        .select_from(Borrower)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            Borrower.id == TriageEntry.borrower_id,
            scope.predicate(Portfolio.path),
        )
        .correlate(TriageEntry)
        .exists()
    )


def _portfolio_matches(value: UUID | str) -> Any:
    if isinstance(value, UUID):
        return (
            select(1)
            .select_from(Borrower)
            .where(
                Borrower.id == TriageEntry.borrower_id,
                Borrower.portfolio_id == value,
            )
            .correlate(TriageEntry)
            .exists()
        )
    return (
        select(1)
        .select_from(Borrower)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(
            Borrower.id == TriageEntry.borrower_id,
            or_(Portfolio.code == value, Portfolio.path == value),
        )
        .correlate(TriageEntry)
        .exists()
    )


def _latest_case_value(column: Any) -> Any:
    return (
        select(column)
        .select_from(Case)
        .where(Case.borrower_id == TriageEntry.borrower_id)
        .order_by(Case.updated_at.desc(), Case.id.desc())
        .limit(1)
        .correlate_except(Case)
        .scalar_subquery()
    )


def _queue_entry(row: Mapping[str, object]) -> QueueEntry:
    return QueueEntry(
        triage_entry_id=_required_uuid(row["triage_entry_id"], "triage_entry_id"),
        run_id=_required_uuid(row["triage_run_id"], "triage_run_id"),
        borrower_id=_required_uuid(row["borrower_id"], "borrower_id"),
        borrower_reference=_required_text(row["borrower_reference"], "borrower_reference"),
        legal_name=_required_text(row["legal_name"], "legal_name"),
        portfolio_id=_required_uuid(row["portfolio_id"], "portfolio_id"),
        portfolio_code=_required_text(row["portfolio_code"], "portfolio_code"),
        portfolio_path=_required_text(row["portfolio_path"], "portfolio_path"),
        industry_code=cast(str | None, row["industry_code"]),
        worst_covenant_version_id=_optional_uuid(
            row["worst_covenant_version_id"], "worst_covenant_version_id"
        ),
        worst_horizon=cast(int | None, row["worst_horizon"]),
        probability=cast(Decimal | None, row["probability"]),
        confidence=cast(Decimal | None, row["confidence"]),
        exposure=cast(Decimal | None, row["exposure"]),
        urgency=cast(Decimal | None, row["urgency"]),
        band=cast(str | None, row["band"]) or "watch",
        sma_band=cast(str | None, row["sma_band"]),
        what_changed=cast(str | None, row["what_changed"]),
        rank=_required_int(row["rank"], "rank"),
        case_state=cast(str | None, row["case_state"]),
        assignee_id=_optional_uuid(row["assignee_id"], "assignee_id"),
        case_reference=cast(str | None, row["case_reference"]),
    )


def _filters_with_keywords(
    filters: QueueFilters | Mapping[str, object] | None,
    **keywords: object,
) -> QueueFilters:
    provided = {key: value for key, value in keywords.items() if value is not None}
    if filters is None:
        return QueueFilters.from_value(provided)
    base = QueueFilters.from_value(filters)
    if not provided:
        return base
    keyword_filters = QueueFilters.from_value(provided)
    values = {
        "band": base.band if base.band is not None else keyword_filters.band,
        "portfolio": base.portfolio if base.portfolio is not None else keyword_filters.portfolio,
        "industry": base.industry if base.industry is not None else keyword_filters.industry,
        "assignee": base.assignee if base.assignee is not None else keyword_filters.assignee,
        "sma_band": base.sma_band if base.sma_band is not None else keyword_filters.sma_band,
        "case_state": base.case_state
        if base.case_state is not None
        else keyword_filters.case_state,
        "signal_family": base.signal_family
        if base.signal_family is not None
        else keyword_filters.signal_family,
    }
    for name in values:
        left = getattr(base, name)
        right = getattr(keyword_filters, name)
        if left is not None and right is not None and left != right:
            raise ValueError(f"Filter {name!r} was provided more than once with different values.")
    return QueueFilters(**values)


def _filters_digest(filters: QueueFilters) -> str:
    return hashlib.sha256(
        json.dumps(filters.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _page_size(page_size: int, limit: int | None) -> int:
    if limit is not None:
        if page_size != DEFAULT_PAGE_SIZE and page_size != limit:
            raise ValueError("Provide either page_size or limit, not two different values.")
        page_size = limit
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise TypeError("Queue page_size must be an integer.")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"Queue page_size must be between 1 and {MAX_PAGE_SIZE}.")
    return page_size


def _cursor_secret(value: bytes | str | None) -> bytes:
    if value is None:
        configured = os.environ.get(_CURSOR_SECRET_ENV)
        return _cursor_secret(configured) if configured else _PROCESS_CURSOR_SECRET
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("Queue cursor secret must contain at least 32 bytes.")
    return value


def _urlsafe(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _urlsafe_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("empty base64 value")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(character not in alphabet for character in value):
        raise ValueError("invalid base64 value")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _required_uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"Queue result field {field_name!r} is missing or invalid.")
    return value


def _optional_uuid(value: object, field_name: str) -> UUID | None:
    if value is None:
        return None
    return _required_uuid(value, field_name)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Queue result field {field_name!r} is missing or invalid.")
    return value


def _required_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Queue result field {field_name!r} is missing or invalid.")
    return value


__all__ = [
    "COMPLETE_RUN_STATE",
    "DEFAULT_PAGE_SIZE",
    "InvalidQueueCursor",
    "MAX_PAGE_SIZE",
    "QueueCursor",
    "SqlAlchemyTriageRepository",
    "TRIAGE_QUEUE_INDEX",
    "TriageRepository",
]
