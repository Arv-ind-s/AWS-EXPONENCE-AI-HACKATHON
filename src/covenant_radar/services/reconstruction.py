"""Warning reconstruction service (`T-068`, `spec §R-20.a/d`, contract `C-15`).

``ReconstructionService.reconstruct`` is the one call `GET
/audit/warnings/{forecast_id}` (a later task's route) will delegate to: it
rebuilds every part of one warning — source data with provenance, the
covenant version and thresholds in force then, the calculation and trend
that produced it, the evidence in force, the forecast itself, its memo, and
any overrides or dispositions — from the forecast's own run, using
point-in-time reads so a later change can never alter a past reconstruction.

Every read stays inside the caller's ordinary portfolio scope:
reconstructing an old warning is a ``VIEW_AUDIT``-permitted read of the same
rows the rest of the product already scopes by portfolio, not a
cross-tenant escape hatch. This service therefore does not reach for the
``AUDITOR_CALLER`` unscoped-read path (`db/scoping.py`) — that stays
reserved for a feature that genuinely needs to cross portfolio boundaries.
``OverrideRecord`` and ``Disposition`` carry no portfolio foreign key of
their own (`db/models/workflow.py`); they are read by the already-resolved,
already-scope-checked forecast's own id, the same justification
`db/repositories/trace.py`'s ``TraceRepository`` gives for reading
``trace_row`` by subject without a second scope check.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.audit.bundle import (
    BundleDocument,
    BundlePdfRenderer,
    EvidenceBundle,
    EvidenceBundleError,
    build_bundle,
)
from covenant_radar.audit.chain import verify_chain
from covenant_radar.audit.events import AuditEventType
from covenant_radar.audit.reconstruct import (
    DispositionPart,
    DriverPart,
    EvidencePart,
    MemoPart,
    OverridePart,
    PurgedReference,
    SourceDocumentPart,
    WarningReconstruction,
    json_safe,
    supersession_note,
)
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, NotFound
from covenant_radar.db.models.audit import ThresholdSnapshot
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.document import Document, DocumentSpan
from covenant_radar.db.models.forecast import Forecast, ForecastPath, ForecastRun
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.operations import RetentionPurgeLog
from covenant_radar.db.models.workflow import Disposition, Memo, OverrideRecord
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.repositories.base import RepositoryBase
from covenant_radar.db.repositories.covenant import CovenantRepository, CovenantVersionRepository
from covenant_radar.db.repositories.driver import DriverRepository
from covenant_radar.db.repositories.evidence import EvidenceRepository, EvidenceTransitionRepository
from covenant_radar.db.repositories.facility import FacilityRepository
from covenant_radar.db.repositories.trace import TraceRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.ports.document_store import DocumentStore
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize

#: `domain/trace.py`'s `TraceStage.FORECAST` — the stage-4 row T-058 writes
#: once per forecast, and the "calculation" part of a reconstruction.
_CALCULATION_TRACE_STAGE = 4

#: `OverrideRecord.subject_type`/`Disposition.subject_type` for a warning.
_FORECAST_SUBJECT_TYPE = "forecast"

#: `RetentionPurgeLog.entity` naming a purged `Document` row.
_DOCUMENT_ENTITY = "document"
_LOGGER = logging.getLogger(__name__)

_DEFAULT_ASYNC_THRESHOLD_BYTES = 50 * 1024 * 1024
_BUNDLE_EVENT = AuditEventType.EVIDENCE_BUNDLE_EXPORTED.value
_BUNDLE_READY_NOTIFICATION = "evidence_bundle_ready"
_BUNDLE_FAILED_NOTIFICATION = "evidence_bundle_failed"
_DEFAULT_BUNDLE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="evidence-bundle")


class BundleStorage(Protocol):
    """The optional durable destination for completed bundle bytes."""

    def put(self, content: bytes) -> str:
        """Persist immutable bundle bytes and return a storage key."""
        ...


class BundleNotifier(Protocol):
    """Notification seam used after asynchronous bundle production."""

    def notify(self, event_type: str, payload: Mapping[str, object]) -> object:
        """Deliver one non-sensitive bundle status notification."""
        ...


class BundleAuditWriter(Protocol):
    """The narrow audit recording surface used by this service."""

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
class EvidenceBundleExportResult:
    """A completed or queued evidence-bundle export."""

    status: str
    bundle_id: UUID | str
    content: bytes | None
    storage_key: str | None
    manifest_hash: str | None
    filename: str
    audit_event: object | None = None
    future: Future[EvidenceBundleExportResult] | None = None
    notification_error: str | None = None

    @property
    def data(self) -> bytes | None:
        """Compatibility-facing name for the completed bundle bytes."""

        return self.content

    @property
    def bundle(self) -> bytes | None:
        """Compatibility-facing name for the completed bundle bytes."""

        return self.content

    @property
    def accepted(self) -> bool:
        """Whether production was accepted for asynchronous processing."""

        return self.status == "queued"

    @property
    def queued(self) -> bool:
        """Whether the bundle is waiting for asynchronous production."""

        return self.accepted

    @property
    def complete(self) -> bool:
        """Whether the bundle is available synchronously in this result."""

        return self.status == "complete"

    def result(self, timeout: float | None = None) -> EvidenceBundleExportResult:
        """Wait for an asynchronous result, or return this completed result."""

        if self.future is None:
            return self
        return self.future.result(timeout=timeout)


class ReconstructionService:
    """Assemble one warning's complete audit-trail view (`C-15`)."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Clock | None = None,
        document_store: DocumentStore | None = None,
        bundle_storage: BundleStorage | None = None,
        bundle_store: BundleStorage | None = None,
        notifier: BundleNotifier | Callable[[str, Mapping[str, object]], object] | None = None,
        executor: Executor | None = None,
        pdf_renderer: BundlePdfRenderer | None = None,
        audit: BundleAuditWriter | object | None = None,
        async_threshold_bytes: int = _DEFAULT_ASYNC_THRESHOLD_BYTES,
        large_bundle_threshold_bytes: int | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("ReconstructionService requires a SQLAlchemy Session.")
        if not isinstance(async_threshold_bytes, int) or isinstance(async_threshold_bytes, bool):
            raise TypeError("async_threshold_bytes must be an integer.")
        resolved_threshold = (
            large_bundle_threshold_bytes
            if large_bundle_threshold_bytes is not None
            else async_threshold_bytes
        )
        if (
            not isinstance(resolved_threshold, int)
            or isinstance(resolved_threshold, bool)
            or resolved_threshold < 1
        ):
            raise ValueError("async_threshold_bytes must be a positive integer.")
        if (
            bundle_storage is not None
            and bundle_store is not None
            and bundle_storage is not bundle_store
        ):
            raise ValueError("Specify only one of bundle_storage or bundle_store.")
        resolved_bundle_storage = bundle_storage or bundle_store
        if resolved_bundle_storage is not None and not callable(
            getattr(resolved_bundle_storage, "put", None)
        ):
            raise TypeError("bundle_storage must provide a callable put method.")
        if notifier is not None and not (
            callable(notifier)
            or callable(getattr(notifier, "notify", None))
            or callable(getattr(notifier, "send", None))
        ):
            raise TypeError("notifier must be callable or provide notify/send.")
        if executor is not None and not callable(getattr(executor, "submit", None)):
            raise TypeError("executor must provide a callable submit method.")
        self.session = session
        self.clock = clock or SystemClock()
        self.document_store = document_store
        self.bundle_storage = resolved_bundle_storage
        self.notifier = notifier
        self.executor = executor
        self.pdf_renderer = pdf_renderer
        self.async_threshold_bytes = resolved_threshold
        self.audit_writer, self.audit_store = _audit_dependencies(session, audit, self.clock)
        self._forecasts = RepositoryBase(session, Forecast, ownership=ownership_path_for(Forecast))
        self._documents = RepositoryBase(session, Document, ownership=ownership_path_for(Document))
        self._spans = RepositoryBase(
            session, DocumentSpan, ownership=ownership_path_for(DocumentSpan)
        )
        self.covenants = CovenantRepository(session)
        self.covenant_versions = CovenantVersionRepository(session)
        self.facilities = FacilityRepository(session)
        self.evidence = EvidenceRepository(session)
        self.evidence_transitions = EvidenceTransitionRepository(session)
        self.drivers = DriverRepository(session)
        self.traces = TraceRepository(session, clock=self.clock)

    def reconstruct(
        self,
        principal: Principal,
        forecast_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> WarningReconstruction:
        """Rebuild `forecast_id`'s warning end to end, as of its own run."""

        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.VIEW_AUDIT)
        if not isinstance(forecast_id, UUID):
            raise TypeError("forecast_id must be a UUID.")
        resolved_scope = self._validated_scope(principal, scope)

        forecast = self._forecasts.get(forecast_id, scope=resolved_scope)
        if forecast is None:
            raise NotFound(f"Forecast {forecast_id} was not found within the current scope.")

        run = self.session.get(ForecastRun, forecast.run_id)
        if run is None:
            raise NotFound(f"Forecast run {forecast.run_id} was not found.")

        covenant_version = self.covenant_versions.get(
            forecast.covenant_version_id, scope=resolved_scope
        )
        if covenant_version is None:
            raise NotFound(
                f"Covenant version {forecast.covenant_version_id} was not found "
                "within the current scope."
            )
        covenant = self.covenants.get(covenant_version.covenant_id, scope=resolved_scope)
        if covenant is None:
            raise NotFound(f"Covenant {covenant_version.covenant_id} was not found.")
        facility = self.facilities.get(covenant.facility_id, scope=resolved_scope)
        if facility is None:
            raise NotFound(f"Facility {covenant.facility_id} was not found.")
        borrower_id = facility.borrower_id

        threshold_snapshot = (
            self.session.get(ThresholdSnapshot, run.threshold_snapshot_id)
            if run.threshold_snapshot_id is not None
            else None
        )
        if threshold_snapshot is None:
            raise NotFound(f"Threshold snapshot for forecast run {run.id} was not found.")

        return WarningReconstruction(
            forecast_id=forecast.id,
            run_id=run.id,
            borrower_id=borrower_id,
            covenant_version_id=covenant_version.id,
            as_of_date=run.as_of_date,
            horizon_days=forecast.horizon_days,
            reconstructed_at=self._now(),
            source_data=self._source_data(covenant_version, scope=resolved_scope),
            formula_inputs=_candidate_inputs(forecast.formula_inputs),
            covenant_version=_covenant_version_snapshot(covenant, covenant_version),
            thresholds=_threshold_snapshot(threshold_snapshot),
            calculation=self._calculation(forecast.id),
            trend=self._trend(run.id, covenant_version.id, scope=resolved_scope),
            forecast=_forecast_snapshot(forecast),
            evidence=self._evidence_in_force(borrower_id, run.as_of_date, scope=resolved_scope),
            drivers=self._drivers(forecast.id, scope=resolved_scope),
            memo=self._memo(borrower_id, run.id, scope=resolved_scope),
            overrides=self._overrides(forecast.id),
            dispositions=self._dispositions(forecast.id),
        )

    def export_bundle(
        self,
        principal: Principal,
        forecast_id: UUID,
        *,
        scope: Scope | None = None,
        exported_at: datetime | None = None,
        bundle_id: UUID | str | None = None,
        async_threshold_bytes: int | None = None,
        large_bundle_threshold_bytes: int | None = None,
        request_id: str | None = None,
    ) -> EvidenceBundleExportResult:
        """Export one scoped warning as a verifiable evidence bundle.

        The method assembles all database facts before handing production to a
        worker.  The worker receives only immutable reconstruction data,
        document storage references and audit-row snapshots; it never uses
        this SQLAlchemy session from another thread.  Small bundles are
        returned immediately.  Large bundles are queued and notify the
        supplied notifier when their durable object is ready.
        """

        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.EXPORT_EVIDENCE)
        if not isinstance(forecast_id, UUID):
            raise TypeError("forecast_id must be a UUID.")
        resolved_scope = self._validated_scope(principal, scope)
        reconstruction = self.reconstruct(principal, forecast_id, scope=resolved_scope)
        documents = self._bundle_documents(reconstruction, scope=resolved_scope)
        audit_rows, chain_status = self._bundle_audit_segment(forecast_id)
        instant = self._now() if exported_at is None else _bundle_instant(exported_at)
        resolved_bundle_id = bundle_id or _new_bundle_id()
        _bundle_identifier(resolved_bundle_id)
        request = _bundle_request_id(request_id)
        threshold = _bundle_threshold(
            async_threshold_bytes,
            large_bundle_threshold_bytes,
            self.async_threshold_bytes,
        )
        estimated_bytes = _estimate_bundle_size(reconstruction, documents, threshold)
        if estimated_bytes > threshold:
            audit_event = self._record_bundle_export(
                principal,
                forecast_id,
                resolved_bundle_id,
                request_id=request,
                status="queued",
                chain_status=chain_status,
            )
            future = self._bundle_executor().submit(
                self._build_bundle_result,
                reconstruction,
                documents,
                audit_rows,
                chain_status,
                resolved_bundle_id,
                instant,
                request,
                audit_event,
                True,
            )
            return EvidenceBundleExportResult(
                status="queued",
                bundle_id=resolved_bundle_id,
                content=None,
                storage_key=None,
                manifest_hash=None,
                filename="evidence-bundle.zip",
                audit_event=audit_event,
                future=future,
            )

        result = self._build_bundle_result(
            reconstruction,
            documents,
            audit_rows,
            chain_status,
            resolved_bundle_id,
            instant,
            request,
            None,
        )
        try:
            audit_event = self._record_bundle_export(
                principal,
                forecast_id,
                resolved_bundle_id,
                request_id=request,
                status="complete",
                chain_status=chain_status,
                manifest_hash=result.manifest_hash,
            )
        except Exception:
            self._remove_persisted_bundle(result.storage_key)
            raise
        return EvidenceBundleExportResult(
            status=result.status,
            bundle_id=result.bundle_id,
            content=result.content,
            storage_key=result.storage_key,
            manifest_hash=result.manifest_hash,
            filename=result.filename,
            audit_event=audit_event,
            future=None,
            notification_error=result.notification_error,
        )

    export_evidence_bundle = export_bundle
    export_bundle_for_warning = export_bundle

    def _build_bundle_result(
        self,
        reconstruction: WarningReconstruction,
        documents: Sequence[BundleDocument],
        audit_rows: Sequence[object],
        chain_status: Mapping[str, object],
        bundle_id: UUID | str,
        generated_at: datetime,
        request_id: str,
        audit_event: object | None,
        notify_on_complete: bool = False,
    ) -> EvidenceBundleExportResult:
        try:
            bundle = build_bundle(
                reconstruction,
                documents=documents,
                document_store=self.document_store,
                audit_rows=audit_rows,
                chain_verification=chain_status,
                bundle_id=bundle_id,
                generated_at=generated_at,
                pdf_renderer=self.pdf_renderer,
            )
            storage_key = self._persist_bundle(bundle)
            notification_error: str | None = None
            if notify_on_complete:
                notification_error = self._notify_bundle(
                    _BUNDLE_READY_NOTIFICATION,
                    {
                        "bundle_id": str(bundle.bundle_id),
                        "forecast_id": str(reconstruction.forecast_id),
                        "status": "complete",
                        "manifest_hash": bundle.manifest_hash,
                        "storage_key": storage_key,
                        "request_id": request_id,
                    },
                )
            return EvidenceBundleExportResult(
                status="complete",
                bundle_id=bundle.bundle_id,
                content=bundle.content,
                storage_key=storage_key,
                manifest_hash=bundle.manifest_hash,
                filename=bundle.filename,
                audit_event=audit_event,
                notification_error=notification_error,
            )
        except Exception as error:
            if notify_on_complete:
                self._notify_bundle(
                    _BUNDLE_FAILED_NOTIFICATION,
                    {
                        "bundle_id": str(bundle_id),
                        "forecast_id": str(reconstruction.forecast_id),
                        "status": "failed",
                        "reason": f"{type(error).__name__}: {error}"[:500],
                        "request_id": request_id,
                    },
                )
            raise

    def _persist_bundle(self, bundle: EvidenceBundle) -> str | None:
        if self.bundle_storage is None:
            return None
        storage_key = self.bundle_storage.put(bundle.content)
        if not isinstance(storage_key, str) or not storage_key.strip():
            raise EvidenceBundleError("Bundle storage returned an invalid storage key.")
        cleaned = storage_key.strip()
        if len(cleaned) > 500 or any(
            ord(character) < 32 or ord(character) == 127 for character in cleaned
        ):
            raise EvidenceBundleError("Bundle storage returned an unsafe storage key.")
        return cleaned

    def _remove_persisted_bundle(self, storage_key: str | None) -> None:
        if storage_key is None:
            return
        remove = getattr(self.bundle_storage, "delete", None)
        if not callable(remove):
            return
        try:
            remove(storage_key)
        except Exception as error:  # noqa: BLE001 - retain the audit failure as the root cause
            _LOGGER.warning("Unable to remove orphaned evidence bundle %s: %s", storage_key, error)

    def _bundle_executor(self) -> Executor:
        return self.executor or _DEFAULT_BUNDLE_EXECUTOR

    def _notify_bundle(self, event_type: str, payload: Mapping[str, object]) -> str | None:
        if self.notifier is None:
            return None
        try:
            if callable(self.notifier):
                self.notifier(event_type, payload)
            else:
                notify = getattr(self.notifier, "notify", None) or getattr(
                    self.notifier, "send", None
                )
                if not callable(notify):  # pragma: no cover - constructor validates this
                    raise TypeError("notifier does not expose a callable notify/send method")
                notify(event_type, payload)
        except Exception as error:  # noqa: BLE001 - the bundle remains available for retry
            message = f"{type(error).__name__}: {error}"[:500]
            _LOGGER.warning("Evidence bundle notification failed: %s", message)
            return message
        return None

    def _record_bundle_export(
        self,
        principal: Principal,
        forecast_id: UUID,
        bundle_id: UUID | str,
        *,
        request_id: str,
        status: str,
        chain_status: Mapping[str, object],
        manifest_hash: str | None = None,
    ) -> object:
        actor: object
        if principal.kind is PrincipalKind.API_KEY:
            actor = f"api-key:{principal.id}"
        elif self.session.get(AppUser, principal.id) is None:
            # Direct service callers and offline workers may use a principal
            # not backed by the local user table.  Keep the actor explicit in
            # the audit label without violating the FK on actor_id.
            actor = f"user:{principal.id}"
        else:
            actor = principal.id
        payload: dict[str, object] = {
            "forecast_id": str(forecast_id),
            "bundle_id": str(bundle_id),
            "status": status,
            "chain_verified": chain_status.get("verified") is True,
            "requested_by": str(principal.id),
        }
        if manifest_hash is not None:
            payload["manifest_hash"] = manifest_hash
        return self.audit_writer.record(
            _BUNDLE_EVENT,
            ("forecast", forecast_id),
            payload,
            actor=actor,
            request_id=request_id,
        )

    def _bundle_documents(
        self, reconstruction: WarningReconstruction, *, scope: Scope
    ) -> tuple[BundleDocument, ...]:
        source = reconstruction.source_data
        if source.id is None:
            return ()
        document = self._documents.get(source.id, scope=scope)
        if document is None:
            reason = "Referenced document metadata is not present in storage."
            if source.purged is not None:
                reason = (
                    f"Document was purged under {source.purged.rule} on "
                    f"{source.purged.purged_at.isoformat()}."
                )
            return (
                BundleDocument(
                    document_id=source.id,
                    filename=source.filename or f"document-{source.id}.bin",
                    storage_key=None,
                    content_hash=source.content_hash,
                    status="purged" if source.status.value == "purged" else "absent",
                    reason=reason,
                ),
            )
        return (
            BundleDocument(
                document_id=document.id,
                filename=document.filename,
                storage_key=document.storage_key,
                content_hash=document.content_hash,
                byte_size=document.byte_size,
            ),
        )

    def _bundle_audit_segment(
        self, forecast_id: UUID
    ) -> tuple[tuple[object, ...], Mapping[str, object]]:
        rows_method = getattr(self.audit_store, "rows", None)
        if not callable(rows_method):
            return (), {
                "verified": True,
                "status": "verified",
                "failure": None,
                "message": "No audit rows available.",
                "range": {"from": None, "to": None},
            }
        all_rows = tuple(rows_method())
        relevant = tuple(row for row in all_rows if getattr(row, "subject_id", None) == forecast_id)
        if not relevant:
            return (), {
                "verified": True,
                "status": "verified",
                "failure": None,
                "message": "No audit rows matched the warning.",
                "range": {"from": None, "to": None},
            }
        first = min(_audit_sequence(row) for row in relevant)
        last = max(_audit_sequence(row) for row in relevant)
        predecessor = max(
            (row for row in all_rows if _audit_sequence(row) < first),
            key=_audit_sequence,
            default=None,
        )
        segment = tuple(row for row in all_rows if first <= _audit_sequence(row) <= last)
        rows = ((predecessor,) if predecessor is not None else ()) + segment
        report = verify_chain(
            tuple(rows),
            from_sequence=first,
            to_sequence=last,
        )
        if report is None:
            status: Mapping[str, object] = {
                "verified": True,
                "status": "verified",
                "failure": None,
                "message": "Audit chain segment verified.",
                "range": {"from": first, "to": last},
            }
        else:
            status = {
                "verified": False,
                "status": "failed",
                "failure": json_safe(
                    {
                        "sequence": report.sequence,
                        "previous_sequence": report.previous_sequence,
                        "reason": report.reason,
                        "expected_prev_hash": report.expected_prev_hash,
                        "actual_prev_hash": report.actual_prev_hash,
                        "expected_hash": report.expected_hash,
                        "actual_hash": report.actual_hash,
                        "message": report.message,
                    }
                ),
                "message": report.message,
                "range": {"from": first, "to": last},
            }
        return rows, status

    # ---- part assembly ---------------------------------------------------

    def _source_data(
        self, covenant_version: CovenantVersion, *, scope: Scope
    ) -> SourceDocumentPart:
        document_id = covenant_version.source_document_id
        if document_id is None:
            return SourceDocumentPart.absent()
        document = self._documents.get(document_id, scope=scope)
        if document is None:
            purge = self._purge_reference(_DOCUMENT_ENTITY, document_id)
            if purge is not None:
                return SourceDocumentPart.mark_purged(document_id, purge)
            return SourceDocumentPart.absent(document_id=document_id)
        span_id: UUID | None = None
        span_text: str | None = None
        if covenant_version.source_span_id is not None:
            span = self._spans.get(covenant_version.source_span_id, scope=scope)
            if span is not None:
                span_id, span_text = span.id, span.text
        return SourceDocumentPart.present(
            id=document.id,
            filename=document.filename,
            doc_type=document.doc_type,
            content_hash=document.content_hash,
            retention_class=document.retention_class,
            span_id=span_id,
            span_text=span_text,
        )

    def _purge_reference(self, entity: str, entity_id: UUID) -> PurgedReference | None:
        rows = (
            self.session.execute(
                select(RetentionPurgeLog)
                .where(RetentionPurgeLog.entity == entity)
                .order_by(RetentionPurgeLog.executed_at.desc(), RetentionPurgeLog.id.desc())
            )
            .scalars()
            .all()
        )
        target = str(entity_id)
        for row in rows:
            criteria = row.criteria or {}
            matched = criteria.get("entity_id") or criteria.get("document_id") or criteria.get("id")
            if matched is not None and str(matched) == target:
                rule = str(criteria.get("rule") or f"{entity} retention purge")
                return PurgedReference(
                    entity=entity,
                    entity_id=entity_id,
                    rule=rule,
                    purged_at=row.executed_at,
                    purged_count=row.purged_count,
                )
        return None

    def _evidence_in_force(
        self, borrower_id: UUID, as_of_date: date, *, scope: Scope
    ) -> tuple[EvidencePart, ...]:
        as_of_items = self.evidence.for_borrower_as_of(borrower_id, as_of_date, scope=scope)
        if not as_of_items:
            return ()
        current_rows = self.evidence.for_borrower(borrower_id, scope=scope, include_superseded=True)
        current_by_id = {row.id: row for row in current_rows}
        transition_rows = self.evidence_transitions.for_borrower(borrower_id, scope=scope)
        transitions_by_item: dict[UUID, list[tuple[date, str, str]]] = {}
        for row in transition_rows:
            transitions_by_item.setdefault(row.evidence_id, []).append(
                (row.occurred_on, row.to_state, row.rule)
            )

        parts: list[EvidencePart] = []
        for item in as_of_items:
            assert item.id is not None
            current = current_by_id.get(item.id)
            note = None
            if current is not None:
                note = supersession_note(
                    current_state=current.state,
                    current_superseded_by_id=current.superseded_by_id,
                    transitions=tuple(transitions_by_item.get(item.id, ())),
                    as_of=as_of_date,
                )
            parts.append(
                EvidencePart(
                    id=item.id,
                    family=item.family,
                    evidence_type=item.evidence_type,
                    first_seen=item.first_seen,
                    last_seen=item.last_seen,
                    state=item.state,
                    materiality_pct=item.materiality_pct,
                    decay_factor=item.decay_factor,
                    counts_toward_pressure=item.counts_toward_pressure,
                    superseded_by_id=item.superseded_by_id,
                    supersedes_id=item.supersedes_id,
                    superseded_since=note,
                )
            )
        return tuple(parts)

    def _drivers(self, forecast_id: UUID, *, scope: Scope) -> tuple[DriverPart, ...]:
        rows = self.drivers.for_forecast(forecast_id, scope=scope)
        return tuple(
            DriverPart(
                name=row.name,
                share=row.share,
                evidence_id=row.evidence_id,
                is_other=row.is_other,
            )
            for row in rows
        )

    def _calculation(self, forecast_id: UUID) -> Mapping[str, object] | None:
        rows = self.traces.history(("forecast", forecast_id), stage=_CALCULATION_TRACE_STAGE)
        if not rows:
            return None
        row = rows[-1]
        return {
            "stage": int(row.stage),
            "decider": row.decider,
            "inputs": dict(row.inputs),
            "outputs": dict(row.outputs),
            "rule_or_prompt_version": row.rule_or_prompt_version,
            "thresholds_compared": [dict(entry) for entry in row.thresholds_compared],
            "confidence": row.confidence,
            "sources": list(row.sources or ()),
            "occurred_at": row.occurred_at,
        }

    def _trend(
        self, run_id: UUID, covenant_version_id: UUID, *, scope: Scope
    ) -> tuple[Mapping[str, object], ...]:
        ownership = ownership_path_for(ForecastPath)
        statement: Select[Any] = ownership.apply(select(ForecastPath)).where(
            scope.predicate(ownership.path_column),
            ForecastPath.run_id == run_id,
            ForecastPath.covenant_version_id == covenant_version_id,
        )
        statement = statement.order_by(ForecastPath.day_offset)
        rows = self.session.execute(statement).scalars().all()
        return tuple(
            {
                "day_offset": row.day_offset,
                "projected_value": row.projected_value,
                "headroom_pct": row.headroom_pct,
            }
            for row in rows
        )

    def _memo(self, borrower_id: UUID, run_id: UUID, *, scope: Scope) -> MemoPart:
        ownership = ownership_path_for(Memo)
        statement: Select[Any] = ownership.apply(select(Memo)).where(
            scope.predicate(ownership.path_column),
            Memo.borrower_id == borrower_id,
            Memo.run_id == run_id,
        )
        statement = statement.order_by(Memo.created_at.desc(), Memo.id.desc()).limit(1)
        memo = self.session.execute(statement).scalars().first()
        if memo is None:
            return MemoPart.not_generated()
        return MemoPart.present(
            id=memo.id,
            template_version=memo.template_version,
            prompt_version=memo.prompt_version,
            drafted_text=memo.drafted_text,
            check_verdict=memo.check_verdict,
            generated_by_id=memo.generated_by_id,
            generated_at=memo.created_at,
        )

    def _overrides(self, forecast_id: UUID) -> tuple[OverridePart, ...]:
        rows = (
            self.session.execute(
                select(OverrideRecord)
                .where(
                    OverrideRecord.subject_type == _FORECAST_SUBJECT_TYPE,
                    OverrideRecord.subject_id == forecast_id,
                )
                .order_by(OverrideRecord.created_at, OverrideRecord.id)
            )
            .scalars()
            .all()
        )
        return tuple(
            OverridePart(
                id=row.id,
                stage=row.stage,
                user_action=row.user_action,
                reason=row.reason,
                actor_id=row.actor_id,
                recorded_at=row.created_at,
            )
            for row in rows
        )

    def _dispositions(self, forecast_id: UUID) -> tuple[DispositionPart, ...]:
        rows = (
            self.session.execute(
                select(Disposition)
                .where(
                    Disposition.subject_type == _FORECAST_SUBJECT_TYPE,
                    Disposition.subject_id == forecast_id,
                )
                .order_by(Disposition.created_at, Disposition.id)
            )
            .scalars()
            .all()
        )
        return tuple(
            DispositionPart(
                id=row.id,
                outcome=row.outcome,
                reason_code=row.reason_code,
                note=row.note,
                actor_id=row.actor_id,
                recorded_at=row.created_at,
            )
            for row in rows
        )

    # ---- internal invariants ---------------------------------------------

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        resolved = scope if scope is not None else resolve_scope(principal, self.session)
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The resolved scope does not belong to the authenticated principal."
            )
        return resolved

    def _now(self) -> datetime:
        value = self.clock.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Reconstruction clock must return a timezone-aware datetime.")
        return value


