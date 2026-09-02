"""Application service for deterministic covenant tests (`T-034`).

The service is the adapter boundary around the pure evaluator.  It resolves
the scoped live version and its approved exception/waiver records, computes a
ratio from normalized statement lines, persists the immutable test fact, and
emits the audit event in the caller's transaction.  It never commits; the
unit-of-work that owns the session does that once the whole use case is
complete.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantException,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
    CovenantWaiver,
)
from covenant_radar.db.models.facility import FacilityConduct
from covenant_radar.db.repositories.covenant import CovenantRepository, CovenantVersionRepository
from covenant_radar.db.repositories.facility import FacilityRepository
from covenant_radar.db.repositories.trace import TraceRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.domain.covenants.calendar import RetestTrigger, RetestTriggerKind, ScheduleState
from covenant_radar.domain.covenants.evaluate import (
    CovenantEvaluation,
    CovenantVersionFacts,
    PeriodFacts,
    Thresholds,
    evaluate_covenant,
)
from covenant_radar.domain.covenants.exceptions import resolve_exception, resolve_waiver
from covenant_radar.domain.covenants.sma import (
    BorrowerSmaDerivation,
    derive_borrower_sma,
)
from covenant_radar.domain.ratios.compute import FacilityFacts, RatioResult, compute_ratio
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.ratios.reasons import NotComputableReason
from covenant_radar.domain.trace import TraceRecord, stage_record
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize

_ENGINE_RULE_VERSION = "covenant.engine.v1"
_SMA_RULE_VERSION = "covenant.sma.v1"

#: `RatioDefinition.required_lines` names that identify a formula reading
#: `FacilityFacts` rather than statement lines (`domain/ratios/definitions.py`'s
#: `utilisation` and `drawing_power_headroom`) — the only covenant versions a
#: `conduct` retest trigger (`T-035`) can possibly affect, since every other
#: definition never reads facility conduct at all.
_FACILITY_CONDUCT_FIELDS: Final[frozenset[str]] = frozenset(
    {"sanctioned_limit", "outstanding", "drawing_power"}
)


def _reads_facility_conduct(definition_ref: str | None) -> bool:
    """Whether a covenant version's ratio definition reads facility conduct."""
    if definition_ref is None:
        return False
    definition = LIBRARY.get(definition_ref)
    if definition is None:
        return False
    return any(line in _FACILITY_CONDUCT_FIELDS for line in definition.required_lines)


class AuditWriter(Protocol):
    """The append-only audit boundary supplied by the caller."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the current transaction."""


