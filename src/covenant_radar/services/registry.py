"""The covenant registry service: register and amend — `plan.md §5.5`,
`spec §R-05.a`/`R-05.b` (`T-031`).

Coordinates `CovenantRepository`/`CovenantVersionRepository`, the domain
value object (`domain/covenants/model.py`) and the audit port in one
caller-owned transaction, the same shape `MasterDataService`
(`services/master_data.py`) already established: this service never opens
or commits a transaction itself, and every write reaches the database only
after `CovenantVersionTerms` has already validated it.

`register` and `amend` each decide their new version's initial `status` —
`"draft"` when the second-actor control (`maker_checker_enabled`) applies,
`"live"` when it does not — but neither submits anything for approval or
moves a draft toward `live` itself: that workflow, wired through
`security/maker_checker.py`'s `ApprovalService` and exposed on the web and
API surfaces (`C-06`, `C-07`), is `T-033`'s task. `T-032` adds the
exception and waiver paths to this same module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantException,
    CovenantTest,
    CovenantVersion,
    CovenantWaiver,
)
from covenant_radar.db.models.maker_checker import MakerCheckerRequest as MakerCheckerRow
from covenant_radar.db.models.workflow import Notification
from covenant_radar.db.repositories.covenant import CovenantRepository, CovenantVersionRepository
from covenant_radar.db.repositories.facility import FacilityRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.covenants.cure import validate_cure_period
from covenant_radar.domain.covenants.exceptions import (
    validate_exception_window,
    validate_no_overlapping_exceptions,
)
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.security.maker_checker import (
    ApplicationCallbackRegistry,
    MakerCheckerRepository,
    MakerCheckerRequest,
    MakerCheckerSettings,
    MakerCheckerState,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize
from covenant_radar.services.approvals import ApprovalService

_REFERENCE_MAX_LENGTH = 20
_NAME_MAX_LENGTH = 300
_COVENANT_CLASS_MAX_LENGTH = 50
_REASON_MAX_LENGTH = 2_000
_WAIVER_SCOPE_MAX_LENGTH = 100

REGISTER_COVENANT_PERMISSION = Permission.REGISTER_COVENANT
VIEW_COVENANT_PERMISSION = Permission.VIEW_COVENANT
RECORD_WAIVER_PERMISSION = Permission.RECORD_WAIVER

COVENANT_REGISTRATION_OPERATION = "covenant_registration"
COVENANT_AMENDMENT_OPERATION = "covenant_amendment"
COVENANT_WAIVER_OPERATION = "covenant_waiver"
COVENANT_RETIREMENT_OPERATION = "covenant_retirement"

_DRAFT_STATUS = "draft"
_LIVE_STATUS = "live"
_SUPERSEDED_STATUS = "superseded"


class AuditWriter(Protocol):
    """The append-only audit port from contract `C-60`."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's current transaction."""


@dataclass(frozen=True, slots=True)
class RegisteredCovenant:
    """What `register` returns: the new covenant and its first version."""

    covenant: Covenant
    version: CovenantVersion
    approval_request: MakerCheckerRequest | None = None


@dataclass(frozen=True, slots=True)
class AmendedCovenant:
    """What `amend` returns: the version it closed and the one it opened."""

    previous_version: CovenantVersion
    version: CovenantVersion
    approval_request: MakerCheckerRequest | None = None


@dataclass(frozen=True, slots=True)
class RetiredCovenant:
    """The covenant and version affected by a retirement request."""

    covenant: Covenant
    version: CovenantVersion
    approval_request: MakerCheckerRequest | None = None