def _covenant_version_snapshot(
    covenant: Covenant, version: CovenantVersion
) -> Mapping[str, object]:
    return {
        "id": version.id,
        "covenant_id": version.covenant_id,
        "covenant_reference": covenant.reference,
        "covenant_name": covenant.name,
        "covenant_class": covenant.covenant_class,
        "version_no": version.version_no,
        "threshold": version.threshold,
        "direction": version.direction,
        "unit": version.unit,
        "frequency": version.frequency,
        "test_basis": version.test_basis,
        "effective_from": version.effective_from,
        "effective_to": version.effective_to,
        "status": version.status,
        "warning_headroom_pct": version.warning_headroom_pct,
        "cure_days": version.cure_days,
        "grace_days": version.grace_days,
    }


def _threshold_snapshot(snapshot: ThresholdSnapshot) -> Mapping[str, object]:
    return {
        "id": snapshot.id,
        "values": dict(snapshot.values),
        "source": snapshot.source,
        "effective_from": snapshot.effective_from,
        "note": snapshot.note,
    }


def _forecast_snapshot(forecast: Forecast) -> Mapping[str, object]:
    return {
        "id": forecast.id,
        "run_id": forecast.run_id,
        "covenant_version_id": forecast.covenant_version_id,
        "horizon_days": forecast.horizon_days,
        "probability": forecast.probability,
        "confidence": forecast.confidence,
        "below_confidence_floor": forecast.below_confidence_floor,
        "projected_cross_date": forecast.projected_cross_date,
        "direction": forecast.direction,
        "data_as_of": forecast.data_as_of,
        "staleness_days": forecast.staleness_days,
    }