class EngineService:
    """Load, evaluate and persist one scoped covenant test."""

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
    ) -> None:
        if not isinstance(session, Session):
            raise TypeError("EngineService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("EngineService requires an append-only audit writer.")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("EngineService scope_resolver must be callable.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 40:
            raise ValueError("Engine request_id must be between 1 and 40 characters.")
        self.scope_resolver = scope_resolver
        self.covenants = CovenantRepository(session, audit=audit)
        self.versions = CovenantVersionRepository(session, audit=audit)
        self.facilities = FacilityRepository(session, audit=audit)
        self.traces = TraceRepository(session, clock=self.clock, request_id=self.request_id)

    def test(
        self,
        principal: Principal,
        covenant: str | UUID | None = None,
        *,
        period: PeriodFacts | Mapping[str, object] | object,
        lines: Mapping[str, Decimal] | None = None,
        facility: FacilityFacts | None = None,
        ratio: RatioResult | None = None,
        exception: object | None = None,
        waiver: object | None = None,
        scope: Scope | None = None,
        as_of_date: date | None = None,
        covenant_reference: str | None = None,
        covenant_version_id: UUID | None = None,
        period_id: UUID | None = None,
        job_run_id: UUID | None = None,
        conduct: Mapping[object, object] | Sequence[object] | None = None,
        sma_facility_ids: Sequence[str | UUID] | None = None,
    ) -> CovenantTest:
        """Test one live covenant and return its newly persisted test row.

        ``covenant`` may be a stable covenant reference/id.  A version id can
        be supplied explicitly for a historical or batch invocation.  The
        method accepts an already-computed ``ratio`` for callers that have a
        custom ratio adapter; normal callers provide ``lines`` and the
        built-in pure library is used.  The same transaction also derives and
        traces the borrower's SMA band.  Conduct may be supplied by an
        ingestion caller; when omitted, the service reads the effective,
        in-scope facility conduct snapshot for the test date and records
        missing rows explicitly.
        """

        principal, resolved_scope = self._read_context(principal, scope)
        validated_as_of_date = _optional_date(as_of_date, "as_of_date")
        validated_period_id = _optional_uuid(period_id, "period_id")
        validated_job_run_id = _optional_uuid(job_run_id, "job_run_id")
        if ratio is not None and not isinstance(ratio, RatioResult):
            raise ValidationError("ratio must be a RatioResult.", field="ratio")
        if facility is not None and not isinstance(facility, FacilityFacts):
            raise ValidationError("facility must be FacilityFacts or null.", field="facility")
        identifier = covenant_reference if covenant_reference is not None else covenant
        version, covenant_row = self._load_version(
            identifier,
            covenant_version_id=covenant_version_id,
            as_of=validated_as_of_date or _period_date(period) or self._now().date(),
            scope=resolved_scope,
        )
        test_period = _coerce_period(
            period, as_of_date=validated_as_of_date, period_id=validated_period_id
        )
        test_date = _period_date(test_period)
        if test_date is None:
            raise ValidationError(
                "period.as_of_date or period.period_end is required to persist a covenant test.",
                field="period.as_of_date",
            )
        if not isinstance(test_period, PeriodFacts):
            raise TypeError("period could not be normalized to PeriodFacts.")

        resolved_facility = facility or self._facility_facts(
            covenant_row, test_date, resolved_scope
        )
        validated_lines = _validate_lines(lines) if lines is not None else {}
        ratio_result = ratio or self._compute_ratio(
            version,
            validated_lines,
            resolved_facility,
            period_complete=test_period.is_complete,
        )

        active_exception = exception
        if active_exception is None and test_period.period_label is not None:
            exception_rows = self._exceptions(version.id, resolved_scope)
            active_exception = resolve_exception(
                {"id": version.id, "exceptions": exception_rows}, test_period.period_label
            )

        active_waiver = waiver
        if active_waiver is None:
            waiver_rows = self._waivers(covenant_row.id, resolved_scope)
            active_waiver = resolve_waiver(
                {"id": covenant_row.id, "waivers": waiver_rows}, test_date
            )

        evaluation = evaluate_covenant(
            CovenantVersionFacts(
                id=version.id,
                covenant_id=version.covenant_id,
                version_no=version.version_no,
                threshold=version.threshold,
                direction=version.direction,
                warning_headroom_pct=version.warning_headroom_pct,
                cure_days=version.cure_days,
            ),
            ratio_result,
            test_period,
            active_exception,
            active_waiver,
            Thresholds(as_of_date=test_date),
        )

        now = self._now()
        row = CovenantTest(
            id=new_id(),
            covenant_version_id=version.id,
            period_id=test_period.period_id,
            as_of_date=test_date,
            value=evaluation.value,
            threshold_used=evaluation.threshold_used,
            headroom_pct=evaluation.headroom_pct,
            verdict=evaluation.verdict,
            exception_id=_record_id(evaluation.exception_applied),
            waiver_id=_record_id(evaluation.waiver_applied),
            cure_ends_on=evaluation.cure_ends_on,
            inputs=_test_inputs(test_period, ratio_result, version, evaluation),
            not_computable_reason=(evaluation.reason.value if evaluation.reason else None),
            computed_at=now,
            job_run_id=validated_job_run_id,
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=self.request_id,
        )
        self.session.add(row)

        # A pending schedule occurrence for this exact due date is resolved
        # by this test — the calendar (`T-035`) and the engine (`T-034`)
        # meet here. A trigger's fresh retest (`queue_retest`) always opens
        # a new `due` row rather than reusing a `tested` one, so this look-up
        # can never re-link a test to a period that already has a settled
        # result.
        linked_schedule = self._pending_schedule(version.id, test_date)
        if linked_schedule is not None:
            linked_schedule.state = ScheduleState.TESTED.value
            linked_schedule.test_id = row.id
            linked_schedule.updated_at = now
            linked_schedule.updated_by_id = principal.id
            linked_schedule.version += 1

        # The flag is bookkeeping, not a covenant-term amendment.  It is
        # intentionally set in the same transaction as the test so a failed
        # insert/audit write cannot leave a version claiming it was tested.
        version.tested_at_least_once = True
        version.updated_at = now
        version.updated_by_id = principal.id
        version.version += 1
        self.session.flush()

        audit_payload = _audit_payload(row, evaluation, version)
        if linked_schedule is not None:
            audit_payload["schedule_id"] = str(linked_schedule.id)
        self.audit.record(
            AuditEventType.COVENANT_TESTED.value,
            ("covenant_test", row.id),
            audit_payload,
            actor=principal.id,
            request_id=self.request_id,
        )
        self.traces.write(
            ("covenant_test", row.id),
            _stage2_trace(evaluation, ratio_result, test_period, version, test_date),
            actor_id=principal.id,
            request_id=self.request_id,
            occurred_at=now,
        )
        facility_row = self.facilities.get(covenant_row.facility_id, scope=resolved_scope)
        if facility_row is None:
            raise NotFound("The covenant facility was not found within the current scope.")
        borrower_id = _validate_identifier(facility_row.borrower_id, "borrower_id")
        if not isinstance(borrower_id, UUID):
            raise ValidationError("borrower_id must be a UUID.", field="borrower_id")
        self._derive_sma(
            principal,
            borrower_id,
            as_of_date=test_date,
            conduct=conduct,
            facility_ids=sma_facility_ids,
            scope=resolved_scope,
        )
        return row

    def test_covenant(self, *args: object, **kwargs: object) -> CovenantTest:
        """Explicitly named alias for callers that avoid ``test``."""

        return self.test(*args, **kwargs)  # type: ignore[arg-type]

    def evaluate(self, *args: object, **kwargs: object) -> CovenantTest:
        """Compatibility alias for the service use case."""

        return self.test(*args, **kwargs)  # type: ignore[arg-type]

    def queue_retest(
        self,
        principal: Principal,
        trigger: RetestTrigger,
        *,
        scope: Scope | None = None,
    ) -> tuple[CovenantSchedule, ...]:
        """Queue an idempotent retest for every live covenant version
        ``trigger`` affects (`T-035`, `spec §R-08`).

        Dependency detection resolves ``trigger`` to affected versions per
        `RetestTrigger`'s own documentation: a statement/restatement reaches
        every live version across the borrower's current facilities, a
        conduct change reaches only versions whose ratio definition reads
        facility conduct on that one facility, a waiver reaches the
        covenant's live version(s), and an exception reaches its own
        version.

        Queueing is idempotent per ``(covenant_version_id, due_date)``: a
        version that already has a pending (``due``) schedule row for the
        same date is returned unchanged rather than duplicated. An
        already-``tested`` row for that same date is never reused or
        touched — the prior test stays exactly as it was, and a fresh
        ``due`` row is queued alongside it, so a restatement of an
        already-tested period leaves both visible.
        """
        principal, resolved_scope = self._read_context(principal, scope)
        if not isinstance(trigger, RetestTrigger):
            raise ValidationError("trigger must be a RetestTrigger.", field="trigger")
        versions = self._versions_for_trigger(trigger, resolved_scope)
        now = self._now()
        return tuple(
            self._queue_one_retest(version, trigger, principal, now) for version in versions
        )

    def _versions_for_trigger(
        self, trigger: RetestTrigger, scope: Scope
    ) -> tuple[CovenantVersion, ...]:
        # `RetestTrigger.__post_init__` already refuses a trigger whose
        # `kind`-appropriate scope field is `None`, so each branch's `assert`
        # below only narrows the type for static checking; it can never fail.
        if trigger.kind is RetestTriggerKind.EXCEPTION:
            assert trigger.covenant_version_id is not None
            version = self.versions.get(trigger.covenant_version_id, scope=scope)
            if version is None or version.status != "live":
                return ()
            return (version,)
        if trigger.kind is RetestTriggerKind.WAIVER:
            assert trigger.covenant_id is not None
            covenant = self.covenants.get(trigger.covenant_id, scope=scope)
            if covenant is None:
                return ()
            return tuple(
                version
                for version in self.versions.for_covenant(covenant.id, scope=scope)
                if version.status == "live"
            )
        if trigger.kind is RetestTriggerKind.CONDUCT:
            assert trigger.facility_id is not None
            candidates = self.versions.live_at(trigger.facility_id, trigger.as_of_date, scope=scope)
            return tuple(
                version
                for version in candidates
                if version.status == "live" and _reads_facility_conduct(version.definition_ref)
            )
        # STATEMENT / RESTATEMENT: any live covenant across the borrower's
        # current facilities may depend on the changed period.
        assert trigger.borrower_id is not None
        facilities = self.facilities.live_for_borrower(trigger.borrower_id, scope=scope)
        versions: list[CovenantVersion] = []
        for facility in facilities:
            versions.extend(
                version
                for version in self.versions.live_at(facility.id, trigger.as_of_date, scope=scope)
                if version.status == "live"
            )
        return tuple(versions)

    def _queue_one_retest(
        self,
        version: CovenantVersion,
        trigger: RetestTrigger,
        principal: Principal,
        now: datetime,
    ) -> CovenantSchedule:
        existing = self._pending_schedule(version.id, trigger.as_of_date)
        if existing is not None:
            return existing
        row = CovenantSchedule(
            id=new_id(),
            covenant_version_id=version.id,
            due_date=trigger.as_of_date,
            state=ScheduleState.DUE.value,
            test_id=None,
            certificate_id=None,
            created_at=now,
            updated_at=now,
            created_by_id=principal.id,
            updated_by_id=principal.id,
            request_id=self.request_id,
        )
        self.session.add(row)
        self.session.flush()
        self.audit.record(
            AuditEventType.COVENANT_RETEST_QUEUED.value,
            ("covenant_schedule", row.id),
            {
                "covenant_version_id": str(version.id),
                "due_date": row.due_date.isoformat(),
                "trigger_kind": trigger.kind.value,
                "period_label": trigger.period_label,
            },
            actor=principal.id,
            request_id=self.request_id,
        )
        return row

    def _pending_schedule(self, version_id: UUID, due_date: date) -> CovenantSchedule | None:
        """Return the one pending (`due`) schedule row for a covenant
        version already resolved in scope by the caller.

        No further scope predicate is applied here: every caller — `test`
        and `_queue_one_retest` — has already loaded `version_id`'s owning
        `CovenantVersion` through a scoped repository call, so re-deriving
        the same ownership join a second time would only repeat a check
        that has already passed.
        """
        statement = select(CovenantSchedule).where(
            CovenantSchedule.covenant_version_id == version_id,
            CovenantSchedule.due_date == due_date,
            CovenantSchedule.state == ScheduleState.DUE.value,
        )
        return self.session.execute(statement).scalars().one_or_none()

    def _derive_sma(
        self,
        principal: Principal,
        borrower_id: UUID,
        *,
        as_of_date: date,
        conduct: Mapping[object, object] | Sequence[object] | None,
        facility_ids: Sequence[str | UUID] | None = None,
        scope: Scope | None = None,
    ) -> BorrowerSmaDerivation:
        """Derive a scoped borrower's SMA band and append its trace row.

        Conduct is supplied by the ingestion/repository boundary as either
        facility-keyed rows or a sequence of rows.  Keeping retrieval outside
        this service avoids coupling the pure banding rule to a particular
        persistence model while still making the derivation auditable at the
        same stage as covenant testing.  ``facility_ids`` is important on a
        day with no rows: each expected facility is recorded as missing data,
        never as current.
        """

        principal, resolved_scope = self._read_context(principal, scope)
        validated_borrower_id = _validate_identifier(borrower_id, "borrower_id")
        if not isinstance(validated_borrower_id, UUID):
            raise ValidationError(
                "borrower_id must be a UUID.",
                field="borrower_id",
            )
        validated_as_of_date = _optional_date(as_of_date, "as_of_date")
        if validated_as_of_date is None:
            raise ValidationError(
                "as_of_date is required to derive an SMA band.", field="as_of_date"
            )
        if facility_ids is not None and isinstance(facility_ids, str | bytes | bytearray):
            raise ValidationError(
                "facility_ids must be a sequence of facility identifiers.", field="facility_ids"
            )

        effective_conduct = conduct
        effective_facility_ids = facility_ids
        if conduct is None and facility_ids is None:
            effective_facility_ids, effective_conduct = self._sma_conduct_snapshot(
                validated_borrower_id,
                validated_as_of_date,
                resolved_scope,
            )

        derivation = derive_borrower_sma(
            effective_conduct,
            borrower_id=validated_borrower_id,
            as_of_date=validated_as_of_date,
            facility_ids=effective_facility_ids,
        )
        now = self._now()
        self.traces.write(
            ("borrower_sma", validated_borrower_id),
            _sma_trace(derivation),
            actor_id=principal.id,
            request_id=self.request_id,
            occurred_at=now,
        )
        return derivation

    def _sma_conduct_snapshot(
        self,
        borrower_id: UUID,
        as_of_date: date,
        scope: Scope,
    ) -> tuple[tuple[UUID, ...], Mapping[object, object]]:
        """Read the effective, scoped conduct snapshot for one borrower.

        Facility versions are effective-dated, so selecting only current rows
        would produce the wrong answer for a historical test.  The conduct
        query repeats the portfolio predicate through its ownership path;
        scope is enforced in SQL rather than by filtering an unscoped result.
        """

        facilities = self.facilities.for_borrower(
            borrower_id,
            scope=scope,
            current_only=False,
        )
        effective_facilities = tuple(
            facility
            for facility in facilities
            if facility.effective_from <= as_of_date
            and (facility.effective_to is None or as_of_date < facility.effective_to)
        )
        facility_ids = tuple(facility.id for facility in effective_facilities)
        if not facility_ids:
            return (), {}

        ownership = ownership_path_for(FacilityConduct)
        statement = ownership.apply(select(FacilityConduct)).where(
            scope.predicate(ownership.path_column),
            FacilityConduct.facility_id.in_(facility_ids),
            FacilityConduct.as_of_date == as_of_date,
        )
        rows = tuple(
            self.session.execute(
                statement.order_by(FacilityConduct.facility_id, FacilityConduct.id)
            )
            .scalars()
            .all()
        )
        conduct_by_facility: dict[object, object] = {row.facility_id: row for row in rows}
        return facility_ids, conduct_by_facility

    def _load_version(
        self,
        identifier: str | UUID | None,
        *,
        covenant_version_id: UUID | None,
        as_of: date,
        scope: Scope,
    ) -> tuple[CovenantVersion, Covenant]:
        if covenant_version_id is not None:
            if not isinstance(covenant_version_id, UUID):
                raise ValidationError(
                    "covenant_version_id must be a UUID.", field="covenant_version_id"
                )
            version = self.versions.get(covenant_version_id, scope=scope)
            if version is None:
                raise NotFound("The covenant version was not found within the current scope.")
            covenant_row = self.covenants.get(version.covenant_id, scope=scope)
            if covenant_row is None:
                raise NotFound("The covenant was not found within the current scope.")
        else:
            if identifier is None:
                raise ValidationError(
                    "A covenant reference or covenant_version_id is required.",
                    field="covenant_reference",
                )
            version_by_id = (
                self.versions.get(identifier, scope=scope) if isinstance(identifier, UUID) else None
            )
            if version_by_id is not None:
                version = version_by_id
                covenant_row = self.covenants.get(version.covenant_id, scope=scope)
                if covenant_row is None:
                    raise NotFound("The covenant was not found within the current scope.")
                if not covenant_row.is_active or version.status != "live":
                    raise ValidationError(
                        "Only a live covenant version can be tested.", field="covenant"
                    )
                if not _version_effective(version, as_of):
                    raise NotFound("The covenant version was not effective on the test date.")
                return version, covenant_row

            covenant_row = self._covenant(identifier, scope)
            if covenant_row is None:
                raise NotFound("The covenant was not found within the current scope.")
            candidates = self.versions.for_covenant(covenant_row.id, scope=scope)
            matching = tuple(
                candidate
                for candidate in candidates
                if candidate.status == "live"
                and candidate.effective_from <= as_of
                and (candidate.effective_to is None or as_of < candidate.effective_to)
            )
            if len(matching) != 1:
                raise NotFound(
                    f"No single live covenant version was effective for {covenant_row.reference!r} "
                    f"on {as_of}."
                )
            version = matching[0]

        if not covenant_row.is_active or version.status != "live":
            raise ValidationError("Only a live covenant version can be tested.", field="covenant")
        if version.covenant_id != covenant_row.id:
            raise NotFound("The covenant version was not found within the current scope.")
        if not _version_effective(version, as_of):
            raise NotFound("The covenant version was not effective on the test date.")
        return version, covenant_row

    def _covenant(self, identifier: str | UUID, scope: Scope) -> Covenant | None:
        if isinstance(identifier, UUID):
            by_id = self.covenants.get(identifier, scope=scope)
            if by_id is not None:
                return by_id
            return None
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValidationError("covenant must be text or a UUID.", field="covenant_reference")
        return self.covenants.by_reference(identifier.strip(), scope=scope)

    def _compute_ratio(
        self,
        version: CovenantVersion,
        lines: Mapping[str, Decimal],
        facility: FacilityFacts | None,
        *,
        period_complete: bool,
    ) -> RatioResult:
        if version.definition_ref is None:
            return RatioResult(
                code="custom_formula",
                value=None,
                computable=False,
                reason=NotComputableReason.FORMULA_NOT_COMPUTABLE,
                inputs_used=dict(lines),
                band_breached=False,
                reason_context={"reason": "custom formula evaluation is not registered"},
            )
        definition = LIBRARY.get(version.definition_ref)
        if definition is None:
            return RatioResult(
                code=version.definition_ref,
                value=None,
                computable=False,
                reason=NotComputableReason.FORMULA_NOT_COMPUTABLE,
                inputs_used=dict(lines),
                band_breached=False,
                reason_context={"reason": "ratio definition is not registered"},
            )
        return compute_ratio(definition, lines, facility, period_complete=period_complete)

    def _facility_facts(
        self, covenant: Covenant, as_of: date, scope: Scope
    ) -> FacilityFacts | None:
        facility = self.facilities.get(covenant.facility_id, scope=scope)
        if facility is None:
            raise NotFound("The covenant facility was not found within the current scope.")
        if facility.effective_from > as_of or (
            facility.effective_to is not None and as_of >= facility.effective_to
        ):
            raise NotFound("The covenant facility was not effective on the test date.")
        return FacilityFacts(
            sanctioned_limit=facility.sanctioned_limit,
            outstanding=facility.outstanding,
            drawing_power=facility.drawing_power,
        )

    def _exceptions(self, version_id: UUID, scope: Scope) -> tuple[CovenantException, ...]:
        ownership = ownership_path_for(CovenantException)
        statement = ownership.apply(select(CovenantException)).where(
            scope.predicate(ownership.path_column),
            CovenantException.covenant_version_id == version_id,
        )
        return tuple(self.session.execute(statement).scalars().all())

    def _waivers(self, covenant_id: UUID, scope: Scope) -> tuple[CovenantWaiver, ...]:
        ownership = ownership_path_for(CovenantWaiver)
        statement = ownership.apply(select(CovenantWaiver)).where(
            scope.predicate(ownership.path_column), CovenantWaiver.covenant_id == covenant_id
        )
        return tuple(self.session.execute(statement).scalars().all())

    def _read_context(self, principal: Principal, scope: Scope | None) -> tuple[Principal, Scope]:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.VIEW_COVENANT)
        if scope is None:
            resolved = (
                self.scope_resolver(principal)
                if self.scope_resolver is not None
                else resolve_scope(principal, self.session)
            )
        else:
            resolved = scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The resolved scope does not belong to the authenticated principal."
            )
        return principal, resolved

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Engine clock must return an aware datetime.")
        return now.astimezone(UTC)


