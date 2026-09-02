"""Warning reconstruction value objects (`T-068`, `spec §R-20.a/d`, `C-15`).

Given a forecast's own run, every part of the warning it represents — the
source data with its provenance, the covenant version and thresholds in
force then, the calculation and trend that produced it, the evidence in
force, the forecast itself, its memo, and any overrides or dispositions
recorded against it — is reachable from one :class:`WarningReconstruction`
value.

This module performs no I/O and imports neither SQLAlchemy nor
``covenant_radar.db``: it is handed already-resolved, already-point-in-time
facts by :mod:`covenant_radar.services.reconstruction` and only shapes,
validates and serialises them. That keeps the assembly testable with plain
fixtures and reusable unchanged by a later export (`T-069`).

A referenced row that retention has since purged is never silently dropped
and never invented: the caller passes a :class:`PurgedReference` naming the
rule and the purge date instead of leaving a blank space that would read as
"there was nothing here."
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

_MAX_TEXT_LENGTH = 4000


class PartStatus(str, Enum):
    """How one part of a reconstruction is represented."""

    PRESENT = "present"
    NOT_GENERATED = "not_generated"
    PURGED = "purged"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class PurgedReference:
    """A named, dated proof that a referenced row was purged, not lost.

    `RetentionPurgeLog.criteria` (`plan.md §5.9`) is a job-defined JSON blob;
    the retention job itself (`T-167`) is out of scope and does not exist
    yet. Until it does, this reconstruction reads ``criteria`` expecting an
    ``entity_id`` (or ``document_id``/``id``) key naming the purged row and
    a ``rule`` key naming the retention rule that purged it — the shape a
    future purge job should write so this lookup and its own stay aligned.
    """

    entity: str
    entity_id: UUID
    rule: str
    purged_at: datetime
    purged_count: int

    def __post_init__(self) -> None:
        _text(self.entity, "entity", 100)
        _uuid(self.entity_id, "entity_id")
        _text(self.rule, "rule", 200)
        _aware_datetime(self.purged_at, "purged_at")
        _non_negative_int(self.purged_count, "purged_count")

    def as_dict(self) -> dict[str, object]:
        return {
            "entity": self.entity,
            "entity_id": str(self.entity_id),
            "rule": self.rule,
            "purged_at": self.purged_at.isoformat(),
            "purged_count": self.purged_count,
        }


@dataclass(frozen=True, slots=True)
class SupersessionNote:
    """A later supersession, noted separately from the point-in-time state.

    `spec §R-20.a`'s "Every case": *"evidence superseded since → the state
    as of then is shown, with the later supersession noted separately."*
    """

    occurred_on: date
    rule: str
    superseded_by_id: UUID | None

    def __post_init__(self) -> None:
        _calendar_date(self.occurred_on, "occurred_on")
        _text(self.rule, "rule", 200)
        if self.superseded_by_id is not None:
            _uuid(self.superseded_by_id, "superseded_by_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "occurred_on": self.occurred_on.isoformat(),
            "rule": self.rule,
            "superseded_by_id": (
                str(self.superseded_by_id) if self.superseded_by_id is not None else None
            ),
        }


def supersession_note(
    *,
    current_state: str,
    current_superseded_by_id: UUID | None,
    transitions: Sequence[tuple[date, str, str]],
    as_of: date,
) -> SupersessionNote | None:
    """Whether an evidence item's current state differs from its as-of state.

    ``transitions`` is the item's complete ``(occurred_on, to_state, rule)``
    history. A note is produced only when the item is *currently* superseded
    but a transition to ``superseded`` occurred strictly after ``as_of`` —
    the point-in-time state already reflects everything on or before that
    date, so an earlier-or-equal supersession is not "since" anything.
    """

    if current_state != "superseded":
        return None
    later = sorted(
        (entry for entry in transitions if entry[1] == "superseded" and entry[0] > as_of),
        key=lambda entry: entry[0],
    )
    if not later:
        return None
    occurred_on, _to_state, rule = later[0]
    return SupersessionNote(
        occurred_on=occurred_on,
        rule=rule,
        superseded_by_id=current_superseded_by_id,
    )


@dataclass(frozen=True, slots=True)
class EvidencePart:
    """One evidence item's state as of the forecast's own run."""

    id: UUID
    family: str
    evidence_type: str
    first_seen: date
    last_seen: date
    state: str
    materiality_pct: Decimal | None
    decay_factor: Decimal | None
    counts_toward_pressure: bool
    superseded_by_id: UUID | None
    supersedes_id: UUID | None
    superseded_since: SupersessionNote | None = None

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        _text(self.family, "family", 50)
        _text(self.evidence_type, "evidence_type", 100)
        _calendar_date(self.first_seen, "first_seen")
        _calendar_date(self.last_seen, "last_seen")
        _text(self.state, "state", 50)
        if not isinstance(self.counts_toward_pressure, bool):
            raise TypeError("counts_toward_pressure must be a boolean.")
        if self.superseded_since is not None and not isinstance(
            self.superseded_since, SupersessionNote
        ):
            raise TypeError("superseded_since must be a SupersessionNote or None.")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "family": self.family,
            "evidence_type": self.evidence_type,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "state": self.state,
            "materiality_pct": json_safe(self.materiality_pct),
            "decay_factor": json_safe(self.decay_factor),
            "counts_toward_pressure": self.counts_toward_pressure,
            "superseded_by_id": (
                str(self.superseded_by_id) if self.superseded_by_id is not None else None
            ),
            "supersedes_id": (
                str(self.supersedes_id) if self.supersedes_id is not None else None
            ),
            "superseded_since": (
                self.superseded_since.as_dict() if self.superseded_since is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class DriverPart:
    """One attributed driver behind the forecast's outcome."""

    name: str
    share: Decimal
    evidence_id: UUID | None
    is_other: bool

    def __post_init__(self) -> None:
        _text(self.name, "name", 100)
        if not isinstance(self.share, Decimal) or not self.share.is_finite():
            raise TypeError("share must be a finite Decimal.")
        if self.evidence_id is not None:
            _uuid(self.evidence_id, "evidence_id")
        if not isinstance(self.is_other, bool):
            raise TypeError("is_other must be a boolean.")

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "share": json_safe(self.share),
            "evidence_id": str(self.evidence_id) if self.evidence_id is not None else None,
            "is_other": self.is_other,
        }


