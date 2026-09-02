"""Append-only persistence for the common stage trace.

Trace rows do not own a portfolio path themselves; their subject does.  The
repository therefore takes an explicit ``(subject_type, subject_id)`` pair and
does not offer an unscoped generic ``get`` method.  Services that expose a
trace must resolve and authorise that subject before calling this adapter.

There is intentionally no update or delete method.  Re-running a stage adds a
new row.  ``read`` selects the latest row per stage for the explainability
surface and pads stages with no row using an in-memory ``not_run`` record;
``history`` remains available for reconstruction and audit work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.audit import TraceRow
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.trace import (
    TRACE_STAGE_MAX,
    TRACE_STAGE_MIN,
    TraceReadRecord,
    TraceRecord,
    stage_record,
)

_DEFAULT_REQUEST_ID: Final[str] = "system-trace"
_SUBJECT_TYPE_MAX_LENGTH: Final[int] = 50
_REQUEST_ID_MAX_LENGTH: Final[int] = 40


@dataclass(frozen=True, slots=True)
class TraceSubject:
    """The explicit identity a group of stage rows explains."""

    subject_type: str
    subject_id: UUID

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject_type", _subject_type(self.subject_type))
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Trace subject_id must be a UUID.")


class TraceRepository:
    """SQLAlchemy adapter for append-only ``trace_row`` history."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Clock | None = None,
        request_id: str | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("TraceRepository requires a SQLAlchemy Session.")
        self.session = session
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or _DEFAULT_REQUEST_ID)

    def write(
        self,
        subject: TraceSubject | tuple[str, UUID],
        record: TraceRecord,
        *,
        actor_id: UUID | None = None,
        request_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> TraceRow:
        """Append one validated trace record in the caller's transaction.

        The method flushes so a caller can immediately use the generated row
        id, but it never commits.  A failed flush is left to the owning unit
        of work to roll back along with the stage that produced the record.
        """

        subject_value = _coerce_subject(subject)
        if not isinstance(record, TraceRecord):
            raise TypeError("TraceRepository.write requires a TraceRecord.")
        validated_record = stage_record(
            record.stage,
            record.decider,
            record.inputs,
            record.outputs,
            record.rule_or_prompt_version,
            record.thresholds_compared,
            record.confidence,
            record.sources,
        )
        if actor_id is not None and not isinstance(actor_id, UUID):
            raise ValueError("Trace actor_id must be a UUID or null.")
        effective_request_id = _request_id(request_id or self.request_id)
        effective_occurred_at = _instant(occurred_at or self.clock.now())

        row = TraceRow(
            id=new_id(),
            subject_type=subject_value.subject_type,
            subject_id=subject_value.subject_id,
            stage=str(validated_record.stage),
            decider=validated_record.decider,
            inputs=dict(validated_record.inputs),
            outputs=dict(validated_record.outputs),
            rule_or_prompt_version=validated_record.rule_or_prompt_version,
            thresholds_compared=[dict(item) for item in validated_record.thresholds_compared],
            confidence=validated_record.confidence,
            sources=list(validated_record.sources),
            occurred_at=effective_occurred_at,
            created_at=effective_occurred_at,
            updated_at=effective_occurred_at,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=effective_request_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def append(
        self,
        subject: TraceSubject | tuple[str, UUID],
        record: TraceRecord,
        *,
        actor_id: UUID | None = None,
        request_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> TraceRow:
        """Semantic alias for :meth:`write` used by append-only call sites."""

        return self.write(
            subject,
            record,
            actor_id=actor_id,
            request_id=request_id,
            occurred_at=occurred_at,
        )

    def read(
        self,
        subject: TraceSubject | tuple[str, UUID],
    ) -> tuple[TraceReadRecord, ...]:
        """Return the latest visible record for every stage, in stage order.

        A missing stage is represented only in this return value and is marked
        ``not_run``.  No synthetic row is inserted into the database.
        """

        subject_value = _coerce_subject(subject)
        rows = self._rows(subject_value)
        latest: dict[int, TraceRow] = {}
        for row in rows:
            stage = _stored_stage(row.stage)
            latest[stage] = row

        result: list[TraceReadRecord] = []
        for stage in range(TRACE_STAGE_MIN, TRACE_STAGE_MAX + 1):
            latest_row = latest.get(stage)
            if latest_row is None:
                result.append(
                    TraceReadRecord(
                        stage=stage,
                        decider=None,
                        inputs={},
                        outputs={},
                        rule_or_prompt_version=None,
                        thresholds_compared=(),
                        confidence=None,
                        sources=(),
                        not_run=True,
                        subject_type=subject_value.subject_type,
                        subject_id=subject_value.subject_id,
                    )
                )
            else:
                result.append(_read_record(latest_row))
        return tuple(result)

    def history(
        self,
        subject: TraceSubject | tuple[str, UUID],
        *,
        stage: int | None = None,
    ) -> tuple[TraceRow, ...]:
        """Return append-only history, oldest first, optionally for one stage."""

        subject_value = _coerce_subject(subject)
        if stage is not None:
            if isinstance(stage, bool) or not isinstance(stage, int):
                raise ValueError("Trace history stage must be an integer.")
            if not TRACE_STAGE_MIN <= stage <= TRACE_STAGE_MAX:
                raise ValueError(
                    f"Trace history stage must be between {TRACE_STAGE_MIN} and {TRACE_STAGE_MAX}."
                )
        rows = self._rows(subject_value)
        if stage is None:
            return rows
        return tuple(row for row in rows if _stored_stage(row.stage) == stage)

    def _rows(self, subject: TraceSubject) -> tuple[TraceRow, ...]:
        statement: Select[tuple[TraceRow]] = select(TraceRow).where(
            TraceRow.subject_type == subject.subject_type,
            TraceRow.subject_id == subject.subject_id,
        )
        statement = statement.order_by(TraceRow.id)
        return tuple(self.session.execute(statement).scalars().all())


def _coerce_subject(value: TraceSubject | tuple[str, UUID]) -> TraceSubject:
    if isinstance(value, TraceSubject):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        subject_type, subject_id = value
        return TraceSubject(subject_type, subject_id)
    raise ValueError("Trace subject must be a (subject_type, subject_id) pair.")


def _subject_type(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Trace subject_type must be non-empty text.")
    cleaned = value.strip()
    if len(cleaned) > _SUBJECT_TYPE_MAX_LENGTH:
        raise ValueError(
            f"Trace subject_type must be at most {_SUBJECT_TYPE_MAX_LENGTH} characters."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError("Trace subject_type contains an invalid control character.")
    return cleaned


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Trace request_id must be non-empty text.")
    cleaned = value.strip()
    if len(cleaned) > _REQUEST_ID_MAX_LENGTH:
        raise ValueError(f"Trace request_id must be at most {_REQUEST_ID_MAX_LENGTH} characters.")
    return cleaned


def _instant(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Trace occurred_at must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def _stored_stage(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("Stored trace stage is not valid text.")
    try:
        stage = int(value)
    except ValueError as error:
        raise ValueError(f"Stored trace stage {value!r} is not a valid stage number.") from error
    if not TRACE_STAGE_MIN <= stage <= TRACE_STAGE_MAX:
        raise ValueError(f"Stored trace stage {stage} is outside the defined range.")
    return stage


def _read_record(row: TraceRow) -> TraceReadRecord:
    """Validate the database payload again before exposing it to a reader."""

    record = stage_record(
        _stored_stage(row.stage),
        row.decider,
        row.inputs,
        row.outputs,
        row.rule_or_prompt_version,
        row.thresholds_compared,
        row.confidence,
        row.sources,
    )
    return TraceReadRecord(
        stage=record.stage,
        decider=record.decider,
        inputs=record.inputs,
        outputs=record.outputs,
        rule_or_prompt_version=record.rule_or_prompt_version,
        thresholds_compared=record.thresholds_compared,
        confidence=record.confidence,
        sources=record.sources,
        row_id=row.id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        request_id=row.request_id,
        occurred_at=row.occurred_at,
    )


__all__ = ["TraceRepository", "TraceSubject"]