def _coerce_period(
    period: PeriodFacts | Mapping[str, object] | object,
    *,
    as_of_date: date | None,
    period_id: UUID | None,
) -> PeriodFacts:
    if isinstance(period, date) and not isinstance(period, datetime):
        return PeriodFacts(period_id=period_id, as_of_date=as_of_date or period)
    if isinstance(period, str):
        return PeriodFacts(period_label=period, period_id=period_id, as_of_date=as_of_date)
    if isinstance(period, PeriodFacts):
        if as_of_date is None and period_id is None:
            return period
        return PeriodFacts(
            period_label=period.period_label,
            is_complete=period.is_complete,
            last_complete_period=period.last_complete_period,
            period_id=period_id or period.period_id,
            as_of_date=as_of_date or period.as_of_date,
            period_end=period.period_end,
        )
    get = period.get if isinstance(period, Mapping) else lambda name: getattr(period, name, None)
    label = get("period_label") or get("fy_label") or get("period")
    complete = get("is_complete")
    last = get("last_complete_period") or get("last_available_period")
    supplied_period_id = get("period_id")
    supplied_as_of = get("as_of_date")
    if supplied_as_of is None:
        supplied_as_of = get("test_date")
    if supplied_as_of is None:
        supplied_as_of = get("as_of")
    period_end = get("period_end")
    if complete is None:
        complete = True
    return PeriodFacts(
        period_label=label if isinstance(label, str) else None,
        is_complete=_required_bool(complete, "period.is_complete"),
        last_complete_period=last if isinstance(last, str) else None,
        period_id=period_id or _optional_uuid(supplied_period_id, "period.period_id"),
        as_of_date=as_of_date or _optional_date(supplied_as_of, "period.as_of_date"),
        period_end=_optional_date(period_end, "period.period_end"),
    )


