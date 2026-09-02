"""Intake services: candidate detection (`T-093`) and, from `T-096`, proposal
persistence, correction, abandonment and the confirm/submit lifecycle
(`spec §R-06`, `C-05`, `C-06`).

`IntakeDetectionService` (`T-093`) is the scoped bridge from a document's
already-extracted pages to the deterministic detector in `domain.intake`.
Detection is recomputed on every call rather than persisted: nothing here
decides that a piece of text *is* a covenant, so there is no confirmed state
to store, and a re-extraction or a page correction is reflected immediately
rather than through a second, staleness-prone table — the same reasoning
`DocumentService.classify_document` already follows for classification.

`IntakeService` (`T-096`) is the layer above it: it never calls a model
provider and never runs candidate detection itself — `ai/intake.py`
(`T-094`) and `IntakeDetectionService` already own those steps, and
`ai/shapes.verify_stage1_proposal` (`T-095`) already owns deciding whether a
proposal passed. What `IntakeService` owns is what happens to a proposal
once it exists: persisting it with its verification results, re-verifying a
correction from scratch rather than trusting the prior verdict, offering an
existing covenant on the same facility as an amendment instead of a
duplicate, retaining an abandoned proposal as evidence, and refusing —
structurally, for every role — to confirm one that failed. `spec §16.1`
marks confirming a failed proposal as permitted to no role in any
configuration; `IntakeService.submit` is where that refusal actually lives,
not merely where a confirm control happens not to render.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.ai.shapes import (
    SecurityAuditEvent,
    Stage1VerificationOutcome,
    verify_stage1_proposal,
)
from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.document import DocumentPage, DocumentSpan
from covenant_radar.db.models.intake import CovenantProposal
from covenant_radar.db.repositories.covenant import CovenantRepository, CovenantVersionRepository
from covenant_radar.db.repositories.document import DocumentRepository
from covenant_radar.db.repositories.proposal import ProposalRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.documents.ocr import is_history_span
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.domain.intake.candidates import (
    CandidateLine,
    CandidatePage,
    DetectionResult,
    detect_candidates,
)
from covenant_radar.domain.intake.proposal import StageOneProposal
from covenant_radar.domain.intake.verify import (
    CheckOutcome,
    VerificationCheckName,
    VerificationContext,
    VerificationReport,
)
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize
from covenant_radar.services.registry import AuditWriter, RegistryService

_MAX_DETECTION_PAGES = 500


class IntakeDetectionService:
    """Scoped, read-only clause-candidate detection for one document."""

    def __init__(self, session: Session) -> None:
        if not is_database_session(session):
            raise TypeError("IntakeDetectionService requires a SQLAlchemy Session.")
        self.session = session
        self.documents = DocumentRepository(session)

    def detect_candidates(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> DetectionResult:
        """Run clause-candidate detection over one scoped document's pages.

        Every page is loaded, including one flagged ``needs_review`` — the
        domain detector excludes it on its own, the same defensive recheck
        `DocumentService.list_detection_pages` already applies at its layer,
        so a caller cannot accidentally reach a page nobody has confirmed by
        forgetting to filter it out first.
        """
        resolved_scope = self._read_context(principal, scope)
        document = self.documents.get(document_id, scope=resolved_scope)
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        return detect_candidates(self._candidate_pages(document.id))

    def _candidate_pages(self, document_id: UUID) -> tuple[CandidatePage, ...]:
        page_rows = tuple(
            self.session.scalars(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_number)
                .limit(_MAX_DETECTION_PAGES)
            ).all()
        )
        span_rows = tuple(
            self.session.scalars(
                select(DocumentSpan)
                .where(DocumentSpan.document_id == document_id)
                .order_by(DocumentSpan.page_number, DocumentSpan.start_offset, DocumentSpan.id)
            ).all()
        )
        lines_by_page: dict[int, list[DocumentSpan]] = {}
        for row in span_rows:
            if is_history_span(row.span_type):
                continue
            lines_by_page.setdefault(row.page_number, []).append(row)

        return tuple(
            CandidatePage(
                page_number=page.page_number,
                text=page.text,
                needs_review=page.needs_review,
                lines=tuple(
                    CandidateLine(
                        page_number=row.page_number,
                        start_offset=row.start_offset,
                        end_offset=row.end_offset,
                        text=row.text,
                    )
                    for row in lines_by_page.get(page.page_number, ())
                ),
            )
            for page in page_rows
        )

    def _read_context(self, principal: Principal, scope: Scope | None) -> Scope:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.VIEW_DOCUMENT)
        resolved = scope if scope is not None else resolve_scope(principal, self.session)
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The resolved scope does not belong to the authenticated principal."
            )
        return resolved


# ---------------------------------------------------------------------------
# T-096: proposal persistence, correction, abandonment and confirm/submit.
# ---------------------------------------------------------------------------

_ABANDON_REASON_MAX_LENGTH = 2_000

#: A covenant-version status this build still treats as "the covenant this
#: definition already lives on" for amendment detection — everything short
#: of `retired`/`superseded`, which free the definition to be registered
#: fresh rather than offered as an amendment of a covenant no longer in force.
_AMENDABLE_VERSION_STATUSES = frozenset({"draft", "pending_approval", "live"})


class ProposalVerificationFailed(Conflict):
    """A submit attempt against a proposal that failed one or more of the
    six code verifications or the injection-shaped-input scan.

    `spec §16.1` marks confirming a covenant that failed verification as
    permitted to **no role in any configuration**. This is the structural
    enforcement of that rule: `IntakeService.submit` raises it
    unconditionally, from the proposal's own persisted verdict, before any
    covenant-creation code runs and independent of whatever permission the
    caller holds — a confirm control not rendering is necessary but never
    sufficient on its own.
    """

    code: ClassVar[str] = "proposal_verification_failed"

    def __init__(self, message: str, *, failed_checks: tuple[str, ...]) -> None:
        super().__init__(message)
        self.failed_checks = failed_checks


@dataclass(frozen=True, slots=True)
class ProposedClause:
    """One already-produced stage-1 proposal (`ai/intake.py`, `T-094`),
    paired with the document span it was extracted from — `None` for a
    hand-entered clause with no uploaded document."""

    proposal: StageOneProposal
    source_span_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, StageOneProposal):
            raise TypeError("ProposedClause.proposal must be a StageOneProposal.")
        if self.source_span_id is not None and not isinstance(self.source_span_id, UUID):
            raise TypeError("ProposedClause.source_span_id must be a UUID or None.")


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    """One persisted proposal and its full verdict, so a caller can read
    every reason a proposal passed or failed without re-running
    verification against it."""

    row: CovenantProposal
    outcome: Stage1VerificationOutcome


@dataclass(frozen=True, slots=True)
class SubmittedProposal:
    """What `IntakeService.submit` returns: the confirmed proposal and the
    registry outcome — a fresh registration or an amendment — it produced."""

    row: CovenantProposal
    covenant: Covenant
    version: CovenantVersion
    approval_request: object | None
    was_amendment: bool


class IntakeService:
    """Persist stage-1 proposals with their verification results, and own
    the correct/abandon/submit lifecycle `spec §R-06`, `C-05` and `C-06`
    require.

    Every proposal this service persists or re-verifies is handed to it
    already parsed as a `StageOneProposal` — this service never calls a
    model provider and never runs candidate detection itself. It always
    re-runs `ai.shapes.verify_stage1_proposal` itself against a caller-
    supplied `VerificationContext` rather than trusting a caller-supplied
    verdict: a forged or stale "passed" outcome is exactly what `spec
    §R-06.b`'s re-verification requirement exists to make impossible.

    ``registry`` composes `RegistryService` (`T-033`) rather than
    reimplementing covenant creation or its maker-checker routing: `submit`
    calls `register`/`amend` and reports back exactly the draft/live/
    pending-approval status those methods already decide.
    """

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        registry: RegistryService,
        clock: Clock | None = None,
        request_id: str | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("IntakeService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("IntakeService requires an append-only audit writer.")
        if not isinstance(registry, RegistryService):
            raise TypeError("IntakeService requires a RegistryService.")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("IntakeService scope_resolver must be callable.")
        self.session = session
        self.audit = audit
        self.registry = registry
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 40:
            raise ValueError("Intake request_id must be between 1 and 40 characters.")
        self.scope_resolver = scope_resolver
        self.proposals = ProposalRepository(session, audit=audit)
        self.covenants = CovenantRepository(session, audit=audit)
        self.versions = CovenantVersionRepository(session, audit=audit)

    # ---- use cases -------------------------------------------------------

    def propose_from_document(
        self,
        principal: Principal,
        *,
        facility_id: UUID,
        clauses: Sequence[ProposedClause],
        context: VerificationContext,
        document_id: UUID | None = None,
        force_reextraction: bool = False,
        scope: Scope | None = None,
    ) -> tuple[ProposalRecord, ...]:
        """Verify and persist every proposed clause, unless this document
        already carries recorded proposals and re-extraction was not
        explicitly requested — in which case those prior proposals are
        returned as-is rather than re-extracted (`spec §R-06`'s duplicate-
        submission case).
        """
        resolved_scope = self._write_context(principal, scope, permission=Permission.RUN_INTAKE)
        if not isinstance(facility_id, UUID):
            raise ValidationError("facility_id must be a UUID.", field="facility_id")
        if not isinstance(context, VerificationContext):
            raise TypeError("propose_from_document requires a VerificationContext.")
        if isinstance(clauses, str | bytes) or not isinstance(clauses, Sequence):
            raise TypeError("propose_from_document requires a sequence of ProposedClause values.")
        for clause in clauses:
            if not isinstance(clause, ProposedClause):
                raise TypeError("propose_from_document requires ProposedClause values only.")
        if not clauses:
            raise ValidationError(
                "propose_from_document requires at least one clause.", field="clauses"
            )

        if document_id is not None and not force_reextraction:
            existing_rows = self.proposals.for_document(document_id, scope=resolved_scope)
            if existing_rows:
                return tuple(
                    ProposalRecord(row=row, outcome=_row_to_outcome(row)) for row in existing_rows
                )

        actor_id = self._actor_id(principal)
        now = self._now()
        records: list[ProposalRecord] = []
        for clause in clauses:
            outcome = verify_stage1_proposal(clause.proposal, context)
            row = CovenantProposal(
                id=new_id(),
                facility_id=facility_id,
                document_id=document_id,
                source_span_id=clause.source_span_id,
                created_at=now,
                updated_at=now,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                request_id=self.request_id,
                status="open",
            )
            _apply_proposal_fields(row, clause.proposal, outcome)
            self.proposals.add(row)
            self.session.flush()
            self._audit_proposal(
                AuditEventType.INTAKE_PROPOSAL_CREATED.value, row, principal, outcome
            )
            records.append(ProposalRecord(row=row, outcome=outcome))
        return tuple(records)

    def proposals_for_document(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> tuple[ProposalRecord, ...]:
        """Return every proposal already recorded for one in-scope document."""
        resolved_scope = self._read_context(principal, scope)
        rows = self.proposals.for_document(document_id, scope=resolved_scope)
        return tuple(ProposalRecord(row=row, outcome=_row_to_outcome(row)) for row in rows)

    def proposal(
        self,
        principal: Principal,
        proposal_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> ProposalRecord:
        """Return one in-scope proposal with its persisted verification.

        The browser intake adapter uses this read boundary for refreshes and
        submit-error rendering.  It deliberately reconstructs the outcome
        from the stored verdict columns instead of re-running verification;
        only ``correct`` is allowed to change a proposal's verdict.
        """
        resolved_scope = self._read_context(principal, scope)
        if not isinstance(proposal_id, UUID):
            raise ValidationError("proposal_id must be a UUID.", field="proposal_id")
        row = self.proposals.get(proposal_id, scope=resolved_scope)
        if row is None:
            raise NotFound(f"Proposal {proposal_id} was not found within the current scope.")
        return ProposalRecord(row=row, outcome=_row_to_outcome(row))

    get_proposal = proposal

    def correct(
        self,
        principal: Principal,
        proposal_id: UUID,
        *,
        proposal: StageOneProposal,
        context: VerificationContext,
        scope: Scope | None = None,
    ) -> ProposalRecord:
        """Apply a corrected field and unconditionally re-run all six
        checks plus the injection scan — never the prior verdict, however
        confident it looked, per `spec §R-06`'s re-verification rule."""
        resolved_scope = self._write_context(principal, scope, permission=Permission.RUN_INTAKE)
        if not isinstance(proposal_id, UUID):
            raise ValidationError("proposal_id must be a UUID.", field="proposal_id")
        if not isinstance(proposal, StageOneProposal):
            raise TypeError("correct requires a StageOneProposal.")
        if not isinstance(context, VerificationContext):
            raise TypeError("correct requires a VerificationContext.")
        row = self.proposals.by_id_for_update(proposal_id, scope=resolved_scope)
        if row is None:
            raise NotFound(f"Proposal {proposal_id} was not found within the current scope.")
        if row.status != "open":
            raise Conflict(
                f"Proposal {proposal_id} is {row.status}; only an open proposal can be corrected."
            )

        outcome = verify_stage1_proposal(proposal, context)
        _apply_proposal_fields(row, proposal, outcome)
        row.updated_at = self._now()
        row.updated_by_id = self._actor_id(principal)
        row.version += 1
        self.session.flush()
        self._audit_proposal(
            AuditEventType.INTAKE_PROPOSAL_CORRECTED.value,
            row,
            principal,
            outcome,
            extra={"all_checks_rerun": True},
        )
        return ProposalRecord(row=row, outcome=outcome)

    def abandon(
        self,
        principal: Principal,
        proposal_id: UUID,
        *,
        reason: str | None = None,
        scope: Scope | None = None,
    ) -> ProposalRecord:
        """Abandon one open proposal, retaining it — never deleting it —
        with its verification results, since a rejected proposal is itself
        evidence about the source document."""
        resolved_scope = self._write_context(principal, scope, permission=Permission.RUN_INTAKE)
        if not isinstance(proposal_id, UUID):
            raise ValidationError("proposal_id must be a UUID.", field="proposal_id")
        row = self.proposals.by_id_for_update(proposal_id, scope=resolved_scope)
        if row is None:
            raise NotFound(f"Proposal {proposal_id} was not found within the current scope.")
        if row.status != "open":
            raise Conflict(
                f"Proposal {proposal_id} is {row.status}; only an open proposal can be abandoned."
            )
        validated_reason = _optional_text(
            reason, "proposal.abandon_reason", maximum=_ABANDON_REASON_MAX_LENGTH
        )
        row.status = "abandoned"
        row.abandon_reason = validated_reason
        row.updated_at = self._now()
        row.updated_by_id = self._actor_id(principal)
        row.version += 1
        self.session.flush()
        self._audit_proposal(
            AuditEventType.INTAKE_PROPOSAL_ABANDONED.value,
            row,
            principal,
            _row_to_outcome(row),
            extra={"reason": validated_reason},
        )
        return ProposalRecord(row=row, outcome=_row_to_outcome(row))

    def find_amendment_target(
        self,
        principal: Principal,
        proposal_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> Covenant | None:
        """Return the covenant this proposal's definition already lives on
        for this facility, if any — what a submit of this proposal would
        amend rather than duplicate."""
        resolved_scope = self._read_context(principal, scope)
        row = self.proposals.get(proposal_id, scope=resolved_scope)
        if row is None:
            raise NotFound(f"Proposal {proposal_id} was not found within the current scope.")
        return self._existing_covenant_for_definition(
            row.facility_id, row.definition_ref, scope=resolved_scope
        )

    def submit(
        self,
        principal: Principal,
        proposal_id: UUID,
        *,
        test_basis: str,
        reference: str | None = None,
        name: str | None = None,
        covenant_class: str | None = None,
        scope: Scope | None = None,
    ) -> SubmittedProposal:
        """Confirm one proposal: register a new covenant, or amend the
        covenant this definition already lives on for this facility.

        Refuses a proposal that did not pass every one of the six code
        verifications and the injection scan — unconditionally, before the
        covenant-creation path below is ever reached, and independent of
        the caller's own permissions. `test_basis` is always required
        because no stage-1 field carries it; `reference`/`name`/
        `covenant_class` are required only when registering a fresh
        covenant — amending an existing one needs none of them.
        """
        resolved_scope = self._write_context(
            principal, scope, permission=Permission.REGISTER_COVENANT
        )
        if not isinstance(proposal_id, UUID):
            raise ValidationError("proposal_id must be a UUID.", field="proposal_id")
        row = self.proposals.by_id_for_update(proposal_id, scope=resolved_scope)
        if row is None:
            raise NotFound(f"Proposal {proposal_id} was not found within the current scope.")

        # The structural refusal (`spec §16.1`): every failed check is
        # named, and nothing below this point can ever run for a proposal
        # that did not pass, regardless of the caller's role or credential.
        if not row.all_passed:
            failed_checks = _failed_checks(row)
            raise ProposalVerificationFailed(
                f"Proposal {proposal_id} failed verification and cannot be confirmed: "
                f"{', '.join(failed_checks)}.",
                failed_checks=failed_checks,
            )
        if row.status != "open":
            raise Conflict(
                f"Proposal {proposal_id} is {row.status}; only an open proposal can be submitted."
            )
        test_basis = _required_text(test_basis, "covenant_version.test_basis", maximum=20)

        # `all_passed` guarantees these six checks already passed, so every
        # field a passing proposal needs is present; a custom formula can
        # never reach `all_passed` in this build (`domain/intake/verify.py`
        # `_check_recomputable`), so `definition_ref` is always set here.
        assert row.definition_ref is not None
        assert row.threshold is not None
        assert row.direction is not None
        assert row.frequency is not None
        assert row.effective_from is not None
        definition = LIBRARY.get(row.definition_ref)
        if definition is None:  # pragma: no cover - unreachable given all_passed
            raise Conflict(f"Proposal {proposal_id} names no known ratio definition.")

        terms = CovenantVersionTerms(
            definition_ref=row.definition_ref,
            custom_formula=None,
            threshold=row.threshold,
            direction=row.direction,
            unit=definition.unit,
            frequency=row.frequency,
            test_basis=test_basis,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            cure_days=row.cure_period_days,
            source_document_id=row.document_id,
            source_span_id=row.source_span_id,
        )

        existing_covenant = self._existing_covenant_for_definition(
            row.facility_id, row.definition_ref, scope=resolved_scope
        )
        was_amendment = existing_covenant is not None
        if existing_covenant is not None:
            if reference is not None and reference != existing_covenant.reference:
                raise ValidationError(
                    f"{row.definition_ref!r} already exists on this facility as "
                    f"{existing_covenant.reference!r}; submit without a reference to amend it.",
                    field="reference",
                )
            amended = self.registry.amend(
                principal,
                existing_covenant.reference,
                terms=terms,
                scope=resolved_scope,
            )
            covenant, version, approval_request = (
                existing_covenant,
                amended.version,
                amended.approval_request,
            )
        else:
            missing = [
                field_name
                for field_name, value in (
                    ("reference", reference),
                    ("name", name),
                    ("covenant_class", covenant_class),
                )
                if value is None
            ]
            if missing:
                raise ValidationError(
                    "Registering a new covenant from this proposal requires: "
                    f"{', '.join(missing)}.",
                    field=missing[0],
                )
            assert reference is not None
            assert name is not None
            assert covenant_class is not None
            registered = self.registry.register(
                principal,
                facility_id=row.facility_id,
                reference=reference,
                name=name,
                covenant_class=covenant_class,
                terms=terms,
                scope=resolved_scope,
            )
            covenant, version, approval_request = (
                registered.covenant,
                registered.version,
                registered.approval_request,
            )

        row.status = "confirmed"
        row.covenant_id = covenant.id
        row.covenant_version_id = version.id
        row.updated_at = self._now()
        row.updated_by_id = self._actor_id(principal)
        row.version += 1
        self.session.flush()
        self._audit_proposal(
            AuditEventType.INTAKE_PROPOSAL_CONFIRMED.value,
            row,
            principal,
            _row_to_outcome(row),
            extra={
                "covenant_id": str(covenant.id),
                "version_id": str(version.id),
                "was_amendment": was_amendment,
            },
        )
        return SubmittedProposal(
            row=row,
            covenant=covenant,
            version=version,
            approval_request=approval_request,
            was_amendment=was_amendment,
        )

    # ---- internal helpers --------------------------------------------------

    def _existing_covenant_for_definition(
        self,
        facility_id: UUID,
        definition_ref: str | None,
        *,
        scope: Scope,
    ) -> Covenant | None:
        if definition_ref is None:
            return None
        for covenant in self.covenants.list(scope=scope):
            if covenant.facility_id != facility_id or not covenant.is_active:
                continue
            latest = self.versions.latest_for_covenant(covenant.id, scope=scope)
            if (
                latest is not None
                and latest.definition_ref == definition_ref
                and latest.status in _AMENDABLE_VERSION_STATUSES
            ):
                return covenant
        return None

    def _audit_proposal(
        self,
        event_type: str,
        row: CovenantProposal,
        principal: Principal,
        outcome: Stage1VerificationOutcome,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "facility_id": str(row.facility_id),
            "document_id": str(row.document_id) if row.document_id is not None else None,
            "status": row.status,
            "all_passed": outcome.all_passed,
            "failed_checks": list(outcome.failed_checks),
            "injection_detected": outcome.injection_detected,
        }
        if extra:
            payload.update(extra)
        self.audit.record(
            event_type,
            ("covenant_proposal", row.id),
            payload,
            actor=principal.id,
            request_id=self.request_id,
        )
        if outcome.security_event is not None:
            self.audit.record(
                outcome.security_event.event_type,
                ("covenant_proposal", row.id),
                {
                    "detail": outcome.security_event.detail,
                    "matched_patterns": list(outcome.security_event.matched_patterns),
                    "excerpt": outcome.security_event.excerpt,
                },
                actor=principal.id,
                request_id=self.request_id,
            )

    def _read_context(
        self,
        principal: Principal,
        scope: Scope | None,
        *,
        permission: Permission = Permission.RUN_INTAKE,
    ) -> Scope:
        self._require_principal(principal, permission)
        return self._validated_scope(principal, scope)

    def _write_context(
        self,
        principal: Principal,
        scope: Scope | None,
        *,
        permission: Permission,
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
    def _actor_id(principal: Principal) -> UUID | None:
        """`covenant_proposal.created_by_id`/`updated_by_id` are foreign
        keys to `app_user`, so only a session-user principal's id is ever
        stored; an API-key principal's actions are still fully audited
        through `_audit_proposal`'s `actor=principal.id`, just not attributed
        to a user row that does not exist for it."""
        return principal.id if principal.kind is PrincipalKind.USER else None

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Intake clock must return an aware datetime.")
        return now.astimezone(UTC)


def _apply_proposal_fields(
    row: CovenantProposal,
    proposal: StageOneProposal,
    outcome: Stage1VerificationOutcome,
) -> None:
    """Write one `StageOneProposal` plus its `Stage1VerificationOutcome`
    onto a `CovenantProposal` row, used identically at creation and at
    correction time so a correction can never leave a stale field behind."""
    row.clause_text = proposal.candidate.text
    row.content_hash = _content_hash(proposal.candidate.text)
    row.raw_reply = proposal.raw_reply
    row.parseable = proposal.parseable
    row.parse_error = proposal.parse_error
    row.definition_ref = proposal.definition_ref
    row.custom_formula = proposal.custom_formula
    row.threshold = proposal.threshold
    row.threshold_ambiguous = proposal.threshold_ambiguous
    row.unit = proposal.unit
    row.currency = proposal.currency
    row.direction = proposal.direction
    row.frequency = proposal.frequency
    row.frequency_ambiguous = proposal.frequency_ambiguous
    row.effective_from = proposal.effective_from
    row.effective_to = proposal.effective_to
    row.exceptions = list(proposal.exceptions)
    row.cure_period_days = proposal.cure_period_days
    row.source_quote = proposal.source_quote
    row.checks = _report_to_json(outcome.verification)
    row.all_passed = outcome.all_passed
    row.injection_detected = outcome.injection_detected
    row.security_event = _security_event_to_json(outcome.security_event)
    row.refusal_message = outcome.refusal_message


def _content_hash(text: str) -> str:
    normalised = " ".join(text.split()).strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _report_to_json(report: VerificationReport) -> list[dict[str, object]]:
    return [
        {"check": outcome.check.value, "passed": outcome.passed, "detail": outcome.detail}
        for outcome in report.checks
    ]


def _json_to_report(data: Sequence[Mapping[str, object]]) -> VerificationReport:
    checks = tuple(
        CheckOutcome(
            check=VerificationCheckName(cast(str, entry["check"])),
            passed=cast(bool, entry["passed"]),
            detail=cast(str, entry["detail"]),
        )
        for entry in data
    )
    return VerificationReport(checks=checks)


def _security_event_to_json(event: SecurityAuditEvent | None) -> dict[str, object] | None:
    if event is None:
        return None
    return {
        "event_type": event.event_type,
        "detail": event.detail,
        "matched_patterns": list(event.matched_patterns),
        "excerpt": event.excerpt,
    }


def _json_to_security_event(data: Mapping[str, object] | None) -> SecurityAuditEvent | None:
    if data is None:
        return None
    return SecurityAuditEvent(
        event_type=cast(str, data["event_type"]),
        detail=cast(str, data["detail"]),
        matched_patterns=tuple(cast(Sequence[str], data["matched_patterns"])),
        excerpt=cast(str, data["excerpt"]),
    )


def _row_to_outcome(row: CovenantProposal) -> Stage1VerificationOutcome:
    return Stage1VerificationOutcome(
        verification=_json_to_report(cast(Sequence[Mapping[str, object]], row.checks)),
        injection_detected=row.injection_detected,
        security_event=_json_to_security_event(row.security_event),
        refusal_message=row.refusal_message,
    )


def _failed_checks(row: CovenantProposal) -> tuple[str, ...]:
    return tuple(
        cast(str, entry["check"])
        for entry in cast(Sequence[Mapping[str, object]], row.checks)
        if not cast(bool, entry["passed"])
    )


def _optional_text(value: object | None, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text or null.", field=field)
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    return cleaned


def _required_text(value: object, field: str, *, maximum: int) -> str:
    cleaned = _optional_text(value, field, maximum=maximum)
    if cleaned is None:
        raise ValidationError(f"{field} is required.", field=field)
    return cleaned


__all__ = [
    "IntakeDetectionService",
    "IntakeService",
    "ProposalRecord",
    "ProposalVerificationFailed",
    "ProposedClause",
    "SubmittedProposal",
]
