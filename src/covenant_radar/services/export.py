"""Safe list export and job-backed asynchronous downloads.

The service owns the export format, bounded rendering and download lifetime.
Callers provide a row source that has already been scoped; the built-in case
source in :meth:`ExportService.export_cases` applies the portfolio predicate
in SQL before a row can reach the renderer.  Large exports are registered as
ordinary scheduler jobs, so retries, restart handling and terminal state all
use the same durable ledger as the nightly pipeline.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import inspect
import io
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any, Final, Protocol, cast
from uuid import UUID

from openpyxl import Workbook
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import (
    AuthorizationError,
    Conflict,
    ExternalServiceError,
    NotFound,
    ValidationError,
)
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Case
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import SessionFactory, is_database_session
from covenant_radar.scheduler.jobs import InterruptionPolicy, JobDefinition, JobPolicy, RetryPolicy
from covenant_radar.scheduler.ledger import SUCCEEDED
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_MAX_COLUMNS: Final[int] = 100
_MAX_COLUMN_LENGTH: Final[int] = 100
_MAX_FILTERS: Final[int] = 30
_MAX_FILTER_VALUE_LENGTH: Final[int] = 500
_MAX_RENDERED_BYTES: Final[int] = 128 * 1024 * 1024
_CASE_REFERENCE_MAX_LENGTH: Final[int] = 20
_DEFAULT_ASYNC_THRESHOLD: Final[int] = 10_000
_DEFAULT_LINK_TTL: Final[timedelta] = timedelta(hours=24)
_EXPORT_JOB_PREFIX: Final[str] = "exports.bulk."


class ExportStore(Protocol):
    """Encrypted, durable storage for completed export bytes."""

    def put(self, content: bytes, *, content_hash: str | None = None) -> str:
        """Store bytes atomically and return an opaque storage key."""
        ...

    def get(self, storage_key: str) -> bytes:
        """Return the complete, integrity-checked object."""
        ...


class ExportJobRunner(Protocol):
    """The scheduler subset required by the export queue."""

    registry: Any

    def submit(self, job_name: str, *, trigger: str, **kwargs: object) -> object:
        """Submit a registered job without blocking the request."""
        ...


class ExportAuditWriter(Protocol):
    """The append-only audit boundary required by exports."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's transaction."""
        ...


@dataclass(frozen=True, slots=True)
class ExportNotification:
    """The disclosure-safe completion notice passed to the notification port."""

    recipient_id: UUID
    export_id: UUID
    download_url: str
    row_count: int
    format: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ExportDownload:
    """A completed export suitable for an HTTP file response."""

    export_id: UUID
    content: bytes
    format: str
    content_hash: str
    filename: str


@dataclass(frozen=True, slots=True)
class ExportResult:
    """The state returned after requesting or querying an export."""

    export_id: UUID
    state: str
    format: str
    row_count: int | None
    filter: Mapping[str, object]
    download_url: str | None = None
    expires_at: datetime | None = None
    content: bytes | None = None
    content_hash: str | None = None
    run_id: str | None = None
    error: str | None = None

    @property
    def id(self) -> UUID:
        return self.export_id

    @property
    def status(self) -> str:
        return self.state

    @property
    def is_async(self) -> bool:
        return self.run_id is not None

    @property
    def queued(self) -> bool:
        return self.state in {"queued", "running"}

    def as_dict(self) -> dict[str, object]:
        return {
            "export_id": str(self.export_id),
            "state": self.state,
            "format": self.format,
            "row_count": self.row_count,
            "filter": dict(self.filter),
            "download_url": self.download_url,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "content_hash": self.content_hash,
            "run_id": self.run_id,
            "error": self.error,
        }


Rows = Iterable[Mapping[str, object]]
RowsFactory = Callable[[], Rows]