def _required_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be a boolean.", field=field)
    return value


def _optional_uuid(value: object, field: str) -> UUID | None:
    if value is not None and not isinstance(value, UUID):
        raise ValidationError(f"{field} must be a UUID or null.", field=field)
    return value


def _optional_date(value: object, field: str) -> date | None:
    if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
        raise ValidationError(f"{field} must be a calendar date or null.", field=field)
    return value


def _validate_identifier(value: object, field: str) -> str | UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValidationError(
        f"{field} must be a non-empty string or UUID.",
        field=field,
    )


def _period_date(period: object) -> date | None:
    if isinstance(period, date) and not isinstance(period, datetime):
        return period
    for name in ("as_of_date", "period_end", "test_date"):
        value = period.get(name) if isinstance(period, Mapping) else getattr(period, name, None)
        if value is None:
            continue
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValidationError(f"period.{name} must be a calendar date.", field=f"period.{name}")
        return value
    return None


def _version_effective(version: CovenantVersion, as_of: date) -> bool:
    return version.effective_from <= as_of and (
        version.effective_to is None or as_of < version.effective_to
    )


def _validate_lines(lines: Mapping[str, Decimal]) -> dict[str, Decimal]:
    if not isinstance(lines, Mapping):
        raise ValidationError(
            "lines must be a mapping of statement codes to Decimal values.", field="lines"
        )
    validated: dict[str, Decimal] = {}
    for key, value in lines.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(
                "Every statement line name must be non-empty text.", field="lines"
            )
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValidationError(
                f"Statement line {key!r} must be a finite Decimal.", field=f"lines.{key}"
            )
        normalized_key = key.strip()
        if normalized_key in validated:
            raise ValidationError(
                f"Statement line {normalized_key!r} was supplied more than once.",
                field=f"lines.{normalized_key}",
            )
        validated[normalized_key] = value
    return validated


