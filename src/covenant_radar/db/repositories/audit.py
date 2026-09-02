"""SQLAlchemy adapter for the append-only audit event stream.

This adapter is intentionally not a general repository.  It has one append
operation and read-only inspection methods; callers cannot obtain a mutation
surface for an existing event.  The append operation is designed to run in
the unit of work opened by the service, so the business change and its audit
event commit together.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Final, cast

from sqlalchemy import Select, TextClause, select, text
from sqlalchemy.orm import Session

from covenant_radar.audit.chain import (
    AuditChainBreak,
    AuditChainRow,
    compute_event_hash,
    normalise_payload,
    verify_chain,
)
from covenant_radar.audit.store import AuditRecord, AuditStore
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.session import is_database_session

_POSTGRES_ADVISORY_LOCK_KEY: Final[int] = -3_077_764_845_787_479_801
_MISSING: Final[object] = object()
_AUDIT_WRITE_LOCK = RLock()


class AuditChainError(ValueError):
    """A caller supplied a previous digest that is not the current tail."""


class AuditRepository(AuditStore):
    """Append and inspect audit rows using one SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        if not is_database_session(session):
            raise TypeError("AuditRepository requires a SQLAlchemy Session.")
        self.session = session

    def append(
        self,
        record: AuditRecord,
        *,
        prev_hash: str | None | object = _MISSING,
        previous_hash: str | None | object = _MISSING,
    ) -> AuditEvent:
        """Append one event and flush it, without committing the session.

        The advisory lock is transaction-scoped on PostgreSQL.  SQLite uses
        an immediate write transaction plus a process lock, covering both
        the first-row case (where there is no row to lock) and normal tail
        reads.  The database trigger remains the final defence for a caller
        that bypasses this adapter.
        """

        if not isinstance(record, AuditRecord):
            raise TypeError("AuditRepository.append requires an AuditRecord.")
        if prev_hash is not _MISSING and previous_hash is not _MISSING:
            raise TypeError("Specify only one of prev_hash or previous_hash.")
        supplied_previous = previous_hash if previous_hash is not _MISSING else prev_hash

        with _AUDIT_WRITE_LOCK:
            self._serialise_writer()
            latest = self._latest_for_append()
            expected_previous = latest.hash if latest is not None else None
            if supplied_previous is not _MISSING and supplied_previous != expected_previous:
                raise AuditChainError(
                    "Audit event previous hash was refused: it does not match "
                    "the current chain tail."
                )
            sequence = latest.sequence + 1 if latest is not None else 1
            payload = normalise_payload(record.payload)
            digest = _digest(record, sequence, expected_previous, payload)
            row = AuditEvent(
                id=new_id(),
                sequence=sequence,
                occurred_at=record.occurred_at,
                actor_id=record.actor_id,
                actor_label=record.actor_label,
                event_type=record.event_type,
                subject_type=record.subject_type,
                subject_id=record.subject_id,
                payload=payload,
                threshold_snapshot_id=record.threshold_snapshot_id,
                prev_hash=expected_previous,
                hash=digest,
                created_at=record.occurred_at,
                updated_at=record.occurred_at,
                created_by_id=record.actor_id,
                updated_by_id=record.actor_id,
                request_id=record.request_id,
            )
            self.session.add(row)
            self.session.flush()
            return row

    def rows(
        self,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
    ) -> tuple[AuditEvent, ...]:
        """Return rows in sequence order within an optional inclusive range."""

        _validate_range(from_sequence, to_sequence)
        statement: Select[tuple[AuditEvent]] = select(AuditEvent)
        if from_sequence is not None:
            statement = statement.where(AuditEvent.sequence >= from_sequence)
        if to_sequence is not None:
            statement = statement.where(AuditEvent.sequence <= to_sequence)
        statement = statement.order_by(AuditEvent.sequence)
        return tuple(self.session.execute(statement).scalars().all())

    def get(self, sequence: int) -> AuditEvent | None:
        """Return one event by its immutable sequence number."""

        _validate_sequence(sequence, "sequence")
        return self.session.scalar(
            select(AuditEvent).where(AuditEvent.sequence == sequence).limit(1)
        )

    def latest(self) -> AuditEvent | None:
        """Return the current chain tail without changing the session."""

        return self._latest_for_append()

    def verify_chain(
        self,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
    ) -> AuditChainBreak | None:
        """Return the first detected break in an optional sequence range."""

        _validate_range(from_sequence, to_sequence)
        rows = self.rows(from_sequence, to_sequence)
        if not rows:
            return None
        if from_sequence is None:
            return verify_chain(cast(Sequence[AuditChainRow], rows))

        predecessor = self.session.scalar(
            select(AuditEvent)
            .where(AuditEvent.sequence < from_sequence)
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
        if predecessor is None:
            return verify_chain(cast(Sequence[AuditChainRow], rows), from_sequence)
        context_rows = (predecessor, *rows)
        # `from_sequence` must still be passed through here: without it, the
        # module-level `verify_chain` treats `predecessor` as row one of the
        # whole chain and misreports "the first row has a previous hash"
        # instead of skipping that genesis check and treating `predecessor`
        # as context for the requested range, per `chain.verify_chain`'s own
        # documented contract for a bounded range.
        return verify_chain(cast(Sequence[AuditChainRow], context_rows), from_sequence)

    def mutation_privileges(self) -> frozenset[str]:
        """Return direct mutation privileges granted to the current role.

        PostgreSQL deployments run the application under a role that is
        separate from the migration owner.  This check gives startup and
        integration diagnostics a concrete assertion for that deployment
        contract.  SQLite has no grant catalogue, so its result is empty.
        """

        if self.session.get_bind().dialect.name != "postgresql":
            return frozenset()
        statement: TextClause = text(
            """
            SELECT privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = current_user
              AND table_schema = current_schema()
              AND table_name = :table_name
              AND privilege_type IN (:privilege_one, :privilege_two)
            """
        )
        result = self.session.execute(
            statement,
            {
                "table_name": "audit_event",
                "privilege_one": "UPDATE",
                "privilege_two": "DELETE",
            },
        )
        return frozenset(str(value) for value in result.scalars())

    def assert_application_role_is_append_only(self) -> None:
        """Raise when the connected PostgreSQL role has direct mutation grants."""

        privileges = self.mutation_privileges()
        if privileges:
            rendered = ", ".join(sorted(privileges))
            raise PermissionError(
                f"The application role has forbidden audit_event privileges: {rendered}."
            )

    def _serialise_writer(self) -> None:
        bind = self.session.get_bind()
        dialect_name = bind.dialect.name
        if dialect_name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _POSTGRES_ADVISORY_LOCK_KEY},
            )
            return
        if dialect_name == "sqlite":
            connection = self.session.connection()
            if not connection.in_transaction():
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    def _latest_for_append(self) -> AuditEvent | None:
        statement: Select[tuple[AuditEvent]] = (
            select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)
        )
        if self.session.get_bind().dialect.name != "sqlite":
            statement = statement.with_for_update()
        return self.session.execute(statement).scalars().first()


class SqlAlchemyAuditStore(AuditRepository):
    """Descriptive alias for dependency-injection configuration."""


AuditEventRepository = AuditRepository


def _digest(
    record: AuditRecord,
    sequence: int,
    previous_hash: str | None,
    payload: dict[str, object],
) -> str:
    return compute_event_hash(
        sequence,
        record.occurred_at,
        record.actor_id if record.actor_id is not None else record.actor_label,
        record.event_type,
        record.subject_type,
        record.subject_id,
        payload,
        previous_hash,
    )


def _validate_sequence(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"Audit {field} must be a positive integer.")


def _validate_range(from_sequence: int | None, to_sequence: int | None) -> None:
    if from_sequence is not None:
        _validate_sequence(from_sequence, "from_sequence")
    if to_sequence is not None:
        _validate_sequence(to_sequence, "to_sequence")
    if from_sequence is not None and to_sequence is not None and from_sequence > to_sequence:
        raise ValueError("Audit from_sequence cannot be greater than to_sequence.")


__all__ = [
    "AuditChainError",
    "AuditEventRepository",
    "AuditRepository",
    "SqlAlchemyAuditStore",
]