@dataclass(frozen=True, slots=True)
class MemoPart:
    """A warning's memo, or an explicit marker that none was generated."""

    status: PartStatus
    id: UUID | None = None
    template_version: str | None = None
    prompt_version: str | None = None
    drafted_text: str | None = None
    check_verdict: str | None = None
    generated_by_id: UUID | None = None
    generated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PartStatus) or self.status not in {
            PartStatus.PRESENT,
            PartStatus.NOT_GENERATED,
        }:
            raise ValueError("MemoPart.status must be PRESENT or NOT_GENERATED.")
        if self.status is PartStatus.PRESENT:
            _uuid(self.id, "id")
            _text(self.template_version, "template_version", 50)
            _text(self.drafted_text, "drafted_text", 200_000)
            _aware_datetime(self.generated_at, "generated_at")
        elif any(
            value is not None
            for value in (
                self.id,
                self.template_version,
                self.prompt_version,
                self.drafted_text,
                self.check_verdict,
                self.generated_by_id,
                self.generated_at,
            )
        ):
            raise ValueError("A not_generated MemoPart cannot carry memo fields.")

    @classmethod
    def present(
        cls,
        *,
        id: UUID,
        template_version: str,
        prompt_version: str | None,
        drafted_text: str,
        check_verdict: str | None,
        generated_by_id: UUID | None,
        generated_at: datetime,
    ) -> MemoPart:
        return cls(
            status=PartStatus.PRESENT,
            id=id,
            template_version=template_version,
            prompt_version=prompt_version,
            drafted_text=drafted_text,
            check_verdict=check_verdict,
            generated_by_id=generated_by_id,
            generated_at=generated_at,
        )

    @classmethod
    def not_generated(cls) -> MemoPart:
        return cls(status=PartStatus.NOT_GENERATED)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "id": str(self.id) if self.id is not None else None,
            "template_version": self.template_version,
            "prompt_version": self.prompt_version,
            "drafted_text": self.drafted_text,
            "check_verdict": self.check_verdict,
            "generated_by_id": (
                str(self.generated_by_id) if self.generated_by_id is not None else None
            ),
            "generated_at": (
                self.generated_at.isoformat() if self.generated_at is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class OverridePart:
    """One person overriding what the system showed for this warning."""

    id: UUID
    stage: str
    user_action: str
    reason: str
    actor_id: UUID
    recorded_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        _text(self.stage, "stage", 50)
        _text(self.user_action, "user_action", 50)
        _text(self.reason, "reason", 2000)
        _uuid(self.actor_id, "actor_id")
        _aware_datetime(self.recorded_at, "recorded_at")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "stage": self.stage,
            "user_action": self.user_action,
            "reason": self.reason,
            "actor_id": str(self.actor_id),
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DispositionPart:
    """One recorded outcome — acted, monitoring, dismissed — for this warning."""

    id: UUID
    outcome: str
    reason_code: str | None
    note: str | None
    actor_id: UUID
    recorded_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        _text(self.outcome, "outcome", 20)
        if self.reason_code is not None:
            _text(self.reason_code, "reason_code", 50)
        if self.note is not None:
            _text(self.note, "note", 4000)
        _uuid(self.actor_id, "actor_id")
        _aware_datetime(self.recorded_at, "recorded_at")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "note": self.note,
            "actor_id": str(self.actor_id),
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SourceDocumentPart:
    """The document (and anchored span) behind the covenant's registered
    terms — present, purged (named and dated), or never referenced."""

    status: PartStatus
    id: UUID | None = None
    filename: str | None = None
    doc_type: str | None = None
    content_hash: str | None = None
    retention_class: str | None = None
    span_id: UUID | None = None
    span_text: str | None = None
    purged: PurgedReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PartStatus):
            raise TypeError("SourceDocumentPart.status must be a PartStatus.")
        if self.status is PartStatus.PRESENT:
            _uuid(self.id, "id")
            _text(self.filename, "filename", 500)
            _text(self.doc_type, "doc_type", 50)
            _text(self.content_hash, "content_hash", 128)
            if self.purged is not None:
                raise ValueError("A present SourceDocumentPart cannot carry a purge record.")
        elif self.status is PartStatus.PURGED:
            _uuid(self.id, "id")
            if not isinstance(self.purged, PurgedReference):
                raise ValueError("A purged SourceDocumentPart requires a PurgedReference.")
            if self.purged.entity_id != self.id:
                raise ValueError("The purge record must name the same entity id.")
        else:  # ABSENT
            if self.filename is not None or self.purged is not None:
                raise ValueError("An absent SourceDocumentPart cannot carry document fields.")

    @classmethod
    def present(
        cls,
        *,
        id: UUID,
        filename: str,
        doc_type: str,
        content_hash: str,
        retention_class: str | None,
        span_id: UUID | None = None,
        span_text: str | None = None,
    ) -> SourceDocumentPart:
        return cls(
            status=PartStatus.PRESENT,
            id=id,
            filename=filename,
            doc_type=doc_type,
            content_hash=content_hash,
            retention_class=retention_class,
            span_id=span_id,
            span_text=span_text,
        )

    @classmethod
    def mark_purged(cls, document_id: UUID, purge: PurgedReference) -> SourceDocumentPart:
        return cls(status=PartStatus.PURGED, id=document_id, purged=purge)

    @classmethod
    def absent(cls, *, document_id: UUID | None = None) -> SourceDocumentPart:
        return cls(status=PartStatus.ABSENT, id=document_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "id": str(self.id) if self.id is not None else None,
            "filename": self.filename,
            "doc_type": self.doc_type,
            "content_hash": self.content_hash,
            "retention_class": self.retention_class,
            "span_id": str(self.span_id) if self.span_id is not None else None,
            "span_text": self.span_text,
            "purged": self.purged.as_dict() if self.purged is not None else None,
        }


