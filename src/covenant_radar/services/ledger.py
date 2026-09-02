"""Application service for evidence-ledger revision and reconstruction.

The service is the transaction and authorisation boundary around the pure
supersession rules.  Raw signal events remain immutable; evidence rows are
inserted or revised in one caller-owned transaction, and every state change
gets an append-only transition plus an audit event.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.config.thresholds import ThresholdStore
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, NotFound
from covenant_radar.db.models.signal import SignalEvent as SignalEventModel
from covenant_radar.db.repositories.evidence import (
    EvidenceRepository,
    EvidenceTransitionRepository,
)
from covenant_radar.db.repositories.trace import TraceRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.signals.evidence import (
    EvidenceFacts,
    EvidenceTransitionFacts,
    SignalEventFacts,
)
from covenant_radar.domain.signals.materiality import MaterialityThresholds
from covenant_radar.domain.signals.persistence import PersistenceThresholds
from covenant_radar.domain.signals.supersession import (
    ContradictionRule,
    SupersessionResult,
    resolve_supersession,
)
from covenant_radar.domain.trace import TraceReadRecord, TraceRecord, stage_record
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize

_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_LEDGER_TRACE_RULE_VERSION: Final[str] = "evidence.ledger.v1"


class AuditWriter(Protocol):
    """The append-only audit boundary required by ledger writes."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an audit event in the caller's transaction."""


@dataclass(frozen=True, slots=True)
class LedgerRevision:
    """The durable and reconstructable result of one ledger revision."""

    borrower_id: UUID
    as_of: date
    items: tuple[EvidenceFacts, ...]
    supersessions: tuple[SupersessionResult, ...]

    @property
    def transitions(self) -> tuple[EvidenceTransitionFacts, ...]:
        """Return the transitions written by the revision."""

        return tuple(revision.transition for revision in self.supersessions)

    @property
    def changed(self) -> bool:
        """Whether at least one contradiction changed the risk interpretation."""

        return bool(self.supersessions)


