"""REST ingestion routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from covenant_radar.api.deps import requires
from covenant_radar.api.v1.schemas.ingest import (
    SignalBatchRequest,
    SignalIngestionResponse,
    StatementDiscrepancySummary,
    StatementImportResponse,
    StatementQuarantineSummary,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.ingestion import SignalIngestionService
from covenant_radar.services.statements import StatementImportService

_INGEST = requires(Permission.INGEST_DATA)
_INGEST_DEP = Depends(_INGEST)

#: A statement extract's bytes are parsed and discarded here, never stored,
#: so this endpoint does not route through `security/uploads.py`'s
#: `UploadGuard` — that gate exists for documents that get persisted and
#: later served back, with a mandatory virus scan before that persistence.
#: This is only a defensive size cap, matching that module's own default.
_MAX_STATEMENT_UPLOAD_BYTES = 10 * 1024 * 1024
_STATEMENT_SOURCE_TYPES = frozenset({"csv", "xlsx", "json"})

# Built once at import time, the same way `_INGEST_DEP` is above: ruff (B008)
# flags a FastAPI parameter marker constructed inline in a default value.
_FILE_DEP = File(...)
_SOURCE_TYPE_DEP = Form(...)
_MAPPING_NAME_DEP = Form(...)
_MAPPING_VERSION_DEP = Form(None)


def create_ingest_router(
    service: SignalIngestionService,
    *,
    statements: StatementImportService | None = None,
    prefix: str = "/api/v1",
) -> APIRouter:
    """Build the ingestion API around injected services.

    `statements` is optional so existing callers that only need signal
    ingestion (`create_ingest_router(signal_service)`) are unaffected; when
    supplied, `POST /ingest/statements` (`C-22`) is also registered.
    """

    if not isinstance(service, SignalIngestionService):
        raise TypeError("create_ingest_router requires a SignalIngestionService.")
    if statements is not None and not isinstance(statements, StatementImportService):
        raise TypeError("create_ingest_router requires a StatementImportService or None.")
    router = APIRouter(prefix=prefix, tags=["ingestion"])

    @router.post(
        "/ingest/signals",
        response_model=SignalIngestionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        name="api_signal_ingest",
    )
    async def ingest_signals(
        payload: SignalBatchRequest,
        request: Request,
        principal: Principal = _INGEST_DEP,
    ) -> SignalIngestionResponse:
        # Header/body support keeps this compatible with standard idempotent
        # API clients.  The service's event content hashes remain the
        # definitive de-duplication key for redelivery in this ingestion slice.
        header_idempotency_key = request.headers.get("idempotency-key")
        if (
            header_idempotency_key is not None
            and payload.idempotency_key is not None
            and header_idempotency_key != payload.idempotency_key
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Header and body idempotency keys must match.",
            )
        if header_idempotency_key is not None:
            if not 1 <= len(header_idempotency_key) <= 200 or not header_idempotency_key.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="The Idempotency-Key header must be between 1 and 200 characters.",
                )
            if any(
                ord(character) < 32 or ord(character) == 127 for character in header_idempotency_key
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="The Idempotency-Key header contains an invalid control character.",
                )
        report = service.ingest(
            principal,
            [event.model_dump(mode="python") for event in payload.events],
            source_id=payload.source_id,
            request_id=getattr(request.state, "request_id", None),
            idempotency_key=header_idempotency_key or payload.idempotency_key,
        )
        return SignalIngestionResponse(
            batch_id=report.batch_id,
            received=report.received,
            inserted=report.inserted,
            duplicates=report.duplicates,
            rejected=report.rejected,
            quarantined=report.quarantined_count,
            accepted=report.accepted,
            reconciled=report.reconciled,
            source_ids=list(report.source_ids),
        )

    if statements is not None:

        @router.post(
            "/ingest/statements",
            response_model=StatementImportResponse,
            status_code=status.HTTP_202_ACCEPTED,
            name="api_statement_ingest",
        )
        async def ingest_statements(
            request: Request,
            file: UploadFile = _FILE_DEP,
            source_type: str = _SOURCE_TYPE_DEP,
            mapping_name: str = _MAPPING_NAME_DEP,
            mapping_version: int | None = _MAPPING_VERSION_DEP,
            principal: Principal = _INGEST_DEP,
        ) -> StatementImportResponse:
            if source_type not in _STATEMENT_SOURCE_TYPES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"source_type must be one of {sorted(_STATEMENT_SOURCE_TYPES)}.",
                )
            content = await file.read(_MAX_STATEMENT_UPLOAD_BYTES + 1)
            if len(content) > _MAX_STATEMENT_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Statement file exceeds {_MAX_STATEMENT_UPLOAD_BYTES} bytes.",
                )
            report = statements.import_statements(
                principal,
                source_type=source_type,
                content=content,
                mapping_name=mapping_name,
                mapping_version=mapping_version,
                source_reference=file.filename,
                request_id=getattr(request.state, "request_id", None),
            )
            return StatementImportResponse(
                batch_id=report.batch_id,
                mapping_name=report.mapping_name,
                mapping_version=report.mapping_version,
                source_type=report.source_type,
                content_hash=report.content_hash,
                received=report.received,
                accepted=report.accepted,
                quarantined=report.quarantined,
                totals_rows=report.totals_rows,
                quarantine=[
                    StatementQuarantineSummary(
                        row_number=item.row_number,
                        rule_failed=item.rule_failed,
                        message=item.message,
                    )
                    for item in report.quarantine
                ],
                discrepancies=[
                    StatementDiscrepancySummary(
                        line_code=item.line_code,
                        expected=item.expected,
                        actual=item.actual,
                        difference=item.difference,
                    )
                    for item in report.discrepancies
                ],
                reconciled=report.reconciled,
            )

    return router


create_signal_ingest_router = create_ingest_router

__all__ = ["create_ingest_router", "create_signal_ingest_router"]