@dataclass(frozen=True, slots=True)
class WarningReconstruction:
    """Every part of one warning, assembled as of its forecast's own run.

    ``formula_inputs``, ``covenant_version``, ``thresholds``,
    ``calculation``, ``trend`` and ``forecast`` are plain column/JSON-blob
    snapshots with no further identity of their own, so a second dataclass
    layer over them would only repeat their own column names; they are
    converted to JSON-safe values on demand by :meth:`as_dict`, via
    :func:`json_safe`. ``evidence``, ``drivers``, ``memo``, ``overrides``
    and ``dispositions`` carry real identity and provenance and are kept
    typed.
    """

    forecast_id: UUID
    run_id: UUID
    borrower_id: UUID
    covenant_version_id: UUID
    as_of_date: date
    horizon_days: int
    reconstructed_at: datetime
    source_data: SourceDocumentPart
    formula_inputs: Mapping[str, object]
    covenant_version: Mapping[str, object]
    thresholds: Mapping[str, object]
    calculation: Mapping[str, object] | None
    trend: Sequence[Mapping[str, object]]
    forecast: Mapping[str, object]
    evidence: Sequence[EvidencePart]
    drivers: Sequence[DriverPart]
    memo: MemoPart
    overrides: Sequence[OverridePart]
    dispositions: Sequence[DispositionPart]

    def __post_init__(self) -> None:
        _uuid(self.forecast_id, "forecast_id")
        _uuid(self.run_id, "run_id")
        _uuid(self.borrower_id, "borrower_id")
        _uuid(self.covenant_version_id, "covenant_version_id")
        _calendar_date(self.as_of_date, "as_of_date")
        _non_negative_int(self.horizon_days, "horizon_days")
        _aware_datetime(self.reconstructed_at, "reconstructed_at")
        if not isinstance(self.source_data, SourceDocumentPart):
            raise TypeError("source_data must be a SourceDocumentPart.")
        if not isinstance(self.memo, MemoPart):
            raise TypeError("memo must be a MemoPart.")
        for evidence_item in self.evidence:
            if not isinstance(evidence_item, EvidencePart):
                raise TypeError("evidence entries must be EvidencePart values.")
        for driver_item in self.drivers:
            if not isinstance(driver_item, DriverPart):
                raise TypeError("driver entries must be DriverPart values.")
        for override_item in self.overrides:
            if not isinstance(override_item, OverridePart):
                raise TypeError("override entries must be OverridePart values.")
        for disposition_item in self.dispositions:
            if not isinstance(disposition_item, DispositionPart):
                raise TypeError("disposition entries must be DispositionPart values.")

    def as_dict(self) -> dict[str, object]:
        """Return the complete, JSON-safe reconstruction view.

        Deterministic given deterministic inputs — the same warning
        reconstructed twice from unchanged point-in-time facts produces an
        identical dict, which is what ``spec §R-20.a`` and a later export
        (`T-069`) both depend on.
        """

        return {
            "forecast_id": str(self.forecast_id),
            "run_id": str(self.run_id),
            "borrower_id": str(self.borrower_id),
            "covenant_version_id": str(self.covenant_version_id),
            "as_of_date": self.as_of_date.isoformat(),
            "horizon_days": self.horizon_days,
            "reconstructed_at": self.reconstructed_at.isoformat(),
            "source_data": {
                **self.source_data.as_dict(),
                "formula_inputs": json_safe(dict(self.formula_inputs)),
            },
            "covenant_version": json_safe(dict(self.covenant_version)),
            "thresholds": json_safe(dict(self.thresholds)),
            "calculation": (
                json_safe(dict(self.calculation)) if self.calculation is not None else None
            ),
            "trend": [json_safe(dict(point)) for point in self.trend],
            "forecast": json_safe(dict(self.forecast)),
            "evidence": [item.as_dict() for item in self.evidence],
            "drivers": [item.as_dict() for item in self.drivers],
            "memo": self.memo.as_dict(),
            "overrides": [item.as_dict() for item in self.overrides],
            "dispositions": [item.as_dict() for item in self.dispositions],
        }


def json_safe(value: object) -> object:
    """Recursively convert a value into a plain, JSON-serialisable value."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _text(value: object, field_name: str, max_length: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text.")
    if len(value) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters.")
    return value


def _uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID.")
    return value


def _calendar_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field_name} must be a calendar date.")
    return value


def _aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be a timezone-aware datetime.")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


__all__ = [
    "DispositionPart",
    "DriverPart",
    "EvidencePart",
    "MemoPart",
    "OverridePart",
    "PartStatus",
    "PurgedReference",
    "SourceDocumentPart",
    "SupersessionNote",
    "WarningReconstruction",
    "json_safe",
    "supersession_note",
]
