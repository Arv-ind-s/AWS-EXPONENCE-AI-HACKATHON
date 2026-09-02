"""Presentation models for the audit search and warning reconstruction screens.

The web layer receives immutable database/domain values and turns them into
small, explicit view values.  Templates do not inspect ORM attributes,
perform date formatting, or decide which reconstruction parts exist.  That
keeps escaping, ordering, and the distinction between an absent and a purged
record in one testable place.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from covenant_radar.audit.chain import AuditChainBreak
from covenant_radar.audit.reconstruct import PartStatus, WarningReconstruction, json_safe

DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_SIZE: Final[int] = 200
MAX_CURSOR_LENGTH: Final[int] = 512
_CURSOR_VERSION: Final[int] = 1
_CURSOR_SECRET_ENV: Final[str] = "COVENANT_RADAR_AUDIT_CURSOR_SECRET"
_PROCESS_CURSOR_SECRET: Final[bytes] = secrets.token_bytes(32)
_BASE64_ALPHABET: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")


class InvalidAuditCursor(ValueError):
    """A cursor is malformed, unauthenticated, or carries invalid fields."""


@dataclass(frozen=True, slots=True)
class AuditCursor:
    """Authenticated seek position bound to one audit filter set."""

    sequence: int
    filters_digest: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise ValueError("Audit cursor sequence must be a positive integer.")
        if (
            not isinstance(self.filters_digest, str)
            or len(self.filters_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.filters_digest)
        ):
            raise ValueError("Audit cursor filters_digest must be a lowercase SHA-256 digest.")

    def encode(self, secret: bytes | str | None = None) -> str:
        """Return an opaque, tamper-evident cursor token."""

        payload = {
            "filters_digest": self.filters_digest,
            "sequence": self.sequence,
            "v": _CURSOR_VERSION,
        }
        body = _urlsafe(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(_cursor_secret(secret), body, hashlib.sha256).digest()
        return f"{body.decode('ascii')}.{_urlsafe(signature).decode('ascii')}"

    @classmethod
    def decode(cls, token: str, secret: bytes | str | None = None) -> AuditCursor:
        """Verify and decode a cursor without trusting client-supplied fields."""

        if not isinstance(token, str) or not 1 <= len(token) <= MAX_CURSOR_LENGTH:
            raise InvalidAuditCursor("Audit cursor is malformed.")
        parts = token.split(".")
        if len(parts) != 2:
            raise InvalidAuditCursor("Audit cursor is malformed.")
        try:
            encoded_body = parts[0].encode("ascii")
            body = _urlsafe_decode(parts[0])
            supplied_signature = _urlsafe_decode(parts[1])
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise InvalidAuditCursor("Audit cursor is malformed.") from error
        expected_signature = hmac.new(_cursor_secret(secret), encoded_body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidAuditCursor("Audit cursor authentication failed.")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidAuditCursor("Audit cursor payload is malformed.") from error
        if not isinstance(payload, dict) or set(payload) != {"filters_digest", "sequence", "v"}:
            raise InvalidAuditCursor("Audit cursor payload is malformed.")
        if payload.get("v") != _CURSOR_VERSION:
            raise InvalidAuditCursor("Audit cursor version is unsupported.")
        try:
            return cls(sequence=payload["sequence"], filters_digest=payload["filters_digest"])
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidAuditCursor("Audit cursor fields are malformed.") from error

    from_token = decode


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """The normalized filters that are bound into a search and its cursor."""

    actor: str | None = None
    subject: str | None = None
    subject_type: str | None = None
    event_type: str | None = None
    event_id: str | None = None
    from_date: date | None = None
    to_date: date | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a stable, non-sensitive representation for hashing/audit."""

        return {
            "actor": self.actor,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "from_date": self.from_date.isoformat() if self.from_date is not None else None,
            "subject": self.subject,
            "subject_type": self.subject_type,
            "to_date": self.to_date.isoformat() if self.to_date is not None else None,
        }

    def digest(self) -> str:
        """Return the SHA-256 binding used by :class:`AuditCursor`."""

        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def query_string(self, *, cursor: str | None = None, page_size: int | None = None) -> str:
        """Build a canonical query string for links that preserve filters."""

        from urllib.parse import urlencode

        values: list[tuple[str, str]] = []
        for key, value in (
            ("actor", self.actor),
            ("subject", self.subject),
            ("subject_type", self.subject_type),
            ("event_type", self.event_type),
            ("event_id", self.event_id),
            ("from_date", self.from_date.isoformat() if self.from_date else None),
            ("to_date", self.to_date.isoformat() if self.to_date else None),
        ):
            if value:
                values.append((key, value))
        if page_size is not None:
            values.append(("page_size", str(page_size)))
        if cursor:
            values.append(("cursor", cursor))
        return urlencode(values)