class ExportService:
    """Render scoped rows synchronously or queue a durable export job."""

    def __init__(
        self,
        session_or_store: Session | ExportStore | None = None,
        *,
        store: ExportStore | None = None,
        runner: ExportJobRunner | None = None,
        notification_sink: object | None = None,
        audit: ExportAuditWriter | None = None,
        session_factory: SessionFactory | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
        async_threshold: int = _DEFAULT_ASYNC_THRESHOLD,
        link_ttl: timedelta = _DEFAULT_LINK_TTL,
        download_path: str = "/exports/{export_id}/download",
    ) -> None:
        if is_database_session(session_or_store):
            session = cast(Session, session_or_store)
            resolved_store = store
        elif session_or_store is not None:
            if store is not None:
                raise TypeError("Provide the export store once, positionally or by keyword.")
            session = None
            resolved_store = cast(ExportStore, session_or_store)
        else:
            session = None
            resolved_store = store
        if resolved_store is None or not callable(getattr(resolved_store, "put", None)):
            raise TypeError("ExportService requires a durable export store.")
        if runner is not None and not callable(getattr(runner, "submit", None)):
            raise TypeError("ExportService runner must expose submit().")
        if audit is not None and not callable(getattr(audit, "record", None)):
            raise TypeError("ExportService audit must expose record().")
        if session_factory is not None and not callable(session_factory):
            raise TypeError("ExportService session_factory must be callable.")
        if (
            not isinstance(async_threshold, int)
            or isinstance(async_threshold, bool)
            or async_threshold < 0
        ):
            raise ValueError("async_threshold must be a non-negative integer.")
        if link_ttl <= timedelta(0):
            raise ValueError("link_ttl must be positive.")
        if not isinstance(download_path, str) or "{export_id}" not in download_path:
            raise ValueError("download_path must contain the {export_id} placeholder.")

        self.session = session
        self.store = resolved_store
        self.runner = runner
        self.notification_sink = notification_sink
        self.audit = audit
        self.session_factory = session_factory or cast(
            SessionFactory | None, getattr(runner, "session_factory", None)
        )
        self.scope_resolver = scope_resolver or (
            (lambda principal: resolve_scope(principal, session)) if session is not None else None
        )
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())
        self.async_threshold = async_threshold
        self.link_ttl = link_ttl
        self.download_path = download_path

    def export_rows(
        self,
        principal: Principal,
        rows: Rows | RowsFactory,
        *,
        columns: Sequence[str],
        format: str = "csv",
        filters: Mapping[str, object] | None = None,
        row_count: int | None = None,
        force_async: bool | None = None,
    ) -> ExportResult:
        """Request an export from a caller-supplied, already-scoped row source."""

        self._require_export_authority(principal)
        normalized_format = _format(format)
        normalized_columns = _columns(columns)
        normalized_filters = _filters(filters)
        source, known_count = self._source(rows)
        resolved_count = row_count if row_count is not None else known_count
        if resolved_count is not None and (isinstance(resolved_count, bool) or resolved_count < 0):
            raise ValidationError("row_count must be a non-negative integer.", field="row_count")
        should_queue = (
            force_async
            if force_async is not None
            else resolved_count is not None and resolved_count > self.async_threshold
        )
        if should_queue:
            if self.runner is None:
                raise ExternalServiceError(
                    "Large exports are unavailable because no job runner is configured."
                )
            if resolved_count is None:
                source_rows = tuple(source())

                def materialized_source() -> Rows:
                    return iter(source_rows)

                source = materialized_source
                resolved_count = len(source_rows)
            return self._queue(
                principal,
                source,
                columns=normalized_columns,
                format=normalized_format,
                filters=normalized_filters,
                row_count=resolved_count,
            )

        export_id = new_id()
        artifact = _render(source(), normalized_columns, normalized_format)
        self._audit(
            principal,
            artifact.row_count,
            normalized_format,
            normalized_filters,
            export_id=export_id,
            asynchronous=False,
            outcome="ready",
        )
        return ExportResult(
            export_id=export_id,
            state="ready",
            format=normalized_format,
            row_count=artifact.row_count,
            filter=normalized_filters,
            content=artifact.content,
            content_hash=artifact.content_hash,
        )

    def export_cases(
        self,
        principal: Principal,
        *,
        case_ids: Sequence[UUID | str] = (),
        filters: Mapping[str, object] | None = None,
        format: str = "csv",
        force_async: bool | None = None,
        scope: Scope | None = None,
    ) -> ExportResult:
        """Export the scoped case list, with filtering performed in SQL."""

        self._require_export_authority(principal)
        if self.session is None or self.scope_resolver is None:
            raise ExternalServiceError(
                "Case exports require a database session and scope resolver."
            )
        resolved_scope = self._scope(principal, scope)
        normalized_filters = _case_filters(filters)
        normalized_ids = _case_ids(case_ids)
        count_statement = self._case_statement(
            resolved_scope, normalized_ids, normalized_filters, count=True
        )
        row_count = int(self.session.scalar(count_statement) or 0)

        def source() -> Rows:
            source_session = self.session
            owns_session = False
            if self.session_factory is not None:
                source_session = self.session_factory()
                owns_session = True
            try:
                statement = self._case_statement(
                    resolved_scope, normalized_ids, normalized_filters, count=False
                )
                for row in source_session.execute(statement).all():
                    yield _case_row(row)
            finally:
                if owns_session:
                    source_session.close()

        # Without a session factory, handing a request-bound session to a
        # worker would be unsafe.  Materialising is bounded by the explicit
        # async threshold and is preferable to crossing thread boundaries.
        selected_source: Rows | RowsFactory = source
        if row_count > self.async_threshold and self.session_factory is None:
            selected_source = tuple(source())
        return self.export_rows(
            principal,
            selected_source,
            columns=_CASE_COLUMNS,
            format=format,
            filters=normalized_filters,
            row_count=row_count,
            force_async=force_async,
        )

    def status(self, principal: Principal, export_id: UUID | str) -> ExportResult:
        """Read one export's state without exposing another user's job."""

        self._require_export_authority(principal)
        row = self._job(principal, export_id)
        metrics = _metrics(row.metrics, allow_empty=row.state != SUCCEEDED)
        resolved_id = _export_id(export_id)
        state = row.state if row.state != SUCCEEDED else "ready"
        expires_at = _optional_datetime(metrics.get("expires_at"))
        if expires_at is not None and self._now() >= expires_at and state == "ready":
            state = "expired"
        return ExportResult(
            export_id=resolved_id,
            state=state,
            format=_format(str(metrics.get("format", "csv"))),
            row_count=_optional_nonnegative_int(metrics.get("row_count")),
            filter=_filters(cast(Mapping[str, object] | None, metrics.get("filter"))),
            download_url=(
                self.download_path.format(export_id=resolved_id) if state == "ready" else None
            ),
            expires_at=expires_at,
            content_hash=_optional_text(metrics.get("content_hash")),
            run_id=row.run_id,
            error=_optional_text(metrics.get("error")) or row.error,
        )

    get_status = status

    def download(self, principal: Principal, export_id: UUID | str) -> ExportDownload:
        """Return a completed export only while its scoped link is valid."""

        self._require_export_authority(principal)
        row = self._job(principal, export_id)
        if row.state != SUCCEEDED:
            raise Conflict("The export is not ready for download.")
        metrics = _metrics(row.metrics)
        expires_at = _required_datetime(metrics.get("expires_at"))
        if self._now() >= expires_at:
            raise NotFound("The export download link has expired.")
        storage_key = _required_text(metrics.get("storage_key"), "storage_key")
        content = self.store.get(storage_key)
        if not isinstance(content, bytes):
            raise ExternalServiceError("The export store returned a non-binary object.")
        expected_hash = _required_text(metrics.get("content_hash"), "content_hash")
        actual_hash = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise ExternalServiceError("The stored export failed its integrity check.")
        resolved_id = _export_id(export_id)
        normalized_format = _format(str(metrics.get("format", "csv")))
        return ExportDownload(
            export_id=resolved_id,
            content=content,
            format=normalized_format,
            content_hash=actual_hash,
            filename=f"covenant-radar-export-{resolved_id}.{normalized_format}",
        )

    def _queue(
        self,
        principal: Principal,
        source: RowsFactory,
        *,
        columns: tuple[str, ...],
        format: str,
        filters: dict[str, object],
        row_count: int,
    ) -> ExportResult:
        runner = self.runner
        if runner is None:
            raise ExternalServiceError(
                "Bulk export is unavailable because no job runner is configured."
            )
        if self.audit is None:
            raise ExternalServiceError(
                "Bulk export is unavailable because audit recording is not configured."
            )
        if self.notification_sink is None:
            raise ExternalServiceError(
                "Bulk export is unavailable because completion notification is not configured."
            )
        export_id = new_id()
        job_name = f"{_EXPORT_JOB_PREFIX}{export_id.hex}"
        run_id = str(export_id)
        expires_at = self._now() + self.link_ttl

        def handler(_context: object) -> Mapping[str, object]:
            artifact = _render(source(), columns, format)
            if artifact.row_count != row_count:
                # The filter is a snapshot, but an upstream source can still
                # change between queueing and execution.  Reporting the real
                # count is safer than presenting a false total.
                actual_count = artifact.row_count
            else:
                actual_count = row_count
            storage_key = self.store.put(artifact.content, content_hash=artifact.content_hash)
            if not isinstance(storage_key, str) or not storage_key.strip():
                raise ExternalServiceError("The export store returned no storage key.")
            notification = ExportNotification(
                recipient_id=principal.id,
                export_id=export_id,
                download_url=self.download_path.format(export_id=export_id),
                row_count=actual_count,
                format=format,
                expires_at=expires_at,
            )
            _invoke_notification(self.notification_sink, notification)
            return {
                "export_id": str(export_id),
                "owner_id": str(principal.id),
                "format": format,
                "row_count": actual_count,
                "filter": dict(filters),
                "storage_key": storage_key,
                "content_hash": artifact.content_hash,
                "expires_at": expires_at.isoformat(),
            }

        definition = JobDefinition(
            name=job_name,
            handler=handler,
            policy=JobPolicy(
                retry=RetryPolicy(max_attempts=3, backoff_seconds=1.0),
                interruption=InterruptionPolicy.RESUME,
                timeout_seconds=900.0,
            ),
        )
        registry = getattr(runner, "registry", None)
        register = getattr(registry, "register", None)
        if not callable(register):
            raise ExternalServiceError(
                "Bulk export is unavailable because job registration is not configured."
            )
        register(definition)
        self._audit(
            principal,
            row_count,
            format,
            filters,
            export_id=export_id,
            asynchronous=True,
            outcome="queued",
        )
        try:
            runner.submit(
                job_name,
                trigger="bulk-export",
                run_id=run_id,
                actor_id=principal.id,
            )
        except Exception:
            # The request transaction is still allowed to roll back the
            # queue audit, so a failed submission cannot look queued later.
            raise ExternalServiceError("The bulk export could not be queued.") from None
        return ExportResult(
            export_id=export_id,
            state="queued",
            format=format,
            row_count=row_count,
            filter=filters,
            download_url=self.download_path.format(export_id=export_id),
            expires_at=expires_at,
            run_id=run_id,
        )

    def _job(self, principal: Principal, export_id: UUID | str) -> JobRun:
        if self.session is None:
            raise ExternalServiceError("Export status requires a database session.")
        resolved_id = _export_id(export_id)
        row = self.session.scalar(
            select(JobRun).where(
                JobRun.run_id == str(resolved_id), JobRun.created_by_id == principal.id
            )
        )
        if not isinstance(row, JobRun):
            raise NotFound("Export was not found.")
        return row

    def _case_statement(
        self,
        scope: Scope,
        case_ids: tuple[UUID | str, ...],
        filters: Mapping[str, object],
        *,
        count: bool,
    ) -> Select[Any]:
        if count:
            statement = (
                select(func.count(Case.id))
                .select_from(Case)
                .join(Borrower, Borrower.id == Case.borrower_id)
                .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
                .where(scope.predicate(Portfolio.path))
            )
        else:
            statement = (
                select(
                    Case.id,
                    Case.reference,
                    Borrower.reference,
                    Borrower.legal_name,
                    Portfolio.code,
                    Case.state,
                    Case.assignee_id,
                    AppUser.full_name,
                    Case.due_at,
                    Case.updated_at,
                )
                .select_from(Case)
                .join(Borrower, Borrower.id == Case.borrower_id)
                .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
                .outerjoin(AppUser, AppUser.id == Case.assignee_id)
                .where(scope.predicate(Portfolio.path))
                .order_by(Case.updated_at.desc(), Case.id.desc())
            )
        if case_ids:
            ids = tuple(item for item in case_ids if isinstance(item, UUID))
            references = tuple(item for item in case_ids if isinstance(item, str))
            selectors = []
            if ids:
                selectors.append(Case.id.in_(ids))
            if references:
                selectors.append(Case.reference.in_(references))
            statement = statement.where(or_(*selectors))
        if filters.get("state") is not None:
            statement = statement.where(Case.state == filters["state"])
        if filters.get("assignee_id") is not None:
            statement = statement.where(Case.assignee_id == filters["assignee_id"])
        if filters.get("borrower_id") is not None:
            statement = statement.where(Case.borrower_id == filters["borrower_id"])
        if filters.get("portfolio_id") is not None:
            statement = statement.where(Portfolio.id == filters["portfolio_id"])
        return statement

    def _source(self, rows: Rows | RowsFactory) -> tuple[RowsFactory, int | None]:
        if callable(rows):
            return cast(RowsFactory, rows), None
        if not isinstance(rows, Iterable) or isinstance(rows, str | bytes | bytearray):
            raise TypeError(
                "Export rows must be an iterable of mappings or a zero-argument factory."
            )
        if isinstance(rows, Sequence):
            snapshot = tuple(rows)
            return lambda: iter(snapshot), len(snapshot)
        return lambda: cast(Rows, rows), None

    def _require_export_authority(self, principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        if not principal.has(Permission.EXPORT_EVIDENCE):
            raise AuthorizationError(f"Missing permission: {Permission.EXPORT_EVIDENCE.value}.")

    def _scope(self, principal: Principal, scope: Scope | None) -> Scope:
        resolved = (
            self.scope_resolver(principal) if scope is None and self.scope_resolver else scope
        )
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The resolved scope does not belong to the authenticated principal."
            )
        return resolved

    def _audit(
        self,
        principal: Principal,
        row_count: int,
        format: str,
        filters: Mapping[str, object],
        *,
        export_id: UUID | None,
        asynchronous: bool,
        outcome: str,
    ) -> None:
        if self.audit is None:
            raise ExternalServiceError("Export audit recording is not configured.")
        subject_id = export_id or new_id()
        self.audit.record(
            AuditEventType.EVIDENCE_BUNDLE_EXPORTED.value,
            ("export", subject_id),
            {
                "export_kind": "bulk_list",
                "export_id": str(export_id) if export_id else None,
                "format": format,
                "filter": dict(filters),
                "row_count": row_count,
                "asynchronous": asynchronous,
                "outcome": outcome,
            },
            actor=principal.id,
            request_id=self.request_id,
        )

    def _now(self) -> datetime:
        value = self.clock.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ExportService clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _Rendered:
    content: bytes
    content_hash: str
    row_count: int