def _candidate_inputs(formula_inputs: Mapping[str, object] | None) -> Mapping[str, object]:
    if not formula_inputs:
        return {}
    candidate = formula_inputs.get("candidate_inputs")
    if not isinstance(candidate, Mapping):
        return {}
    return dict(candidate)


def _audit_dependencies(
    session: Session,
    audit: BundleAuditWriter | object | None,
    clock: Clock,
) -> tuple[BundleAuditWriter, object]:
    default_store = AuditRepository(session)
    if audit is None:
        return cast(BundleAuditWriter, AuditRecorder(default_store, clock=clock)), default_store
    if callable(getattr(audit, "record", None)):
        writer = audit
    elif callable(getattr(audit, "append", None)):
        writer = AuditRecorder(audit, clock=clock)  # type: ignore[arg-type]
    else:
        raise TypeError("audit must provide record or append.")
    store = getattr(writer, "store", None) or audit
    if not callable(getattr(store, "rows", None)):
        store = default_store
    return writer, store  # type: ignore[return-value]


def _audit_sequence(row: object) -> int:
    value = getattr(row, "sequence", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Audit rows must carry a positive integer sequence.")
    return value


def _bundle_documents_size(documents: Sequence[BundleDocument]) -> int:
    total = 0
    for document in documents:
        if document.status == "present":
            if document.byte_size is None:
                return -1
            total += document.byte_size
    return total


def _estimate_bundle_size(
    reconstruction: WarningReconstruction,
    documents: Sequence[BundleDocument],
    threshold: int,
) -> int:
    document_size = _bundle_documents_size(documents)
    if document_size < 0:
        # Unknown-size streams are conservatively asynchronous.  The worker
        # still enforces the backend's integrity and chunk contracts.
        return threshold + 1
    reconstruction_size = len(str(json_safe(reconstruction.as_dict())).encode("utf-8"))
    return document_size + reconstruction_size + 2 * 1024 * 1024


def _bundle_threshold(
    value: int | None,
    alias: int | None,
    default: int,
) -> int:
    if value is not None and alias is not None and value != alias:
        raise ValueError(
            "Specify only one of async_threshold_bytes or large_bundle_threshold_bytes."
        )
    resolved = value if value is not None else alias if alias is not None else default
    if not isinstance(resolved, int) or isinstance(resolved, bool) or resolved < 1:
        raise ValueError("Bundle asynchronous threshold must be a positive integer.")
    return resolved


def _bundle_identifier(value: UUID | str) -> None:
    if not isinstance(value, UUID | str) or not str(value).strip():
        raise TypeError("bundle_id must be a non-empty UUID or string.")


def _new_bundle_id() -> UUID:
    return uuid4()


def _bundle_request_id(value: str | None) -> str:
    resolved = value or get_request_id() or new_request_id()
    if not isinstance(resolved, str) or not resolved.strip() or len(resolved.strip()) > 40:
        raise ValueError("Bundle request_id must be between 1 and 40 characters.")
    return resolved.strip()


def _bundle_instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Bundle timestamp must be timezone-aware.")
    return value


__all__ = [
    "BundleExportResult",
    "BundleAuditWriter",
    "BundleNotifier",
    "BundleStorage",
    "EvidenceBundleResult",
    "EvidenceBundleExportResult",
    "ReconstructionService",
]


EvidenceBundleResult = EvidenceBundleExportResult
BundleExportResult = EvidenceBundleExportResult