@dataclass(frozen=True, slots=True)
class AuditEventRow:
    """Escaped-ready representation of one immutable audit event."""

    event_id: str
    sequence: int
    occurred_at: datetime
    occurred_at_display: str
    actor: str
    event_type: str
    subject_type: str
    subject_id: str
    subject_display: str
    payload: Mapping[str, object]
    payload_display: str
    prev_hash: str | None
    hash: str
    warning_href: str | None


@dataclass(frozen=True, slots=True)
class ChainVerificationView:
    """The chain result rendered at the top of an audit surface."""

    verified: bool
    status: str
    message: str
    failure: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class AuditSearchView:
    """Complete data required by the audit event search template."""

    filters: AuditFilters
    rows: tuple[AuditEventRow, ...]
    total_count: int
    next_cursor: str | None
    page_size: int
    chain: ChainVerificationView
    export_href: str
    next_href: str | None

    @property
    def empty(self) -> bool:
        """Whether the current filter has no matching events."""

        return not self.rows


@dataclass(frozen=True, slots=True)
class ReconstructionPartView:
    """One ordered reconstruction section and its provenance references."""

    key: str
    title: str
    status: str
    details: object
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconstructionView:
    """Complete warning reconstruction presentation state."""

    forecast_id: UUID
    run_id: UUID
    as_of_date: date
    horizon_days: int
    parts: tuple[ReconstructionPartView, ...]
    chain: ChainVerificationView
    can_export: bool
    bundle_href: str


def build_chain_view(
    result: AuditChainBreak | Mapping[str, object] | None,
) -> ChainVerificationView:
    """Normalize a chain verifier result into the prominent UI shape."""

    if result is None:
        return ChainVerificationView(
            verified=True,
            status="verified",
            message="Audit chain verified.",
        )
    if isinstance(result, AuditChainBreak):
        failure: Mapping[str, object] = {
            "sequence": result.sequence,
            "previous_sequence": result.previous_sequence,
            "reason": result.reason,
            "expected_prev_hash": result.expected_prev_hash,
            "actual_prev_hash": result.actual_prev_hash,
            "expected_hash": result.expected_hash,
            "actual_hash": result.actual_hash,
        }
        return ChainVerificationView(
            verified=False,
            status="failed",
            message=result.message,
            failure=failure,
        )
    if not isinstance(result, Mapping):
        raise TypeError("Chain verification must be an AuditChainBreak, mapping, or None.")
    verified = result.get("verified") is True
    message = result.get("message")
    if not isinstance(message, str) or not message:
        message = "Audit chain verified." if verified else "Audit chain verification failed."
    failure_value = result.get("failure")
    mapping_failure = dict(failure_value) if isinstance(failure_value, Mapping) else None
    return ChainVerificationView(
        verified=verified,
        status="verified" if verified else "failed",
        message=message,
        failure=mapping_failure,
    )


def build_audit_event_row(row: object) -> AuditEventRow:
    """Shape one ORM-like audit row, refusing incomplete data."""

    event_id = _required_uuid(getattr(row, "id", None), "id")
    sequence = _required_positive_int(getattr(row, "sequence", None), "sequence")
    occurred_at = _required_datetime(getattr(row, "occurred_at", None), "occurred_at")
    event_type = _required_text(getattr(row, "event_type", None), "event_type")
    subject_type = _required_text(getattr(row, "subject_type", None), "subject_type")
    subject_id = _required_uuid(getattr(row, "subject_id", None), "subject_id")
    actor_id = getattr(row, "actor_id", None)
    actor_label = getattr(row, "actor_label", None)
    if actor_label is not None and not isinstance(actor_label, str):
        raise TypeError("Audit actor_label must be text or None.")
    actor = actor_label or (str(actor_id) if isinstance(actor_id, UUID) else "System")
    payload = getattr(row, "payload", None)
    if not isinstance(payload, Mapping):
        raise TypeError("Audit payload must be a mapping.")
    payload_safe = json_safe(dict(payload))
    if not isinstance(payload_safe, Mapping):  # pragma: no cover - json_safe preserves mappings
        raise TypeError("Audit payload could not be normalized.")
    stored_hash = _required_text(getattr(row, "hash", None), "hash")
    previous_hash = getattr(row, "prev_hash", None)
    if previous_hash is not None and not isinstance(previous_hash, str):
        raise TypeError("Audit prev_hash must be text or None.")
    return AuditEventRow(
        event_id=str(event_id),
        sequence=sequence,
        occurred_at=occurred_at,
        occurred_at_display=_ist_timestamp(occurred_at),
        actor=actor,
        event_type=event_type,
        subject_type=subject_type,
        subject_id=str(subject_id),
        subject_display=f"{subject_type}: {subject_id}",
        payload=payload_safe,
        payload_display=json.dumps(payload_safe, sort_keys=True, separators=(",", ":")),
        prev_hash=previous_hash,
        hash=stored_hash,
        warning_href=(f"/audit/warnings/{subject_id}" if subject_type == "forecast" else None),
    )