_CASE_COLUMNS: Final[tuple[str, ...]] = (
    "case_id",
    "case_reference",
    "borrower_reference",
    "borrower_name",
    "portfolio",
    "state",
    "assignee_id",
    "assignee",
    "due_at",
    "updated_at",
)


def _render(rows: Rows, columns: tuple[str, ...], format: str) -> _Rendered:
    if format == "csv":
        content, count = _render_csv(rows, columns)
    else:
        content, count = _render_xlsx(rows, columns)
    if len(content) > _MAX_RENDERED_BYTES:
        raise ValidationError(
            f"The rendered export exceeds {_MAX_RENDERED_BYTES} bytes.", field="export"
        )
    return _Rendered(content, hashlib.sha256(content).hexdigest(), count)


def _render_csv(rows: Rows, columns: tuple[str, ...]) -> tuple[bytes, int]:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(columns)
    count = 0
    for row in rows:
        _validate_row(row, columns)
        writer.writerow([_cell(row.get(column)) for column in columns])
        count += 1
    return output.getvalue().encode("utf-8-sig"), count


def _render_xlsx(rows: Rows, columns: tuple[str, ...]) -> tuple[bytes, int]:
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Export")
    worksheet.append(list(columns))
    count = 0
    for row in rows:
        _validate_row(row, columns)
        worksheet.append([_cell(row.get(column)) for column in columns])
        count += 1
    with SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as output:
        workbook.save(output)
        output.seek(0)
        return output.read(), count