class RegistryService:
    """Coordinate covenant registration and amendment.

    ``session`` must belong to the current unit of work, the same
    requirement `MasterDataService` documents: keeping the dependency
    explicit makes it impossible for a repository read and its
    corresponding write/audit event to drift into different transactions.
    """

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        maker_checker_enabled: bool = True,
        approval_service: ApprovalService | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("RegistryService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("RegistryService requires an append-only audit writer.")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("RegistryService scope_resolver must be callable.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 40:
            raise ValueError("Registry request_id must be between 1 and 40 characters.")
        self.scope_resolver = scope_resolver
        self.maker_checker_enabled = bool(maker_checker_enabled)
        self.covenants = CovenantRepository(session, audit=audit)
        self.versions = CovenantVersionRepository(session, audit=audit)
        self.facilities = FacilityRepository(session, audit=audit)
        self.approvals = approval_service
        if self.maker_checker_enabled and self.approvals is None:
            self.approvals = _default_approval_service(
                session,
                audit=audit,
                clock=self.clock,
                request_id=self.request_id,
            )
        if self.approvals is not None:
            self._wire_approval_callbacks()

    # ---- use cases -------------------------------------------------------

    def register(
        self,
        principal: Principal,
        *,
        facility_id: UUID,
        reference: str,
        name: str,
        covenant_class: str,
        terms: CovenantVersionTerms,
        scope: Scope | None = None,
    ) -> RegisteredCovenant:
        """Create a covenant and its first version, in draft or live per
        the maker-checker setting. `terms` is already fully validated
        (`CovenantVersionTerms.__post_init__`) before this method ever
        touches the session, so an unknown definition or an out-of-set
        direction, frequency or unit is refused with nothing written."""
        resolved_scope = self._write_context(principal, scope)
        actor_id = self._registering_user_id(principal)
        validate_cure_period(terms.frequency, terms.cure_days)
        facility = self.facilities.get(facility_id, scope=resolved_scope)
        if facility is None:
            raise NotFound(f"Facility {facility_id} was not found within the current scope.")
        reference = _clean_reference(reference, "covenant.reference", maximum=_REFERENCE_MAX_LENGTH)
        name = _required_text(name, "covenant.name", maximum=_NAME_MAX_LENGTH)
        covenant_class = _required_text(
            covenant_class, "covenant.covenant_class", maximum=_COVENANT_CLASS_MAX_LENGTH
        )
        now = self._now()

        covenant = Covenant(
            id=new_id(),
            reference=reference,
            facility_id=facility.id,
            name=name,
            covenant_class=covenant_class,
            is_active=True,
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=self.request_id,
        )
        self.covenants.add(covenant)
        self._flush_or_conflict(f"Covenant reference {reference!r} already exists.")

        version = _new_version_row(
            covenant_id=covenant.id,
            version_no=1,
            terms=terms,
            status=self._initial_status(),
            registered_by_id=actor_id,
            now=now,
            request_id=self.request_id,
            actor_id=actor_id,
        )
        self.versions.add(version)
        self._flush_or_conflict(f"Covenant {reference!r} version 1 conflicts with an existing row.")

        self._audit(
            AuditEventType.COVENANT_REGISTERED.value,
            covenant,
            {
                "action": "registered",
                "reference": covenant.reference,
                "facility_id": str(covenant.facility_id),
                "version_no": version.version_no,
                "status": version.status,
            },
            principal,
        )
        approval_request = self._submit_approval(
            COVENANT_REGISTRATION_OPERATION,
            subject=("covenant_version", version.id),
            payload={
                "covenant_id": str(covenant.id),
                "version_id": str(version.id),
                "reference": covenant.reference,
                "version_no": version.version_no,
            },
            maker=principal,
        )
        return RegisteredCovenant(
            covenant=covenant,
            version=version,
            approval_request=approval_request,
        )

    def amend(
        self,
        principal: Principal,
        reference: str,
        *,
        terms: CovenantVersionTerms,
        scope: Scope | None = None,
    ) -> AmendedCovenant:
        """Create the next version, close the prior with `effective_to` and
        mark it superseded, in one transaction. Refuses — before writing
        anything — an amendment whose effective range overlaps any existing
        version of the same covenant, naming both version numbers."""
        resolved_scope = self._write_context(principal, scope)
        actor_id = self._registering_user_id(principal)
        validate_cure_period(terms.frequency, terms.cure_days)
        covenant = self.covenants.by_reference(reference, scope=resolved_scope)
        if covenant is None:
            raise NotFound(f"Covenant {reference!r} was not found within the current scope.")

        versions = self.versions.for_covenant(covenant.id, scope=resolved_scope)
        if not versions:
            raise Conflict(f"Covenant {covenant.reference} has no version to amend.")
        approval_workflow = self._approval_workflow_configured()
        approval_required = self._approval_required(COVENANT_AMENDMENT_OPERATION)
        if approval_workflow:
            pending = tuple(
                version
                for version in versions
                if version.status in {_DRAFT_STATUS, "pending_approval"}
            )
            if pending:
                raise Conflict(
                    f"Covenant {covenant.reference} already has a pending amendment "
                    f"(version {pending[-1].version_no})."
                )
            live_versions = tuple(version for version in versions if version.status == _LIVE_STATUS)
            if not live_versions:
                raise Conflict(
                    f"Covenant {covenant.reference} cannot be amended until a version is live."
                )
            current = live_versions[-1]
        else:
            current = versions[-1]
        self._validate_no_overlap(covenant, current, versions[:-1], terms)

        now = self._now()
        if not approval_workflow:
            self.versions.close_and_supersede(current, effective_to=terms.effective_from)
            current.updated_at = now
            current.updated_by_id = actor_id

        version = _new_version_row(
            covenant_id=covenant.id,
            version_no=current.version_no + 1,
            terms=terms,
            status=(
                "pending_approval"
                if approval_required
                else (_DRAFT_STATUS if approval_workflow else _LIVE_STATUS)
            ),
            registered_by_id=actor_id,
            now=now,
            request_id=self.request_id,
            actor_id=actor_id,
        )
        self.versions.add(version)
        self._flush_or_conflict(
            f"Covenant {covenant.reference} version {version.version_no} conflicts with an "
            "existing row."
        )

        self._audit(
            AuditEventType.COVENANT_AMENDED.value,
            covenant,
            {
                "action": "amended",
                "reference": covenant.reference,
                "previous_version_no": current.version_no,
                "version_no": version.version_no,
                "status": version.status,
                "effective_from": version.effective_from.isoformat(),
            },
            principal,
        )
        approval_request = self._submit_approval(
            COVENANT_AMENDMENT_OPERATION,
            subject=("covenant_version", version.id),
            payload={
                "covenant_id": str(covenant.id),
                "version_id": str(version.id),
                "reference": covenant.reference,
                "version_no": version.version_no,
                "effective_from": version.effective_from.isoformat(),
            },
            maker=principal,
        )
        return AmendedCovenant(
            previous_version=current,
            version=version,
            approval_request=approval_request,
        )

    def register_exception(
        self,
        principal: Principal,
        version_id: UUID,
        *,
        from_period: str,
        to_period: str,
        relaxed_threshold: Decimal,
        reason: str,
        document_id: UUID | None = None,
        scope: Scope | None = None,
    ) -> CovenantException:
        """Register one approved exception against a covenant version.

        Exception windows are inclusive, unlike covenant-version effective
        ranges.  The complete candidate is validated before it is staged;
        the overlap query is scoped through the already-authorised version's
        ownership path, and a conflict names both windows so an operator can
        correct the registration without guessing.
        """
        resolved_scope = self._write_context(principal, scope)
        actor_id = self._registering_user_id(principal)
        if not isinstance(version_id, UUID):
            raise ValidationError("version_id must be a UUID.", field="version_id")
        # Lock the immutable parent while checking and inserting the child.
        # This serialises exception registration for a version on PostgreSQL,
        # closing the query-then-insert race that could otherwise admit two
        # overlapping windows in concurrent transactions.
        version = self._version_for_exception(version_id, resolved_scope)
        if version is None:
            raise NotFound(f"Covenant version {version_id} was not found within the current scope.")
        try:
            start, end = validate_exception_window(from_period, to_period)
        except (TypeError, ValueError) as error:
            raise ValidationError(str(error), field="covenant_exception.from_period") from error
        validated_reason = _required_text(
            reason, "covenant_exception.reason", maximum=_REASON_MAX_LENGTH
        )
        threshold = _validated_exception_threshold(
            relaxed_threshold,
            base_threshold=version.threshold,
            direction=version.direction,
        )
        _validated_optional_uuid(document_id, "covenant_exception.document_id")

        existing = self._exceptions_for_version(version.id, resolved_scope)
        try:
            validate_no_overlapping_exceptions(start, end, existing)
        except ValueError as error:
            raise Conflict(
                f"Covenant version {version.version_no}: {error}",
                field="covenant_exception.from_period",
            ) from error

        now = self._now()
        exception = CovenantException(
            id=new_id(),
            covenant_version_id=version.id,
            from_period=start,
            to_period=end,
            relaxed_threshold=threshold,
            reason=validated_reason,
            document_id=document_id,
            # An exception is a recorded contractual relaxation, rather than
            # a pending waiver decision; its registering user is the approver
            # recorded on this first-class object.
            approved_by_id=actor_id,
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=self.request_id,
        )
        self.session.add(exception)
        self._flush_or_conflict(
            f"Covenant version {version.version_no} exception could not be registered."
        )
        self._audit_subject(
            AuditEventType.COVENANT_EXCEPTION_REGISTERED.value,
            "covenant_exception",
            exception.id,
            {
                "action": "registered",
                "covenant_version_id": str(version.id),
                "version_no": version.version_no,
                "from_period": start,
                "to_period": end,
                "relaxed_threshold": str(threshold) if threshold is not None else None,
                "approved_by_id": str(actor_id),
            },
            principal,
        )
        return exception

    def request_waiver(
        self,
        principal: Principal,
        covenant_reference: str | UUID,
        *,
        from_date: date,
        to_date: date | None = None,
        reason: str,
        waiver_scope: str | None = None,
        document_id: UUID | None = None,
        scope: Scope | None = None,
    ) -> CovenantWaiver:
        """Create a pending waiver request without changing covenant terms."""
        resolved_scope = self._write_context(principal, scope, permission=RECORD_WAIVER_PERMISSION)
        actor_id = self._registering_user_id(principal, operation="record a waiver")
        covenant = self._covenant_by_identifier(covenant_reference, resolved_scope)
        if covenant is None:
            raise NotFound("The covenant was not found within the current scope.")
        start, end = _validate_date_window(from_date, to_date, field="covenant_waiver.from_date")
        validated_reason = _required_text(
            reason, "covenant_waiver.reason", maximum=_REASON_MAX_LENGTH
        )
        validated_scope = _optional_text(
            waiver_scope,
            "covenant_waiver.scope",
            maximum=_WAIVER_SCOPE_MAX_LENGTH,
        )
        _validated_optional_uuid(document_id, "covenant_waiver.document_id")
        now = self._now()
        waiver = CovenantWaiver(
            id=new_id(),
            covenant_id=covenant.id,
            from_date=start,
            to_date=end,
            scope=validated_scope,
            reason=validated_reason,
            document_id=document_id,
            requested_by_id=actor_id,
            approved_by_id=None,
            state="requested",
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=self.request_id,
        )
        self.session.add(waiver)
        self._flush_or_conflict("The covenant waiver could not be recorded.")
        self._audit_subject(
            AuditEventType.COVENANT_WAIVER_REQUESTED.value,
            "covenant_waiver",
            waiver.id,
            {
                "action": "requested",
                "covenant_id": str(covenant.id),
                "from_date": start.isoformat(),
                "to_date": end.isoformat() if end is not None else None,
                "scope": validated_scope,
                "reason": validated_reason,
                "requested_by_id": str(actor_id),
                "state": waiver.state,
            },
            principal,
        )
        self._submit_approval(
            COVENANT_WAIVER_OPERATION,
            subject=("covenant_waiver", waiver.id),
            payload={
                "covenant_id": str(covenant.id),
                "waiver_id": str(waiver.id),
                "reference": covenant.reference,
                "from_date": start.isoformat(),
                "to_date": end.isoformat() if end is not None else None,
            },
            maker=principal,
        )
        return waiver

    def approve_waiver(
        self,
        principal: Principal,
        waiver_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> CovenantWaiver:
        """Approve one pending waiver in the caller's transaction."""
        if self._approval_required(COVENANT_WAIVER_OPERATION):
            self._require_principal(principal, Permission.APPROVE_COVENANT)
            resolved_scope = self._validated_scope(principal, scope)
            if not isinstance(waiver_id, UUID):
                raise ValidationError("waiver_id must be a UUID.", field="waiver_id")
            waiver = self._waiver_by_id(waiver_id, resolved_scope, for_update=False)
            if waiver is None:
                raise NotFound(
                    f"Covenant waiver {waiver_id} was not found within the current scope."
                )
            request = self._pending_request_for_payload(
                COVENANT_WAIVER_OPERATION,
                "waiver_id",
                waiver_id,
                resolved_scope,
            )
            if request is None:
                raise Conflict(f"Covenant waiver {waiver_id} has no pending approval request.")
            self.decide_approval(
                principal,
                request.id,
                approved=True,
                scope=resolved_scope,
            )
            return waiver
        resolved_scope = self._write_context(principal, scope, permission=RECORD_WAIVER_PERMISSION)
        actor_id = self._registering_user_id(principal, operation="approve a waiver")
        if not isinstance(waiver_id, UUID):
            raise ValidationError("waiver_id must be a UUID.", field="waiver_id")
        waiver = self._waiver_by_id(waiver_id, resolved_scope, for_update=True)
        if waiver is None:
            raise NotFound(f"Covenant waiver {waiver_id} was not found within the current scope.")
        if waiver.state != "requested":
            raise Conflict(
                f"Covenant waiver {waiver_id} is {waiver.state}; only requested waivers "
                "can be approved."
            )
        if waiver.requested_by_id == actor_id:
            raise Conflict(
                f"Covenant waiver {waiver_id} cannot be approved by its requester; "
                "the distinct-actor rule requires a different approver."
            )
        now = self._now()
        waiver.state = "approved"
        waiver.approved_by_id = actor_id
        waiver.updated_at = now
        waiver.updated_by_id = actor_id
        waiver.version += 1
        self.session.flush()
        self._audit_subject(
            AuditEventType.COVENANT_WAIVER_APPROVED.value,
            "covenant_waiver",
            waiver.id,
            {
                "action": "approved",
                "covenant_id": str(waiver.covenant_id),
                "requested_by_id": str(waiver.requested_by_id)
                if waiver.requested_by_id is not None
                else None,
                "approved_by_id": str(actor_id),
                "from_date": waiver.from_date.isoformat(),
                "to_date": waiver.to_date.isoformat() if waiver.to_date is not None else None,
                "state": waiver.state,
            },
            principal,
        )
        return waiver

    def reject_waiver(
        self,
        principal: Principal,
        waiver_id: UUID,
        *,
        reason: str,
        scope: Scope | None = None,
    ) -> CovenantWaiver:
        """Reject one pending waiver and retain the request for audit."""
        if self._approval_required(COVENANT_WAIVER_OPERATION):
            self._require_principal(principal, Permission.APPROVE_COVENANT)
            resolved_scope = self._validated_scope(principal, scope)
            if not isinstance(waiver_id, UUID):
                raise ValidationError("waiver_id must be a UUID.", field="waiver_id")
            waiver = self._waiver_by_id(waiver_id, resolved_scope, for_update=False)
            if waiver is None:
                raise NotFound(
                    f"Covenant waiver {waiver_id} was not found within the current scope."
                )
            request = self._pending_request_for_payload(
                COVENANT_WAIVER_OPERATION,
                "waiver_id",
                waiver_id,
                resolved_scope,
            )
            if request is None:
                raise Conflict(f"Covenant waiver {waiver_id} has no pending approval request.")
            self.decide_approval(
                principal,
                request.id,
                approved=False,
                reason=reason,
                scope=resolved_scope,
            )
            return waiver
        resolved_scope = self._write_context(principal, scope, permission=RECORD_WAIVER_PERMISSION)
        actor_id = self._registering_user_id(principal, operation="reject a waiver")
        if not isinstance(waiver_id, UUID):
            raise ValidationError("waiver_id must be a UUID.", field="waiver_id")
        validated_reason = _required_text(
            reason, "covenant_waiver.rejection_reason", maximum=_REASON_MAX_LENGTH
        )
        waiver = self._waiver_by_id(waiver_id, resolved_scope, for_update=True)
        if waiver is None:
            raise NotFound(f"Covenant waiver {waiver_id} was not found within the current scope.")
        if waiver.state != "requested":
            raise Conflict(
                f"Covenant waiver {waiver_id} is {waiver.state}; only requested waivers "
                "can be rejected."
            )
        now = self._now()
        waiver.state = "rejected"
        waiver.updated_at = now
        waiver.updated_by_id = actor_id
        waiver.version += 1
        self.session.flush()
        self._audit_subject(
            AuditEventType.COVENANT_WAIVER_REJECTED.value,
            "covenant_waiver",
            waiver.id,
            {
                "action": "rejected",
                "covenant_id": str(waiver.covenant_id),
                "requested_by_id": str(waiver.requested_by_id)
                if waiver.requested_by_id is not None
                else None,
                "rejected_by_id": str(actor_id),
                "rejection_reason": validated_reason,
                "state": waiver.state,
            },
            principal,
        )
        return waiver

    def retire(
        self,
        principal: Principal,
        reference: str,
        *,
        scope: Scope | None = None,
    ) -> RetiredCovenant:
        """Retire a covenant, preserving every historical version.

        A retirement is a state change to the stable covenant identity and
        the live version.  When maker-checker is enabled the request is
        staged without changing either row; the approval callback repeats the
        cure-state check immediately before applying it.
        """
        resolved_scope = self._write_context(principal, scope)
        actor_user_id = self._registering_user_id(principal, operation="retire a covenant")
        covenant = self.covenants.by_reference(reference, scope=resolved_scope)
        if covenant is None:
            raise NotFound(f"Covenant {reference!r} was not found within the current scope.")
        versions = tuple(self.versions.for_covenant(covenant.id, scope=resolved_scope))
        live_versions = tuple(version for version in versions if version.status == _LIVE_STATUS)
        if not live_versions:
            raise Conflict(f"Covenant {covenant.reference} has no live version to retire.")
        version = live_versions[-1]
        self._refuse_open_cure(covenant.id, today=self._now().date())

        approval_required = self._approval_required(COVENANT_RETIREMENT_OPERATION)
        approval_request: MakerCheckerRequest | None = None
        if approval_required:
            if any(
                request.operation == COVENANT_RETIREMENT_OPERATION
                and _payload_uuid(request.payload, "covenant_id") == covenant.id
                for request in self._pending_requests_in_scope(resolved_scope)
            ):
                raise Conflict(
                    f"Covenant {covenant.reference} already has a pending retirement approval."
                )
            approval_request = self._submit_approval(
                COVENANT_RETIREMENT_OPERATION,
                subject=("covenant", covenant.id),
                payload={
                    "covenant_id": str(covenant.id),
                    "version_id": str(version.id),
                    "reference": covenant.reference,
                },
                maker=principal,
            )
            return RetiredCovenant(covenant, version, approval_request)

        self._apply_retirement(covenant, version, actor_user_id, principal)
        return RetiredCovenant(covenant, version)

    def list_covenants(
        self,
        principal: Principal,
        *,
        active_only: bool | None = True,
        limit: int | None = None,
        offset: int = 0,
        scope: Scope | None = None,
    ) -> Sequence[Covenant]:
        """Return a deterministic, scoped covenant list."""
        if active_only is not None and not isinstance(active_only, bool):
            raise ValidationError("active_only must be a boolean or null.", field="active_only")
        if offset < 0:
            raise ValidationError("offset must not be negative.", field="offset")
        if limit is not None and not 1 <= limit <= 200:
            raise ValidationError("limit must be between 1 and 200.", field="limit")
        resolved_scope = self._read_context(principal, scope)
        rows = tuple(self.covenants.list(scope=resolved_scope))
        if active_only is not None:
            rows = tuple(row for row in rows if row.is_active is active_only)
        rows = tuple(sorted(rows, key=lambda row: (row.reference, row.id.hex)))
        if limit is None:
            return rows[offset:]
        return rows[offset : offset + limit]

    def get_covenant(
        self,
        principal: Principal,
        reference: str,
        *,
        scope: Scope | None = None,
    ) -> Covenant:
        """Return one covenant or the same designed 404 for an out-of-scope row."""
        resolved_scope = self._read_context(principal, scope)
        covenant = self.covenants.by_reference(reference, scope=resolved_scope)
        if covenant is None:
            raise NotFound(f"Covenant {reference!r} was not found within the current scope.")
        return covenant

    def list_versions(
        self,
        principal: Principal,
        covenant_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> Sequence[CovenantVersion]:
        """Return all versions for one scoped covenant, oldest first."""
        resolved_scope = self._read_context(principal, scope)
        covenant = self.covenants.get(covenant_id, scope=resolved_scope)
        if covenant is None:
            raise NotFound("The covenant was not found within the current scope.")
        return self.versions.for_covenant(covenant.id, scope=resolved_scope)

    def pending_approvals(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
    ) -> tuple[MakerCheckerRequest, ...]:
        """Return only approval requests for scoped covenant subjects."""
        self._require_principal(principal, Permission.APPROVE_COVENANT)
        if self.approvals is None:
            return ()
        resolved_scope = self._validated_scope(principal, scope)
        requests = self.approvals.list_pending(principal)
        visible: list[MakerCheckerRequest] = []
        for request in requests:
            if self._approval_request_in_scope(request, resolved_scope):
                visible.append(request)
        return tuple(visible)

    def decide_approval(
        self,
        principal: Principal,
        request_id: UUID | str,
        *,
        approved: bool,
        reason: str | None = None,
        scope: Scope | None = None,
    ) -> MakerCheckerRequest:
        """Decide a scoped request and apply rejected waiver state if needed."""
        if self.approvals is None:
            raise ValidationError("Maker-checker is not configured.", field="approval")
        if not isinstance(principal, Principal) or principal.kind is not PrincipalKind.USER:
            raise AuthorizationError(
                "Only an authenticated user may decide a covenant approval.",
                field="principal",
            )
        self._require_principal(principal, Permission.APPROVE_COVENANT)
        resolved_scope = self._validated_scope(principal, scope)
        parsed_id = _coerce_uuid(request_id, "request_id")
        pending = next(
            (
                request
                for request in self._pending_requests_in_scope(resolved_scope)
                if request.id == parsed_id
            ),
            None,
        )
        if pending is None:
            raise NotFound(
                f"Maker-checker request {parsed_id} was not found within the current scope."
            )
        decided = self.approvals.decide(parsed_id, principal, approved, reason)
        if not approved and decided.operation == COVENANT_WAIVER_OPERATION:
            self._apply_waiver_rejection(decided, principal, resolved_scope)
        return decided

    def approve_covenant(
        self,
        principal: Principal,
        reference: str,
        *,
        approved: bool,
        reason: str | None = None,
        scope: Scope | None = None,
    ) -> MakerCheckerRequest:
        """Apply the pending registration/amendment/retirement for a reference."""
        self._require_principal(principal, Permission.APPROVE_COVENANT)
        resolved_scope = self._validated_scope(principal, scope)
        covenant = self.covenants.by_reference(reference, scope=resolved_scope)
        if covenant is None:
            raise NotFound(f"Covenant {reference!r} was not found within the current scope.")
        candidates = tuple(
            request
            for request in self._pending_requests_in_scope(resolved_scope)
            if request.operation
            in {
                COVENANT_REGISTRATION_OPERATION,
                COVENANT_AMENDMENT_OPERATION,
                COVENANT_RETIREMENT_OPERATION,
            }
            if _payload_uuid(request.payload, "covenant_id") == covenant.id
        )
        if not candidates:
            raise NotFound(
                f"No pending approval was found for covenant {reference!r} "
                "within the current scope."
            )
        request = candidates[0]
        return self.decide_approval(
            principal,
            request.id,
            approved=approved,
            reason=reason,
            scope=resolved_scope,
        )

    def _pending_requests_in_scope(self, scope: Scope) -> tuple[MakerCheckerRequest, ...]:
        """Return pending covenant requests without hiding the maker's own row.

        ``ApprovalService.list_pending`` intentionally omits a maker's own
        request for queue presentation.  Decision endpoints need a different
        view: they must locate that row so ``ApprovalService.decide`` can
        return its explicit 409 distinct-actor error.  Authorization is done
        by the public caller before this repository read.
        """
        if self.approvals is None:
            return ()
        return tuple(
            request
            for request in self.approvals.repository.list_pending()
            if request.state is MakerCheckerState.PENDING
            and self._approval_request_in_scope(request, scope)
        )

    def _pending_request_for_payload(
        self,
        operation: str,
        key: str,
        value: UUID,
        scope: Scope,
    ) -> MakerCheckerRequest | None:
        return next(
            (
                request
                for request in self._pending_requests_in_scope(scope)
                if request.operation == operation and _payload_uuid(request.payload, key) == value
            ),
            None,
        )

    def live_at(
        self,
        principal: Principal,
        facility_id: UUID,
        as_of: date,
        *,
        scope: Scope | None = None,
    ) -> Sequence[CovenantVersion]:
        """Return the versions in force, one per covenant of `facility_id`,
        on `as_of` — empty for a date before any version existed."""
        if not isinstance(as_of, date):
            raise ValidationError("as_of must be a date.", field="as_of")
        resolved_scope = self._read_context(principal, scope)
        return tuple(
            version
            for version in self.versions.live_at(facility_id, as_of, scope=resolved_scope)
            if version.status == _LIVE_STATUS
        )

    # ---- internal invariants ---------------------------------------------

    def _initial_status(self) -> str:
        if not self.maker_checker_enabled:
            return _LIVE_STATUS
        return _DRAFT_STATUS

    def _approval_workflow_configured(self) -> bool:
        return self.maker_checker_enabled and self.approvals is not None

    def _approval_required(self, operation: str) -> bool:
        return bool(
            self.maker_checker_enabled
            and self.approvals is not None
            and self.approvals.settings.is_enabled(operation)
        )

    def _wire_approval_callbacks(self) -> None:
        """Register exactly one callback for every covenant operation."""
        if self.approvals is None:
            return
        callbacks = {
            COVENANT_REGISTRATION_OPERATION: self._apply_registration_approval,
            COVENANT_AMENDMENT_OPERATION: self._apply_amendment_approval,
            COVENANT_WAIVER_OPERATION: self._apply_waiver_approval,
            COVENANT_RETIREMENT_OPERATION: self._apply_retirement_approval,
        }
        permissions = {
            COVENANT_REGISTRATION_OPERATION: (
                REGISTER_COVENANT_PERMISSION,
                Permission.APPROVE_COVENANT,
            ),
            COVENANT_AMENDMENT_OPERATION: (
                REGISTER_COVENANT_PERMISSION,
                Permission.APPROVE_COVENANT,
            ),
            COVENANT_WAIVER_OPERATION: (RECORD_WAIVER_PERMISSION, Permission.APPROVE_COVENANT),
            COVENANT_RETIREMENT_OPERATION: (
                REGISTER_COVENANT_PERMISSION,
                Permission.APPROVE_COVENANT,
            ),
        }
        for operation, callback in callbacks.items():
            propose, approve = permissions[operation]
            if operation in self.approvals.registry:
                existing = self.approvals.registry.get(operation)
                if existing.callback != callback:
                    raise Conflict(
                        f"Maker-checker operation {operation!r} is already wired "
                        "to another callback."
                    )
                continue
            self.approvals.registry.register(
                operation,
                callback,
                propose_permission=propose,
                approve_permission=approve,
            )

    def _submit_approval(
        self,
        operation: str,
        *,
        subject: object,
        payload: Mapping[str, object],
        maker: Principal,
    ) -> MakerCheckerRequest | None:
        if not self._approval_workflow_configured():
            return None
        return self.approvals.submit(operation, subject, payload, maker)

    def _approval_request_in_scope(self, request: MakerCheckerRequest, scope: Scope) -> bool:
        if request.operation not in {
            COVENANT_REGISTRATION_OPERATION,
            COVENANT_AMENDMENT_OPERATION,
            COVENANT_WAIVER_OPERATION,
            COVENANT_RETIREMENT_OPERATION,
        }:
            return False
        subject_type = request.subject_type
        if subject_type == "covenant":
            return self.covenants.get(request.subject_id, scope=scope) is not None
        if subject_type == "covenant_version":
            return self.versions.get(request.subject_id, scope=scope) is not None
        if subject_type == "covenant_waiver":
            return self._waiver_by_id(request.subject_id, scope, for_update=False) is not None
        return False

    def _apply_registration_approval(
        self, request: MakerCheckerRequest, checker_id: UUID
    ) -> object:
        covenant_id = _payload_uuid(request.payload, "covenant_id")
        version_id = _payload_uuid(request.payload, "version_id")
        principal = self._approval_principal(checker_id)
        scope = self._validated_scope(principal, None)
        version = self.versions.get(version_id, scope=scope)
        if version is None or version.covenant_id != covenant_id:
            raise NotFound(
                "The covenant version for this approval was not found in the current scope."
            )
        if version.status not in {_DRAFT_STATUS, "pending_approval"}:
            raise Conflict(
                f"Covenant version {version.version_no} is {version.status}; "
                "only a draft can be approved."
            )
        now = self._now()
        version.status = _LIVE_STATUS
        version.approved_by_id = checker_id
        version.updated_at = now
        version.updated_by_id = checker_id
        version.version += 1
        self.session.flush()
        self._audit_subject(
            AuditEventType.COVENANT_REGISTRATION_APPROVED.value,
            "covenant_version",
            version.id,
            {
                "action": "approved",
                "covenant_id": str(version.covenant_id),
                "version_no": version.version_no,
                "approved_by_id": str(checker_id),
                "status": version.status,
            },
            principal,
        )
        return version

    def _apply_amendment_approval(self, request: MakerCheckerRequest, checker_id: UUID) -> object:
        covenant_id = _payload_uuid(request.payload, "covenant_id")
        version_id = _payload_uuid(request.payload, "version_id")
        principal = self._approval_principal(checker_id)
        scope = self._validated_scope(principal, None)
        version = self.versions.get(version_id, scope=scope)
        if version is None or version.covenant_id != covenant_id:
            raise NotFound(
                "The covenant amendment for this approval was not found in the current scope."
            )
        if version.status not in {_DRAFT_STATUS, "pending_approval"}:
            raise Conflict(
                f"Covenant amendment version {version.version_no} is {version.status}; "
                "only a pending amendment can be approved."
            )
        covenant = self.covenants.get(version.covenant_id, scope=scope)
        if covenant is None:
            raise NotFound("The covenant for this amendment was not found in the current scope.")
        versions = tuple(self.versions.for_covenant(covenant.id, scope=scope))
        live_versions = tuple(item for item in versions if item.status == _LIVE_STATUS)
        if len(live_versions) != 1:
            raise Conflict(
                f"Covenant {covenant.reference} must have exactly one live version before approval."
            )
        current = live_versions[0]
        if (
            version.version_no <= current.version_no
            or version.effective_from <= current.effective_from
        ):
            raise Conflict(
                f"Covenant {covenant.reference} amendment version {version.version_no} "
                "is no longer the next effective version."
            )
        now = self._now()
        self.versions.close_and_supersede(current, effective_to=version.effective_from)
        current.updated_at = now
        current.updated_by_id = checker_id
        version.status = _LIVE_STATUS
        version.approved_by_id = checker_id
        version.updated_at = now
        version.updated_by_id = checker_id
        version.version += 1
        self.session.flush()
        self._audit_subject(
            AuditEventType.COVENANT_AMENDMENT_APPROVED.value,
            "covenant_version",
            version.id,
            {
                "action": "approved",
                "covenant_id": str(covenant.id),
                "reference": covenant.reference,
                "previous_version_no": current.version_no,
                "version_no": version.version_no,
                "approved_by_id": str(checker_id),
                "status": version.status,
            },
            principal,
        )
        return version

    def _apply_waiver_approval(self, request: MakerCheckerRequest, checker_id: UUID) -> object:
        covenant_id = _payload_uuid(request.payload, "covenant_id")
        waiver_id = _payload_uuid(request.payload, "waiver_id")
        principal = self._approval_principal(checker_id)
        scope = self._validated_scope(principal, None)
        waiver = self._waiver_by_id(waiver_id, scope, for_update=True)
        if waiver is None or waiver.covenant_id != covenant_id:
            raise NotFound(
                "The covenant waiver for this approval was not found in the current scope."
            )
        if waiver.state != "requested":
            raise Conflict(
                f"Covenant waiver {waiver.id} is {waiver.state}; "
                "only requested waivers can be approved."
            )
        now = self._now()
        waiver.state = "approved"
        waiver.approved_by_id = checker_id
        waiver.updated_at = now
        waiver.updated_by_id = checker_id
        waiver.version += 1
        self.session.flush()
        self._audit_subject(
            AuditEventType.COVENANT_WAIVER_APPROVED.value,
            "covenant_waiver",
            waiver.id,
            {
                "action": "approved",
                "covenant_id": str(waiver.covenant_id),
                "requested_by_id": str(waiver.requested_by_id),
                "approved_by_id": str(checker_id),
                "state": waiver.state,
            },
            principal,
        )
        return waiver

    def _apply_waiver_rejection(
        self,
        request: MakerCheckerRequest,
        principal: Principal,
        scope: Scope,
    ) -> CovenantWaiver:
        covenant_id = _payload_uuid(request.payload, "covenant_id")
        waiver_id = _payload_uuid(request.payload, "waiver_id")
        waiver = self._waiver_by_id(waiver_id, scope, for_update=True)
        if waiver is None or waiver.covenant_id != covenant_id:
            raise NotFound(
                "The covenant waiver for this decision was not found in the current scope."
            )
        if waiver.state != "requested":
            raise Conflict(
                f"Covenant waiver {waiver.id} is {waiver.state}; "
                "only requested waivers can be rejected."
            )
        now = self._now()
        waiver.state = "rejected"
        waiver.updated_at = now
        waiver.updated_by_id = principal.id
        waiver.version += 1
        self.session.flush()
        self._audit_subject(
            AuditEventType.COVENANT_WAIVER_REJECTED.value,
            "covenant_waiver",
            waiver.id,
            {
                "action": "rejected",
                "covenant_id": str(waiver.covenant_id),
                "requested_by_id": str(waiver.requested_by_id),
                "rejected_by_id": str(principal.id),
                "rejection_reason": request.reason,
                "state": waiver.state,
            },
            principal,
        )
        return waiver

    def _apply_retirement_approval(self, request: MakerCheckerRequest, checker_id: UUID) -> object:
        covenant_id = _payload_uuid(request.payload, "covenant_id")
        version_id = _payload_uuid(request.payload, "version_id")
        principal = self._approval_principal(checker_id)
        scope = self._validated_scope(principal, None)
        covenant = self.covenants.get(covenant_id, scope=scope)
        version = self.versions.get(version_id, scope=scope)
        if covenant is None or version is None or version.covenant_id != covenant.id:
            raise NotFound(
                "The covenant retirement for this approval was not found in the current scope."
            )
        self._apply_retirement(covenant, version, checker_id, principal)
        return covenant

    def _apply_retirement(
        self,
        covenant: Covenant,
        version: CovenantVersion,
        actor_id: UUID,
        principal: Principal,
    ) -> None:
        self._refuse_open_cure(covenant.id, today=self._now().date())
        if not covenant.is_active or version.status != _LIVE_STATUS:
            raise Conflict(f"Covenant {covenant.reference} is already retired or not live.")
        now = self._now()
        covenant.is_active = False
        covenant.updated_at = now
        covenant.updated_by_id = actor_id
        version.status = "retired"
        version.updated_at = now
        version.updated_by_id = actor_id
        version.version += 1
        self.session.flush()
        self._audit(
            AuditEventType.COVENANT_RETIRED.value,
            covenant,
            {
                "action": "retired",
                "reference": covenant.reference,
                "version_no": version.version_no,
                "retired_by_id": str(actor_id),
                "status": version.status,
            },
            principal,
        )

    def _approval_principal(self, actor_id: UUID) -> Principal:
        return Principal.user(actor_id, (Permission.APPROVE_COVENANT, Permission.VIEW_COVENANT))

    def _refuse_open_cure(self, covenant_id: UUID, *, today: date) -> None:
        statement = (
            select(CovenantTest)
            .join(CovenantVersion, CovenantVersion.id == CovenantTest.covenant_version_id)
            .where(
                CovenantVersion.covenant_id == covenant_id,
                CovenantTest.verdict == "breach_cure_open",
                (CovenantTest.cure_ends_on.is_(None) | (CovenantTest.cure_ends_on >= today)),
            )
            .limit(1)
        )
        open_test = self.session.execute(statement).scalars().one_or_none()
        if open_test is not None:
            raise Conflict(
                f"Covenant retirement is refused while cure state {open_test.verdict!r} "
                f"is open through {open_test.cure_ends_on or 'an undated window'}."
            )

    def _validate_no_overlap(
        self,
        covenant: Covenant,
        current: CovenantVersion,
        historical: Sequence[CovenantVersion],
        terms: CovenantVersionTerms,
    ) -> None:
        next_version_no = current.version_no + 1
        if terms.effective_from <= current.effective_from:
            raise Conflict(
                f"Covenant {covenant.reference}: the proposed version "
                f"{next_version_no}'s effective_from ({terms.effective_from}) does not come "
                f"after version {current.version_no}'s own effective_from "
                f"({current.effective_from}); the ranges overlap.",
                field="covenant_version.effective_from",
            )
        for historical_version in historical:
            if _ranges_overlap(
                terms.effective_from,
                terms.effective_to,
                historical_version.effective_from,
                historical_version.effective_to,
            ):
                raise Conflict(
                    f"Covenant {covenant.reference}: the proposed version "
                    f"{next_version_no}'s effective range overlaps version "
                    f"{historical_version.version_no}'s "
                    f"({historical_version.effective_from} to "
                    f"{historical_version.effective_to or 'open'}).",
                    field="covenant_version.effective_from",
                )

    def _read_context(self, principal: Principal, scope: Scope | None) -> Scope:
        self._require_principal(principal, VIEW_COVENANT_PERMISSION)
        return self._validated_scope(principal, scope)

    def _write_context(
        self,
        principal: Principal,
        scope: Scope | None,
        *,
        permission: Permission = REGISTER_COVENANT_PERMISSION,
    ) -> Scope:
        self._require_principal(principal, permission)
        return self._validated_scope(principal, scope)

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        if scope is None:
            resolved = (
                self.scope_resolver(principal)
                if self.scope_resolver is not None
                else resolve_scope(principal, self.session)
            )
            if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
                raise AuthorizationError(
                    "The resolved scope does not belong to the authenticated principal."
                )
            return resolved
        if scope.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied scope does not belong to the authenticated principal."
            )
        return scope

    @staticmethod
    def _require_principal(principal: Principal, permission: Permission) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, permission)

    @staticmethod
    def _registering_user_id(
        principal: Principal, *, operation: str = "register or amend a covenant"
    ) -> UUID:
        """`covenant_version.registered_by_id` is a required foreign key to
        `app_user`, so only a session-user principal — never an API key —
        can register or amend a covenant."""
        if principal.kind is not PrincipalKind.USER:
            raise ValidationError(
                f"Only an authenticated user, not an API key, may {operation}.",
                field="principal",
            )
        return principal.id

    def _audit(
        self,
        event_type: str,
        covenant: Covenant,
        payload: Mapping[str, object],
        principal: Principal,
    ) -> None:
        self._audit_subject(event_type, "covenant", covenant.id, payload, principal)

    def _audit_subject(
        self,
        event_type: str,
        subject_type: str,
        subject_id: UUID,
        payload: Mapping[str, object],
        principal: Principal,
    ) -> None:
        self.audit.record(
            event_type,
            (subject_type, subject_id),
            dict(payload),
            actor=principal.id,
            request_id=self.request_id,
        )

    def _exceptions_for_version(
        self, version_id: UUID, scope: Scope
    ) -> Sequence[CovenantException]:
        ownership = ownership_path_for(CovenantException)
        statement = ownership.apply(select(CovenantException)).where(
            scope.predicate(ownership.path_column),
            CovenantException.covenant_version_id == version_id,
        )
        return tuple(self.session.execute(statement).scalars().all())

    def _version_for_exception(self, version_id: UUID, scope: Scope) -> CovenantVersion | None:
        ownership = ownership_path_for(CovenantVersion)
        statement = ownership.apply(select(CovenantVersion)).where(
            scope.predicate(ownership.path_column),
            CovenantVersion.id == version_id,
        )
        return self.session.execute(statement.with_for_update()).scalars().one_or_none()

    def _waiver_by_id(
        self,
        waiver_id: UUID,
        scope: Scope,
        *,
        for_update: bool,
    ) -> CovenantWaiver | None:
        ownership = ownership_path_for(CovenantWaiver)
        statement = ownership.apply(select(CovenantWaiver)).where(
            scope.predicate(ownership.path_column),
            CovenantWaiver.id == waiver_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.execute(statement).scalars().one_or_none()

    def _covenant_by_identifier(self, identifier: str | UUID, scope: Scope) -> Covenant | None:
        if isinstance(identifier, UUID):
            return self.covenants.get(identifier, scope=scope)
        if not isinstance(identifier, str):
            raise ValidationError(
                "covenant_reference must be text or a UUID.", field="covenant_reference"
            )
        return self.covenants.by_reference(identifier, scope=scope)

    def _flush_or_conflict(self, message: str) -> None:
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as error:
            raise Conflict(message) from error

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Registry clock must return an aware datetime.")
        return now.astimezone(UTC)


def _ranges_overlap(from_a: date, to_a: date | None, from_b: date, to_b: date | None) -> bool:
    """Whether half-open intervals `[from_a, to_a)` and `[from_b, to_b)`
    intersect, with `None` meaning open-ended."""
    if to_a is not None and to_a <= from_b:
        return False
    if to_b is not None and to_b <= from_a:
        return False
    return True


class SqlAlchemyMakerCheckerRepository(MakerCheckerRepository):
    """SQLAlchemy adapter for the shared maker-checker request port.

    The adapter is colocated with the registry composition because the
    generic approval service deliberately has no database dependency.  Every
    method participates in the registry caller's transaction; no method
    commits or silently opens another session.
    """

    def __init__(self, session: Session, *, request_id: str = "maker-checker") -> None:
        if not is_database_session(session):
            raise TypeError("SqlAlchemyMakerCheckerRepository requires a SQLAlchemy Session.")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 40:
            raise ValueError("Maker-checker request_id must be between 1 and 40 characters.")
        self.session = session
        self.request_id = request_id

    def create(self, request: MakerCheckerRequest) -> MakerCheckerRequest:
        row = MakerCheckerRow(
            id=request.id,
            subject_type=request.subject_type,
            subject_id=request.subject_id,
            operation=request.operation,
            payload=dict(request.payload),
            maker_id=request.maker_id,
            checker_id=None,
            state=request.state.value,
            created_at=request.created_at,
            updated_at=request.created_at,
            created_by_id=request.maker_id,
            updated_by_id=request.maker_id,
            request_id=self.request_id,
            version=request.version,
        )
        self.session.add(row)
        self.session.flush()
        return _maker_checker_request(row)

    def get_for_update(self, request_id: UUID) -> MakerCheckerRequest | None:
        statement = (
            select(MakerCheckerRow).where(MakerCheckerRow.id == request_id).with_for_update()
        )
        row = self.session.execute(statement).scalars().one_or_none()
        return _maker_checker_request(row) if row is not None else None

    def list_pending(self) -> Sequence[MakerCheckerRequest]:
        rows = (
            self.session.execute(
                select(MakerCheckerRow)
                .where(MakerCheckerRow.state == MakerCheckerState.PENDING.value)
                .order_by(MakerCheckerRow.created_at, MakerCheckerRow.id)
            )
            .scalars()
            .all()
        )
        return tuple(_maker_checker_request(row) for row in rows)

    def decide(
        self,
        request_id: UUID,
        *,
        checker_id: UUID,
        state: MakerCheckerState,
        decided_at: datetime,
        reason: str | None,
        expected_version: int,
    ) -> MakerCheckerRequest:
        row = self._locked_pending(request_id, expected_version)
        row.checker_id = checker_id
        row.state = state.value
        row.decided_at = decided_at
        row.reason = reason
        row.updated_at = decided_at
        row.updated_by_id = checker_id
        row.version += 1
        self.session.flush()
        return _maker_checker_request(row)

    def expire(
        self,
        request_id: UUID,
        *,
        expired_at: datetime,
        expected_version: int,
    ) -> MakerCheckerRequest:
        row = self._locked_pending(request_id, expected_version)
        row.state = MakerCheckerState.EXPIRED.value
        row.decided_at = expired_at
        row.reason = "Approval window elapsed."
        row.updated_at = expired_at
        row.updated_by_id = None
        row.version += 1
        self.session.flush()
        return _maker_checker_request(row)

    def _locked_pending(self, request_id: UUID, expected_version: int) -> MakerCheckerRow:
        row = (
            self.session.execute(
                select(MakerCheckerRow).where(MakerCheckerRow.id == request_id).with_for_update()
            )
            .scalars()
            .one_or_none()
        )
        if row is None:
            raise NotFound(f"Maker-checker request {request_id} was not found.")
        if row.state != MakerCheckerState.PENDING.value:
            raise Conflict(
                f"Maker-checker request {request_id} is already {row.state}; it is not pending."
            )
        if row.version != expected_version:
            raise Conflict(f"Maker-checker request {request_id} has a stale version.")
        return row


class _DatabaseApprovalNotifier:
    """Queue expiry notices in the durable in-app notification table."""

    def __init__(self, session: Session, clock: Clock, *, request_id: str) -> None:
        self.session = session
        self.clock = clock
        self.request_id = request_id

    def notify(self, event_type: str, payload: Mapping[str, object]) -> object:
        maker_id = _payload_uuid(payload, "maker_id")
        subject_id = _payload_uuid(payload, "subject_id")
        now = self.clock.now()
        notification = Notification(
            id=new_id(),
            recipient_id=maker_id,
            channel="inapp",
            template=event_type,
            subject_type=str(payload.get("subject_type", "maker_checker_request")),
            subject_id=subject_id,
            payload=dict(payload),
            state="pending",
            scheduled_for=now,
            created_at=now,
            updated_at=now,
            created_by_id=maker_id,
            updated_by_id=maker_id,
            request_id=self.request_id,
        )
        self.session.add(notification)
        self.session.flush()
        return notification


def _default_approval_service(
    session: Session,
    *,
    audit: AuditWriter,
    clock: Clock,
    request_id: str,
) -> ApprovalService:
    """Build the production database-backed approval workflow."""
    return ApprovalService(
        SqlAlchemyMakerCheckerRepository(session, request_id=request_id),
        audit,
        registry=ApplicationCallbackRegistry(),
        clock=clock,
        settings=MakerCheckerSettings(
            enabled_operations={
                COVENANT_REGISTRATION_OPERATION: True,
                COVENANT_AMENDMENT_OPERATION: True,
                COVENANT_WAIVER_OPERATION: True,
                COVENANT_RETIREMENT_OPERATION: True,
            }
        ),
        notifier=_DatabaseApprovalNotifier(session, clock, request_id=request_id),
        request_id=request_id,
    )


def _maker_checker_request(row: MakerCheckerRow) -> MakerCheckerRequest:
    return MakerCheckerRequest(
        id=row.id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        operation=row.operation,
        payload=row.payload,
        maker_id=row.maker_id,
        checker_id=row.checker_id,
        state=row.state,
        created_at=row.created_at,
        decided_at=row.decided_at,
        reason=row.reason,
        version=row.version,
    )


def _payload_uuid(payload: Mapping[str, object], key: str) -> UUID:
    value = payload.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValidationError(
                f"Approval payload field {key!r} must be a UUID.", field=key
            ) from error
    raise ValidationError(f"Approval payload field {key!r} must be a UUID.", field=key)


def _coerce_uuid(value: UUID | str, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValidationError(f"{field} must be a UUID.", field=field) from error
    raise ValidationError(f"{field} must be a UUID.", field=field)


def _new_version_row(
    *,
    covenant_id: UUID,
    version_no: int,
    terms: CovenantVersionTerms,
    status: str,
    registered_by_id: UUID,
    now: datetime,
    request_id: str,
    actor_id: UUID | None,
) -> CovenantVersion:
    return CovenantVersion(
        id=new_id(),
        covenant_id=covenant_id,
        version_no=version_no,
        definition_ref=terms.definition_ref,
        custom_formula=terms.custom_formula,
        threshold=terms.threshold,
        direction=terms.direction,
        unit=terms.unit,
        frequency=terms.frequency,
        test_basis=terms.test_basis,
        effective_from=terms.effective_from,
        effective_to=terms.effective_to,
        warning_headroom_pct=terms.warning_headroom_pct,
        cure_days=terms.cure_days,
        grace_days=terms.grace_days,
        source_document_id=terms.source_document_id,
        source_span_id=terms.source_span_id,
        status=status,
        tested_at_least_once=False,
        registered_by_id=registered_by_id,
        approved_by_id=None,
        created_at=now,
        updated_at=now,
        created_by_id=actor_id,
        updated_by_id=actor_id,
        request_id=request_id,
    )


def _required_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} is required.", field=field)
    clean = value.strip()
    if not clean:
        raise ValidationError(f"{field} is required.", field=field)
    if len(clean) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ValidationError(f"{field} contains an invalid control character.", field=field)
    return clean


def _clean_reference(value: object, field: str, *, maximum: int) -> str:
    return _required_text(value, field, maximum=maximum)


def _validated_exception_threshold(
    value: object | None,
    *,
    base_threshold: object,
    direction: object,
) -> Decimal:
    """Validate that a supplied exception threshold actually relaxes terms."""
    if value is None:
        raise ValidationError(
            "covenant_exception.relaxed_threshold is required.",
            field="covenant_exception.relaxed_threshold",
        )
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError(
            "covenant_exception.relaxed_threshold must be a finite Decimal or null.",
            field="covenant_exception.relaxed_threshold",
        )
    if not isinstance(base_threshold, Decimal) or not base_threshold.is_finite():
        raise ValidationError(
            "The covenant version has an invalid threshold.",
            field="covenant_version.threshold",
        )
    if direction == "max" and value < base_threshold:
        raise ValidationError(
            "A max covenant exception must not lower its threshold.",
            field="covenant_exception.relaxed_threshold",
        )
    if direction == "min" and value > base_threshold:
        raise ValidationError(
            "A min covenant exception must not raise its threshold.",
            field="covenant_exception.relaxed_threshold",
        )
    return value


def _validate_date_window(
    from_date: object,
    to_date: object | None,
    *,
    field: str,
) -> tuple[date, date | None]:
    to_date_field = "covenant_waiver.to_date"
    if isinstance(from_date, datetime) or not isinstance(from_date, date):
        raise ValidationError(f"{field} must be a calendar date.", field=field)
    if to_date is not None and (isinstance(to_date, datetime) or not isinstance(to_date, date)):
        raise ValidationError(
            f"{to_date_field} must be a calendar date or null.", field=to_date_field
        )
    if to_date is not None and to_date < from_date:
        raise ValidationError(
            "covenant_waiver.to_date must not precede from_date.",
            field="covenant_waiver.to_date",
        )
    return from_date, to_date


def _optional_text(value: object | None, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, maximum=maximum)


def _validated_optional_uuid(value: object | None, field: str) -> None:
    if value is not None and not isinstance(value, UUID):
        raise ValidationError(f"{field} must be a UUID or null.", field=field)


__all__ = [
    "AmendedCovenant",
    "AuditWriter",
    "COVENANT_AMENDMENT_OPERATION",
    "COVENANT_REGISTRATION_OPERATION",
    "COVENANT_RETIREMENT_OPERATION",
    "COVENANT_WAIVER_OPERATION",
    "RECORD_WAIVER_PERMISSION",
    "REGISTER_COVENANT_PERMISSION",
    "RegisteredCovenant",
    "RetiredCovenant",
    "RegistryService",
    "SqlAlchemyMakerCheckerRepository",
    "VIEW_COVENANT_PERMISSION",
]