def build_audit_search_view(
    rows: Sequence[object],
    *,
    filters: AuditFilters,
    total_count: int,
    next_cursor: str | None,
    page_size: int,
    chain_status: AuditChainBreak | Mapping[str, object] | None = None,
) -> AuditSearchView:
    """Build the search screen with canonical pagination links."""

    _validate_page_size(page_size)
    if not isinstance(filters, AuditFilters):
        raise TypeError("filters must be an AuditFilters value.")
    if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
        raise ValueError("total_count must be a non-negative integer.")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise TypeError("next_cursor must be text or None.")
    export_query = filters.query_string(page_size=page_size)
    export_href = "/audit/export" + (f"?{export_query}" if export_query else "")
    next_href = None
    if next_cursor:
        next_query = filters.query_string(cursor=next_cursor, page_size=page_size)
        next_href = "/audit" + (f"?{next_query}" if next_query else "")
    return AuditSearchView(
        filters=filters,
        rows=tuple(build_audit_event_row(row) for row in rows),
        total_count=total_count,
        next_cursor=next_cursor,
        page_size=page_size,
        chain=build_chain_view(chain_status),
        export_href=export_href,
        next_href=next_href,
    )


def build_reconstruction_view(
    reconstruction: WarningReconstruction,
    *,
    chain_status: AuditChainBreak | Mapping[str, object] | None = None,
    can_export: bool = False,
) -> ReconstructionView:
    """Build the ordered, provenance-bearing reconstruction timeline."""

    if not isinstance(reconstruction, WarningReconstruction):
        raise TypeError("reconstruction must be a WarningReconstruction.")
    if not isinstance(can_export, bool):
        raise TypeError("can_export must be a boolean.")

    source = reconstruction.source_data
    parts = (
        ReconstructionPartView(
            key="source_data",
            title="Source data",
            status=source.status.value,
            details=json_safe(source.as_dict()),
            provenance=_source_provenance(source.as_dict()),
        ),
        _mapping_part(
            "formula_inputs",
            "Formula inputs",
            reconstruction.formula_inputs,
            (f"forecast:{reconstruction.forecast_id}", f"forecast_run:{reconstruction.run_id}"),
        ),
        _mapping_part(
            "covenant_version",
            "Covenant version",
            reconstruction.covenant_version,
            _mapping_provenance(reconstruction.covenant_version, "covenant_version"),
        ),
        _mapping_part(
            "thresholds",
            "Thresholds in force",
            reconstruction.thresholds,
            _mapping_provenance(reconstruction.thresholds, "threshold_snapshot"),
        ),
        _optional_mapping_part(
            "calculation",
            "Calculation",
            reconstruction.calculation,
            _mapping_provenance(reconstruction.calculation, "calculation"),
        ),
        _sequence_part(
            "trend",
            "Trend",
            reconstruction.trend,
            (
                f"forecast_run:{reconstruction.run_id}",
                f"covenant_version:{reconstruction.covenant_version_id}",
            ),
        ),
        _mapping_part(
            "forecast",
            "Forecast",
            reconstruction.forecast,
            (f"forecast:{reconstruction.forecast_id}", f"forecast_run:{reconstruction.run_id}"),
        ),
        _sequence_part(
            "evidence",
            "Evidence in force",
            tuple(item.as_dict() for item in reconstruction.evidence),
            tuple(f"evidence:{item.id}" for item in reconstruction.evidence),
        ),
        _sequence_part(
            "drivers",
            "Drivers",
            tuple(item.as_dict() for item in reconstruction.drivers),
            _driver_provenance(reconstruction.drivers, reconstruction.forecast_id),
        ),
        ReconstructionPartView(
            key="memo",
            title="Memo and model-call record",
            status=reconstruction.memo.status.value,
            details=json_safe(reconstruction.memo.as_dict()),
            provenance=(
                (f"memo:{reconstruction.memo.id}",) if reconstruction.memo.id is not None else ()
            ),
        ),
        _sequence_part(
            "overrides",
            "Overrides",
            tuple(item.as_dict() for item in reconstruction.overrides),
            tuple(f"override:{item.id}" for item in reconstruction.overrides),
        ),
        _sequence_part(
            "dispositions",
            "Dispositions",
            tuple(item.as_dict() for item in reconstruction.dispositions),
            tuple(f"disposition:{item.id}" for item in reconstruction.dispositions),
        ),
    )
    return ReconstructionView(
        forecast_id=reconstruction.forecast_id,
        run_id=reconstruction.run_id,
        as_of_date=reconstruction.as_of_date,
        horizon_days=reconstruction.horizon_days,
        parts=parts,
        chain=build_chain_view(chain_status),
        can_export=can_export,
        bundle_href=f"/audit/warnings/{reconstruction.forecast_id}/bundle",
    )