class LedgerService:
    """Authorise, persist and read scoped evidence revisions."""

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        scope_resolver: object | None = None,
        evidence_repository: EvidenceRepository | None = None,
        transition_repository: EvidenceTransitionRepository | None = None,
        trace_repository: TraceRepository | None = None,
        threshold_store: object | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("LedgerService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("LedgerService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("LedgerService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("LedgerService scope_resolver must be callable.")
        if evidence_repository is not None and not isinstance(
            evidence_repository, EvidenceRepository
        ):
            raise TypeError("evidence_repository must be an EvidenceRepository.")
        if transition_repository is not None and not isinstance(
            transition_repository, EvidenceTransitionRepository
        ):
            raise TypeError("transition_repository must be an EvidenceTransitionRepository.")
        if trace_repository is not None and not isinstance(trace_repository, TraceRepository):
            raise TypeError("trace_repository must be a TraceRepository.")
        if threshold_store is not None and not _is_threshold_source(threshold_store):
            raise TypeError("threshold_store must expose get(name) or be a threshold mapping.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = _request_id(request_id or get_request_id() or new_request_id())
        self.scope_resolver = scope_resolver
        self.evidence = evidence_repository or EvidenceRepository(session, audit=audit)
        self.transitions = transition_repository or EvidenceTransitionRepository(session)
        self.traces = trace_repository or TraceRepository(
            session,
            clock=self.clock,
            request_id=self.request_id,
        )
        self.threshold_store = threshold_store if threshold_store is not None else ThresholdStore()

    def revise(
        self,
        principal: Principal,
        borrower_id: UUID,
        events: Iterable[SignalEventFacts | Mapping[str, object] | object] | None = None,
        *,
        as_of: date,
        scope: Scope | None = None,
        rules: Sequence[ContradictionRule] | Mapping[str, object] | None = None,
        thresholds: object | None = None,
        request_id: str | None = None,
    ) -> LedgerRevision:
        """Apply incoming events and persist any supersession they cause.

        ``events`` is the newly arrived batch.  If it is omitted, the method
        reads all persisted signal events for the borrower through ``as_of``;
        this is useful for a deterministic replay.  Duplicate source-event
        ids are ignored by the pure domain layer, making retries safe.
        """

        resolved_scope = self._write_context(principal, scope)
        scoring_date = _calendar_date(as_of, "as_of")
        resolved_request_id = _request_id(request_id or self.request_id)
        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        self._assert_borrower_visible(borrower_id, resolved_scope)
        incoming = (
            self._load_events(borrower_id, scoring_date, resolved_scope)
            if events is None
            else tuple(self._normalise_incoming(events, borrower_id, scoring_date))
        )
        existing_rows = tuple(
            self.evidence.for_borrower(
                borrower_id,
                scope=resolved_scope,
                include_superseded=True,
            )
        )
        existing = tuple(EvidenceFacts.from_item(row) for row in existing_rows)
        batch = resolve_supersession(
            existing,
            incoming,
            as_of=scoring_date,
            rules=rules,
        )
        actor_id = _actor_id(principal)
        now = self._now()
        persisted_ids: set[UUID] = set()
        trace_source = self.threshold_store if thresholds is None else thresholds
        if not _is_threshold_source(trace_source):
            raise TypeError("thresholds must expose get(name) or be a threshold mapping.")

        # Every revision is written as a single repository operation.  This
        # inserts the successor before setting the predecessor's foreign key,
        # locks both sides, and records the transition in the same savepoint.
        with self.session.begin_nested():
            for revision in batch.revisions:
                self.evidence.save_supersession(
                    revision,
                    scope=resolved_scope,
                    occurred_at=now,
                    request_id=resolved_request_id,
                    actor_id=actor_id,
                )
                if revision.superseded.id is not None:
                    persisted_ids.add(revision.superseded.id)
                if revision.successor.id is not None:
                    persisted_ids.add(revision.successor.id)
                self._audit_supersession(revision, principal, resolved_request_id)

            existing_ids = {item.id for item in existing if item.id is not None}
            for item in batch.items:
                if item.id is None or item.id in persisted_ids:
                    continue
                self.evidence.save_score(
                    item,
                    scope=resolved_scope,
                    occurred_at=now,
                    request_id=resolved_request_id,
                    actor_id=actor_id,
                    force_new=item.id not in existing_ids,
                )
                persisted_ids.add(item.id)

            self.traces.write(
                ("borrower", borrower_id),
                _stage3_trace(batch.items, batch.revisions, scoring_date, trace_source),
                actor_id=actor_id,
                request_id=resolved_request_id,
                occurred_at=now,
            )

            self.audit.record(
                AuditEventType.EVIDENCE_LEDGER_REVISED.value,
                ("borrower", borrower_id),
                {
                    "as_of": scoring_date.isoformat(),
                    "incoming_events": len(incoming),
                    "evidence_items": len(batch.items),
                    "supersessions": len(batch.revisions),
                    "superseded_item_ids": [
                        str(revision.superseded.id)
                        for revision in batch.revisions
                        if revision.superseded.id is not None
                    ],
                    "successor_item_ids": [
                        str(revision.successor.id)
                        for revision in batch.revisions
                        if revision.successor.id is not None
                    ],
                },
                actor=principal.id if principal.kind is PrincipalKind.USER else None,
                request_id=resolved_request_id,
            )

        return LedgerRevision(
            borrower_id=borrower_id,
            as_of=scoring_date,
            items=tuple(EvidenceFacts.from_item(item) for item in batch.items),
            supersessions=batch.revisions,
        )

    score = revise
    apply = revise
    process = revise

    def read_as_of(
        self,
        principal: Principal,
        borrower_id: UUID,
        as_of: date,
        *,
        scope: Scope | None = None,
    ) -> tuple[EvidenceFacts, ...]:
        """Read the ledger state exactly as of a historical calendar date."""

        resolved_scope = self._read_context(principal, scope)
        scoring_date = _calendar_date(as_of, "as_of")
        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        self._assert_borrower_visible(borrower_id, resolved_scope)
        return tuple(
            self.evidence.for_borrower_as_of(
                borrower_id,
                scoring_date,
                scope=resolved_scope,
            )
        )

    point_in_time = read_as_of
    reconstruct = read_as_of

    def read_trace(
        self,
        principal: Principal,
        borrower_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> tuple[TraceReadRecord, ...]:
        """Return the latest trace record for each stage for one borrower."""

        resolved_scope = self._read_context(principal, scope)
        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        self._assert_borrower_visible(borrower_id, resolved_scope)
        return self.traces.read(("borrower", borrower_id))

    trace = read_trace

    def _load_events(
        self,
        borrower_id: UUID,
        as_of: date,
        scope: Scope,
    ) -> tuple[SignalEventFacts, ...]:
        statement: Select[tuple[SignalEventModel]] = select(SignalEventModel).where(
            SignalEventModel.borrower_id == borrower_id,
            SignalEventModel.event_date <= as_of,
        )
        # SignalEvent owns a borrower directly, so compose the same scope
        # predicate used by all other scoped evidence reads.
        from covenant_radar.db.models.borrower import Borrower
        from covenant_radar.db.models.portfolio import Portfolio

        statement = statement.join(Borrower, Borrower.id == SignalEventModel.borrower_id).join(
            Portfolio, Portfolio.id == Borrower.portfolio_id
        )
        statement = statement.where(scope.predicate(Portfolio.path)).order_by(
            SignalEventModel.event_date,
            SignalEventModel.id,
        )
        rows = self.session.execute(statement).scalars().all()
        return tuple(SignalEventFacts.from_event(row) for row in rows)

    def _normalise_incoming(
        self,
        events: Iterable[SignalEventFacts | Mapping[str, object] | object],
        borrower_id: UUID,
        as_of: date,
    ) -> tuple[SignalEventFacts, ...]:
        result: list[SignalEventFacts] = []
        for value in events:
            event = (
                value if isinstance(value, SignalEventFacts) else SignalEventFacts.from_event(value)
            )
            if event.borrower_id != borrower_id:
                raise ValueError("Every ledger event must belong to the requested borrower.")
            if event.event_date > as_of:
                continue
            result.append(event)
        return tuple(result)

    def _assert_borrower_visible(self, borrower_id: UUID, scope: Scope) -> None:
        if self.evidence.for_borrower(borrower_id, scope=scope, include_superseded=True):
            return
        from covenant_radar.db.models.borrower import Borrower
        from covenant_radar.db.models.portfolio import Portfolio

        visible = self.session.scalar(
            select(Borrower.id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(Borrower.id == borrower_id, scope.predicate(Portfolio.path))
            .limit(1)
        )
        if visible is None:
            raise NotFound("The borrower was not found within the current portfolio scope.")

    def _write_context(self, principal: Principal, scope: Scope | None) -> Scope:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.INGEST_DATA)
        return self._resolve_scope(principal, scope)

    def _read_context(self, principal: Principal, scope: Scope | None) -> Scope:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.VIEW_EVIDENCE)
        return self._resolve_scope(principal, scope)

    def _resolve_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        resolved = (
            scope
            if scope is not None
            else (
                self.scope_resolver(principal)
                if self.scope_resolver is not None
                else resolve_scope(principal, self.session)
            )
        )
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied portfolio scope does not belong to the principal."
            )
        return resolved

    def _audit_supersession(
        self,
        revision: SupersessionResult,
        principal: Principal,
        request_id: str,
    ) -> None:
        self.audit.record(
            AuditEventType.EVIDENCE_SUPERSEDED.value,
            ("evidence_item", revision.superseded.id),
            {
                "successor_id": str(revision.successor.id),
                "occurred_on": revision.occurred_on.isoformat(),
                "family": revision.superseded.family,
                "superseded_type": revision.superseded.evidence_type,
                "successor_type": revision.successor.evidence_type,
                "rule": revision.rule.rule,
                "from_state": revision.transition.from_state,
                "to_state": revision.transition.to_state,
            },
            actor=principal.id if principal.kind is PrincipalKind.USER else None,
            request_id=request_id,
        )

    def _now(self) -> datetime:
        now = self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Ledger clock must return a timezone-aware datetime.")
        return now.astimezone(UTC)


EvidenceLedgerService = LedgerService


def _stage3_trace(
    items: Sequence[EvidenceFacts],
    revisions: Sequence[SupersessionResult],
    as_of: date,
    threshold_source: object,
) -> TraceRecord:
    """Build the complete explainability record for one ledger run."""

    persistence = PersistenceThresholds.from_store(threshold_source)
    materiality = MaterialityThresholds.from_store(threshold_source)
    item_inputs: list[dict[str, object]] = []
    item_outputs: list[dict[str, object]] = []
    comparisons: list[dict[str, object]] = []
    sources: list[object] = []

    for item in items:
        persistence_days = item.persistence_days
        event_count_window = item.event_count_window
        materiality_pct = item.materiality_pct
        firing_arm, firing_rule = _t3_arm(
            persistence_days,
            event_count_window,
            persistence,
        )
        item_id = str(item.id) if item.id is not None else None
        item_inputs.append(
            {
                "id": item_id,
                "family": item.family,
                "evidence_type": item.evidence_type,
                "first_seen": item.first_seen,
                "last_seen": item.last_seen,
                "persistence_days": persistence_days,
                "event_count_window": event_count_window,
                "materiality_pct": materiality_pct,
                "decay_factor": item.decay_factor,
                "source_event_ids": item.source_event_ids,
            }
        )
        item_outputs.append(
            {
                "id": item_id,
                "state": item.state,
                "counts_toward_pressure": item.counts_toward_pressure,
                "persistence": {
                    "firing_arm": firing_arm,
                    "rule": firing_rule,
                },
                "materiality": {
                    "pct": materiality_pct,
                    "counts_toward_pressure": item.counts_toward_pressure,
                    "available": materiality_pct is not None,
                },
                "decay_factor": item.decay_factor,
                "superseded_by_id": (
                    str(item.superseded_by_id) if item.superseded_by_id is not None else None
                ),
                "supersedes_id": (
                    str(item.supersedes_id) if item.supersedes_id is not None else None
                ),
            }
        )
        comparisons.extend(
            (
                _threshold_comparison(
                    "T3.sustained_days",
                    persistence.sustained_days,
                    persistence_days,
                ),
                _threshold_comparison(
                    "T3.sustained_events",
                    persistence.sustained_events,
                    event_count_window,
                ),
                _threshold_comparison(
                    "T4.headroom_erosion_pct",
                    materiality.threshold_pct,
                    materiality_pct,
                ),
            )
        )
        if item.id is not None:
            sources.append({"type": "evidence_item", "id": str(item.id)})

    supersessions = [
        {
            "superseded_id": str(revision.superseded.id),
            "successor_id": str(revision.successor.id),
            "occurred_on": revision.occurred_on,
            "rule": revision.rule.rule,
            "from_state": revision.transition.from_state,
            "to_state": revision.transition.to_state,
        }
        for revision in revisions
    ]
    snapshot_id = _threshold_snapshot_id(threshold_source)
    inputs: dict[str, object] = {
        "as_of": as_of,
        "thresholds": {
            "T3": {
                "sustained_days": persistence.sustained_days,
                "sustained_events": persistence.sustained_events,
                "event_window_days": persistence.event_window_days,
            },
            "T4": {
                "headroom_erosion_pct": materiality.headroom_erosion_pct,
                "threshold_pct": materiality.threshold_pct,
            },
        },
        "threshold_snapshot_id": snapshot_id,
        "items": item_inputs,
    }
    outputs: dict[str, object] = {
        "as_of": as_of,
        "item_count": len(items),
        "no_evidence": not items,
        "items": item_outputs,
        "supersessions": supersessions,
        "supersession_count": len(supersessions),
    }
    return stage_record(
        3,
        "code",
        inputs,
        outputs,
        _LEDGER_TRACE_RULE_VERSION,
        comparisons,
        Decimal("1"),
        sources,
    )


def _t3_arm(
    persistence_days: int | None,
    event_count_window: int | None,
    thresholds: PersistenceThresholds,
) -> tuple[str | None, str]:
    measured_days = persistence_days or 0
    measured_events = event_count_window or 0
    if measured_days >= thresholds.sustained_days:
        return "sustained_days", "T3.sustained_days"
    if measured_events >= thresholds.sustained_events:
        return "sustained_events", "T3.sustained_events"
    return None, "T3.neither_arm"


def _threshold_comparison(
    name: str,
    threshold: int | Decimal,
    observed: int | Decimal | None,
) -> dict[str, object]:
    measured = observed if observed is not None else 0
    if measured > threshold:
        side = "above"
    elif measured < threshold:
        side = "below"
    else:
        side = "at"
    return {"name": name, "value": threshold, "observed": measured, "side": side}


def _threshold_snapshot_id(source: object) -> str | None:
    candidate = getattr(source, "snapshot_id", None)
    if callable(candidate):
        candidate = candidate()
    elif isinstance(source, Mapping):
        candidate = source.get("threshold_snapshot_id")
    if candidate is None:
        return None
    if isinstance(candidate, UUID):
        return str(candidate)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    raise TypeError("threshold_snapshot_id must be a UUID or non-empty text.")


def _is_threshold_source(value: object) -> bool:
    return isinstance(value, Mapping) or callable(getattr(value, "get", None))


def _actor_id(principal: Principal) -> UUID | None:
    return principal.id if principal.kind is PrincipalKind.USER else None


def _calendar_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a calendar date, not a datetime.")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO calendar date.") from error
    raise TypeError(f"{field_name} must be a calendar date.")


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(f"request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters.")
    if not value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("request_id must be non-blank text without control characters.")
    return value


__all__ = ["AuditWriter", "EvidenceLedgerService", "LedgerRevision", "LedgerService"]