def _validate_row(row: Mapping[str, object], columns: tuple[str, ...]) -> None:
    if not isinstance(row, Mapping):
        raise ValidationError("Every export row must be an object.", field="rows")
    unknown = set(row).difference(columns)
    if unknown:
        raise ValidationError(
            "Export row contains unknown columns: "
            f"{', '.join(sorted(str(value) for value in unknown))}.",
            field="rows",
        )


def _case_row(row: tuple[object, ...]) -> dict[str, object]:
    (
        case_id,
        case_reference,
        borrower_reference,
        borrower_name,
        portfolio_code,
        state,
        assignee_id,
        assignee_name,
        due_at,
        updated_at,
    ) = row
    return {
        "case_id": case_id,
        "case_reference": case_reference,
        "borrower_reference": borrower_reference,
        "borrower_name": borrower_name,
        "portfolio": portfolio_code,
        "state": state,
        "assignee_id": assignee_id,
        "assignee": assignee_name,
        "due_at": due_at,
        "updated_at": updated_at,
    }


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        text = value.astimezone(UTC).isoformat()
    elif isinstance(value, UUID | Path):
        text = str(value)
    else:
        text = str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return f"'{text}"
    return text


def _format(value: object) -> str:
    if not isinstance(value, str) or value.strip().lower() not in {"csv", "xlsx"}:
        raise ValidationError("format must be csv or xlsx.", field="format")
    return value.strip().lower()