def _mapping_part(
    key: str,
    title: str,
    details: Mapping[str, object],
    provenance: tuple[str, ...],
) -> ReconstructionPartView:
    return ReconstructionPartView(
        key=key,
        title=title,
        status="present",
        details=json_safe(dict(details)),
        provenance=provenance,
    )


def _optional_mapping_part(
    key: str,
    title: str,
    details: Mapping[str, object] | None,
    provenance: tuple[str, ...],
) -> ReconstructionPartView:
    return ReconstructionPartView(
        key=key,
        title=title,
        status="present" if details else PartStatus.NOT_GENERATED.value,
        details=json_safe(dict(details)) if details else {},
        provenance=provenance,
    )


def _sequence_part(
    key: str,
    title: str,
    details: Sequence[object],
    provenance: tuple[str, ...],
) -> ReconstructionPartView:
    return ReconstructionPartView(
        key=key,
        title=title,
        status="present" if details else PartStatus.NOT_GENERATED.value,
        details=json_safe(list(details)),
        provenance=provenance,
    )


def _source_provenance(details: Mapping[str, object]) -> tuple[str, ...]:
    references: list[str] = []
    if details.get("id") is not None:
        references.append(f"document:{details['id']}")
    if details.get("span_id") is not None:
        references.append(f"document_span:{details['span_id']}")
    purged = details.get("purged")
    if isinstance(purged, Mapping) and purged.get("rule"):
        references.append(f"retention_rule:{purged['rule']}")
    return tuple(references)


def _driver_provenance(drivers: Sequence[object], forecast_id: UUID) -> tuple[str, ...]:
    references: list[str] = [f"forecast:{forecast_id}"]
    for driver in drivers:
        evidence_id = getattr(driver, "evidence_id", None)
        if isinstance(evidence_id, UUID):
            references.append(f"evidence:{evidence_id}")
    return tuple(dict.fromkeys(references))


def _mapping_provenance(details: Mapping[str, object] | None, fallback: str) -> tuple[str, ...]:
    if not details:
        return ()
    references: list[str] = []
    for key in ("id", "covenant_version_id", "covenant_id", "run_id", "forecast_id"):
        value = details.get(key)
        if value is not None:
            references.append(f"{fallback}:{value}")
    return tuple(dict.fromkeys(references))


def _required_uuid(value: object, field: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"Audit row field {field!r} is missing or invalid.")
    return value


def _required_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"Audit row field {field!r} is missing or invalid.")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Audit row field {field!r} is missing or invalid.")
    return value


def _required_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Audit row field {field!r} must be timezone-aware.")
    return value.astimezone(UTC)


def _ist_timestamp(value: datetime) -> str:
    return value.astimezone(_IST).strftime("%d %b %Y, %H:%M:%S IST")


def _validate_page_size(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError(f"Audit page_size must be between 1 and {MAX_PAGE_SIZE}.")


def _cursor_secret(value: bytes | str | None) -> bytes:
    if value is None:
        configured = os.environ.get(_CURSOR_SECRET_ENV)
        return _cursor_secret(configured) if configured else _PROCESS_CURSOR_SECRET
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("Audit cursor secret must contain at least 32 bytes.")
    return value


def _urlsafe(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _urlsafe_decode(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or any(character not in _BASE64_ALPHABET for character in value)
    ):
        raise ValueError("Invalid base64 value.")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = [
    "AuditCursor",
    "AuditEventRow",
    "AuditFilters",
    "AuditSearchView",
    "ChainVerificationView",
    "DEFAULT_PAGE_SIZE",
    "InvalidAuditCursor",
    "MAX_PAGE_SIZE",
    "ReconstructionPartView",
    "ReconstructionView",
    "build_audit_event_row",
    "build_audit_search_view",
    "build_chain_view",
    "build_reconstruction_view",
]