def _record_id(record: object | None) -> UUID | None:
    value = record.get("id") if isinstance(record, Mapping) else getattr(record, "id", None)
    return value if isinstance(value, UUID) else None


def _json_decimal(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError("A persisted numeric value must be a Decimal or None.")
    return str(value)


def _test_inputs(
    period: PeriodFacts,
    ratio: RatioResult,
    version: CovenantVersion,
    evaluation: CovenantEvaluation,
) -> dict[str, object]:
    inputs_used = {str(key): _json_decimal(value) for key, value in ratio.inputs_used.items()}
    return {
        "definition_ref": version.definition_ref,
        "period_label": period.period_label,
        "period_complete": period.is_complete,
        "inputs_used": inputs_used,
        "ratio_reason": ratio.reason.value if ratio.reason else None,
        "reason_context": dict(ratio.reason_context),
        "stale_reason": evaluation.stale_reason,
        "thresholds_compared": [
            {
                "name": str(item["name"]),
                "value": _json_decimal(item["value"]),
                "observed": _json_decimal(item["observed"]),
                "side": str(item["side"]),
            }
            for item in evaluation.thresholds_compared
        ],
    }


def _audit_payload(
    row: CovenantTest, evaluation: CovenantEvaluation, version: CovenantVersion
) -> dict[str, object]:
    return {
        "covenant_version_id": str(version.id),
        "version_no": version.version_no,
        "as_of_date": row.as_of_date.isoformat(),
        "value": _json_decimal(row.value),
        "threshold_used": _json_decimal(row.threshold_used),
        "headroom_pct": _json_decimal(row.headroom_pct),
        "verdict": row.verdict,
        "exception_id": str(row.exception_id) if row.exception_id else None,
        "waiver_id": str(row.waiver_id) if row.waiver_id else None,
        "cure_ends_on": row.cure_ends_on.isoformat() if row.cure_ends_on else None,
        "not_computable_reason": row.not_computable_reason,
        "thresholds_compared": [
            {
                "name": str(item["name"]),
                "value": _json_decimal(item["value"]),
                "observed": _json_decimal(item["observed"]),
                "side": str(item["side"]),
            }
            for item in evaluation.thresholds_compared
        ],
    }


def _stage2_trace(
    evaluation: CovenantEvaluation,
    ratio: RatioResult,
    period: PeriodFacts,
    version: CovenantVersion,
    test_date: date,
) -> TraceRecord:
    """Build the explainability record for one deterministic covenant test."""

    exception_id = _record_id(evaluation.exception_applied)
    waiver_id = _record_id(evaluation.waiver_applied)
    inputs: dict[str, object] = {
        "covenant_version_id": str(version.id),
        "version_no": version.version_no,
        "definition_ref": version.definition_ref,
        "direction": version.direction,
        "period_id": str(period.period_id) if period.period_id else None,
        "period_label": period.period_label,
        "as_of_date": test_date.isoformat(),
        "ratio_code": ratio.code,
        "ratio_computable": ratio.computable,
        "ratio_value": _json_decimal(ratio.value),
        "ratio_inputs": {
            str(key): _json_decimal(value) for key, value in ratio.inputs_used.items()
        },
        "exception_id": str(exception_id) if exception_id else None,
        "waiver_id": str(waiver_id) if waiver_id else None,
    }
    outputs: dict[str, object] = {
        "value": _json_decimal(evaluation.value),
        "threshold_used": _json_decimal(evaluation.threshold_used),
        "headroom_pct": _json_decimal(evaluation.headroom_pct),
        "verdict": evaluation.verdict,
        "reason": evaluation.reason.value if evaluation.reason else None,
        "reason_context": dict(evaluation.reason_context),
        "cure_ends_on": evaluation.cure_ends_on.isoformat() if evaluation.cure_ends_on else None,
        "stale_reason": evaluation.stale_reason,
        "exception_applied": bool(evaluation.exception_applied),
        "waiver_applied": bool(evaluation.waiver_applied),
    }
    thresholds = [
        {
            "name": str(item["name"]),
            "value": _json_decimal(item["value"]),
            "observed": _json_decimal(item["observed"]),
            "side": str(item["side"]),
        }
        for item in evaluation.thresholds_compared
    ]
    sources: list[object] = [{"type": "covenant_version", "id": str(version.id)}]
    if period.period_id is not None:
        sources.append({"type": "financial_period", "id": str(period.period_id)})
    return stage_record(
        2,
        "code",
        inputs,
        outputs,
        _ENGINE_RULE_VERSION,
        thresholds,
        Decimal("1"),
        sources,
    )


def _sma_trace(derivation: BorrowerSmaDerivation) -> TraceRecord:
    """Build the stage-2 trace for a borrower SMA derivation."""

    facility_inputs = [
        {
            "facility_id": str(item.facility_id),
            "as_of_date": item.as_of_date.isoformat(),
            "days_past_due": item.days_past_due,
            "source_id": str(item.source_id) if item.source_id is not None else None,
        }
        for item in derivation.facilities
    ]
    facility_bands = [
        {
            "facility_id": str(item.facility_id),
            "as_of_date": item.as_of_date.isoformat(),
            "days_past_due": item.days_past_due,
            "sma_band": item.band.value,
            "reason": item.reason,
            "source_id": str(item.source_id) if item.source_id is not None else None,
        }
        for item in derivation.facilities
    ]
    inputs: dict[str, object] = {
        "borrower_id": str(derivation.borrower_id) if derivation.borrower_id is not None else None,
        "as_of_date": derivation.as_of_date.isoformat(),
        "facility_conduct": facility_inputs,
    }
    worst_facility = derivation.worst_facility
    outputs: dict[str, object] = {
        "sma_band": derivation.band.value,
        "reason": derivation.reason,
        "worst_facility_id": (
            str(worst_facility.facility_id) if worst_facility is not None else None
        ),
        "facility_bands": facility_bands,
    }
    sources = [
        {
            "type": "facility_conduct",
            "facility_id": str(item.facility_id),
            "as_of_date": item.as_of_date.isoformat(),
            "source_id": str(item.source_id) if item.source_id is not None else None,
        }
        for item in derivation.facilities
    ]
    return stage_record(
        2,
        "code",
        inputs,
        outputs,
        _SMA_RULE_VERSION,
        (),
        Decimal("1"),
        sources,
    )


CovenantEngineService = EngineService


__all__ = ["AuditWriter", "CovenantEngineService", "EngineService"]