def _columns(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not value:
        raise ValidationError("columns must be a non-empty sequence.", field="columns")
    if len(value) > _MAX_COLUMNS:
        raise ValidationError(
            f"columns may contain at most {_MAX_COLUMNS} entries.", field="columns"
        )
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not 1 <= len(item.strip()) <= _MAX_COLUMN_LENGTH:
            raise ValidationError("Every column name must be bounded text.", field="columns")
        normalized = item.strip()
        if normalized in seen:
            raise ValidationError(f"Duplicate export column {normalized!r}.", field="columns")
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _filters(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > _MAX_FILTERS:
        raise ValidationError(
            f"filters may contain at most {_MAX_FILTERS} fields.", field="filters"
        )
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("filter names must be non-empty text.", field="filters")
        if isinstance(item, str) and len(item) > _MAX_FILTER_VALUE_LENGTH:
            raise ValidationError(f"filter {key!r} is too long.", field="filters")
        if isinstance(item, float) and not isfinite(item):
            raise ValidationError(f"filter {key!r} must be finite.", field="filters")
        if not isinstance(item, str | int | float | bool | UUID | None):
            raise ValidationError(f"filter {key!r} has an unsupported value.", field="filters")
        result[key.strip()] = str(item) if isinstance(item, UUID) else item
    return result


def _case_filters(value: Mapping[str, object] | None) -> dict[str, object]:
    result = _filters(value)
    allowed = {"state", "assignee_id", "borrower_id", "portfolio_id"}
    unknown = set(result).difference(allowed)
    if unknown:
        raise ValidationError(
            f"Unknown case filter(s): {', '.join(sorted(unknown))}.", field="filters"
        )
    if result.get("state") is not None:
        state = result["state"]
        if not isinstance(state, str) or state not in {
            "open",
            "in_progress",
            "monitoring",
            "escalated",
            "closed",
        }:
            raise ValidationError("filters.state is not a valid case state.", field="filters.state")
    for field in ("assignee_id", "borrower_id", "portfolio_id"):
        if field in result and result[field] is not None:
            result[field] = _uuid(result[field], f"filters.{field}")
    return result


def _case_ids(value: Sequence[UUID | str]) -> tuple[UUID | str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError("case_ids must be a sequence.", field="case_ids")
    result: list[UUID | str] = []
    seen: set[UUID | str] = set()
    for item in value:
        if isinstance(item, UUID):
            resolved: UUID | str = item
        elif isinstance(item, str) and item.strip():
            text = item.strip()
            try:
                resolved = UUID(text)
            except ValueError:
                if len(text) > _CASE_REFERENCE_MAX_LENGTH:
                    raise ValidationError(
                        f"case_ids references may contain at most {_CASE_REFERENCE_MAX_LENGTH} "
                        "characters.",
                        field="case_ids",
                    ) from None
                resolved = text
        else:
            raise ValidationError("case_ids must contain UUIDs or references.", field="case_ids")
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return tuple(result)


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value.strip())
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} must be a UUID.", field=field)


def _export_id(value: UUID | str) -> UUID:
    return _uuid(value, "export_id")


def _metrics(value: object, *, allow_empty: bool = False) -> dict[str, object]:
    if value is None and allow_empty:
        return {}
    if not isinstance(value, Mapping):
        raise ExternalServiceError("The export job has no valid result metadata.")
    return dict(value)


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(value: object, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ExternalServiceError(f"The export job has no {field}.")
    return text


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExternalServiceError("The export job has invalid row-count metadata.")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExternalServiceError("The export job has invalid expiry metadata.")
    return _required_datetime(value)


def _required_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ExternalServiceError("The export job has invalid expiry metadata.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ExternalServiceError("The export job has invalid expiry metadata.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExternalServiceError("The export job expiry must be timezone-aware.")
    return parsed.astimezone(UTC)


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(
            f"Export request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
        )
    return value.strip()


def _invoke_notification(sink: object, notification: ExportNotification) -> None:
    method = getattr(sink, "notify", sink)
    if not callable(method):
        raise TypeError("notification_sink must expose notify() or be callable.")
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        method(notification)
        return
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        method(
            recipient_id=notification.recipient_id,
            export_id=notification.export_id,
            download_url=notification.download_url,
            row_count=notification.row_count,
            format=notification.format,
            expires_at=notification.expires_at,
        )
        return
    if len(parameters) == 1:
        method(notification)
        return
    names = set(parameters)
    expected = {
        "recipient_id": notification.recipient_id,
        "export_id": notification.export_id,
        "download_url": notification.download_url,
        "row_count": notification.row_count,
        "format": notification.format,
        "expires_at": notification.expires_at,
    }
    if names.issubset(expected):
        method(**{name: expected[name] for name in names})
        return
    raise TypeError("notification_sink.notify has an unsupported signature.")


__all__ = [
    "ExportDownload",
    "ExportJobRunner",
    "ExportNotification",
    "ExportResult",
    "ExportService",
    "ExportStore",
]
