"""Integration coverage for T-069 evidence bundles and verification."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from pypdf import PdfReader
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.audit.bundle import BundleDocument, build_bundle, verify_bundle
from covenant_radar.audit.reconstruct import (
    MemoPart,
    SourceDocumentPart,
    WarningReconstruction,
)
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.audit.store import InMemoryAuditStore
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import NotFound
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.reconstruction import ReconstructionService
from tests.integration.test_reconstruction import _NOW, _build_world

_NOW_LOCAL = datetime(2026, 8, 31, 9, 30, tzinfo=UTC)


class _DocumentStore:
    def __init__(self, content: bytes, *, missing: bool = False) -> None:
        self.content = content
        self.missing = missing

    def stream(self, _storage_key: str) -> Iterable[bytes]:
        if self.missing:
            raise NotFound("document was removed")
        midpoint = len(self.content) // 2 or len(self.content)
        return iter((self.content[:midpoint], self.content[midpoint:]))


class _Notifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    def notify(self, event_type: str, payload: Mapping[str, object]) -> object:
        self.messages.append((event_type, dict(payload)))
        return None


class _AuditRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append(
            (event_type, subject, {**payload, "actor": actor, "request_id": request_id})
        )
        return self.events[-1]


def _reconstruction(*, source_document_id: UUID | None = None) -> WarningReconstruction:
    source = SourceDocumentPart.absent()
    if source_document_id is not None:
        source = SourceDocumentPart.present(
            id=source_document_id,
            filename="sanction-letter.pdf",
            doc_type="loan_agreement",
            content_hash=hashlib.sha256(b"sanction bytes").hexdigest(),
            retention_class="statutory_7y",
        )
    return WarningReconstruction(
        forecast_id=uuid4(),
        run_id=uuid4(),
        borrower_id=uuid4(),
        covenant_version_id=uuid4(),
        as_of_date=date(2026, 8, 30),
        horizon_days=90,
        reconstructed_at=_NOW_LOCAL,
        source_data=source,
        formula_inputs={"observed": "1.25"},
        covenant_version={"threshold": "1.10", "direction": "min"},
        thresholds={"T2": {"confidence_floor": "0.50"}},
        calculation={"stage": 4, "decider": "code"},
        trend=({"day_offset": 0, "headroom_pct": "8.0"},),
        forecast={"probability": "0.42", "confidence": "0.88"},
        evidence=(),
        drivers=(),
        memo=MemoPart.not_generated(),
        overrides=(),
        dispositions=(),
    )


def _archive_with_replaced_file(content: bytes, path: str, replacement: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            target.writestr(
                info, replacement if info.filename == path else source.read(info.filename)
            )
    return output.getvalue()


def test_manifest_hash_matches_contents() -> None:
    document_id = uuid4()
    reconstruction = _reconstruction(source_document_id=document_id)
    document_store = _DocumentStore(b"sanction bytes")
    bundle = build_bundle(
        reconstruction,
        documents=[
            BundleDocument(
                document_id=document_id,
                filename="sanction-letter.pdf",
                storage_key="sha256/document",
                content_hash=hashlib.sha256(b"sanction bytes").hexdigest(),
                byte_size=len(b"sanction bytes"),
            )
        ],
        document_store=document_store,
    )

    verification = verify_bundle(bundle.content)

    assert verification.valid
    assert verification.chain_verified is True
    assert verification.manifest_hash == bundle.manifest_hash
    assert verification.checked_files == 4


def test_altered_file_fails_verification_naming_it() -> None:
    reconstruction = _reconstruction()
    bundle = build_bundle(reconstruction)

    altered = _archive_with_replaced_file(bundle.content, "reconstruction.json", b"altered")
    verification = verify_bundle(altered)

    assert not verification.valid
    assert any("reconstruction.json" in failure for failure in verification.failures)


def test_missing_document_recorded_and_still_verifies() -> None:
    document_id = uuid4()
    reconstruction = _reconstruction(source_document_id=document_id)
    bundle = build_bundle(
        reconstruction,
        documents=[
            BundleDocument(
                document_id=document_id,
                filename="sanction-letter.pdf",
                storage_key="sha256/document",
                content_hash="legacy-content-reference",
            )
        ],
        document_store=_DocumentStore(b"sanction bytes", missing=True),
    )

    verification = verify_bundle(bundle.content)
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    assert verification.valid
    assert manifest["missing_documents"][0]["document_id"] == str(document_id)
    assert "missing from storage" in manifest["missing_documents"][0]["reason"]


def test_chain_failure_stated_prominently() -> None:
    reconstruction = _reconstruction()
    audit_store = InMemoryAuditStore()
    recorder = AuditRecorder(audit_store, clock=FixedClock(_NOW_LOCAL), request_id="rq-t069")
    recorder.record(
        "forecast_candidate_scored",
        ("forecast", reconstruction.forecast_id),
        {"forecast_id": str(reconstruction.forecast_id)},
        actor=None,
        request_id="rq-t069",
        occurred_at=_NOW_LOCAL,
    )
    audit_store.rows()[0].payload["forecast_id"] = "tampered"
    bundle = build_bundle(reconstruction, audit_rows=audit_store.rows())

    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        audit_payload = json.loads(archive.read("audit_chain.json"))
        pdf_text = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(io.BytesIO(archive.read("reconstruction.pdf"))).pages
        )

    assert audit_payload["verification"]["verified"] is False
    assert "AUDIT CHAIN VERIFICATION FAILED" in pdf_text
    assert "sequence 1" in pdf_text


def test_large_bundle_async_with_notification() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        world = _build_world(session)
        source = b"x" * 1024
        assert world.document is not None
        world.document.content_hash = hashlib.sha256(source).hexdigest()
        world.document.byte_size = len(source)
        principal = Principal.user(
            world.principal.id,
            (Permission.VIEW_AUDIT, Permission.EXPORT_EVIDENCE),
        )
        notifier = _Notifier()
        result = ReconstructionService(
            session,
            clock=FixedClock(_NOW),
            document_store=_DocumentStore(source),
            notifier=notifier,
            async_threshold_bytes=1,
        ).export_bundle(principal, world.forecast.id, scope=world.scope)

        completed = result.result(timeout=20)

        assert result.accepted
        assert completed.complete
        assert completed.content is not None
        assert verify_bundle(completed.content).valid
        assert notifier.messages[0][0] == "evidence_bundle_ready"
    finally:
        session.close()
        engine.dispose()


def test_export_audited() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        world = _build_world(session, with_document=False)
        audit = _AuditRecorder()
        principal = Principal.user(
            world.principal.id,
            (Permission.VIEW_AUDIT, Permission.EXPORT_EVIDENCE),
        )
        result = ReconstructionService(
            session,
            clock=FixedClock(_NOW),
            audit=audit,
        ).export_bundle(principal, world.forecast.id, scope=world.scope)

        assert result.complete
        assert audit.events[0][0] == "evidence_bundle_exported"
        assert audit.events[0][1] == ("forecast", world.forecast.id)
        assert audit.events[0][2]["bundle_id"] == str(result.bundle_id)
        assert session.query(AuditEvent).count() == 0
    finally:
        session.close()
        engine.dispose()
