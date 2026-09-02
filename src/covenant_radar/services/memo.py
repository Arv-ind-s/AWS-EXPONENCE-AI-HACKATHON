"""Memo assembly (`T-099`) and grounded-memo persistence/refusal (`T-101`).

`MemoAssemblyService` is intentionally upstream of the model call.  It
accepts only explicitly referenced record DTOs and selects values already
present in those records.  It does not calculate ratios, probabilities,
headroom, evidence counts or simulation effects.  Those facts must be
produced and persisted by their owning stages before this service is called.

`MemoGenerationService` (`T-101`) is the layer above `ai.memo.draft_memo`
(`T-100`).  It never talks to a model provider directly and never decides
whether a draft passed — `ai.memo.draft_memo` and its stage-7 shape checks
already own that.  What this service owns is what happens to a draft once
`draft_memo` has a verdict: a passed draft is persisted with its slot map,
drafted text, actions and versions, in one transaction with its stage-7
trace row; a refused draft (`ai.memo.MemoShapeRefusal`, raised after the
one permitted regeneration) writes no memo row at all — `spec §R-17.b`'s
"no record at all" is enforced by never constructing the row, not by
deleting one afterwards — while still leaving a traced, audited answer to
"why is there no memo". A provider outage or a model-call ceiling is
returned as a typed outcome rather than raised, so a caller can render its
banner or degraded message without an unhandled exception taking the rest
of the screen down with it (`spec §R-17.c`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.ai.budget import CeilingReached
from covenant_radar.ai.client import ModelClient
from covenant_radar.ai.errors import ModelGovernanceBlocked, ProviderUnavailable
from covenant_radar.ai.memo import (
    PROMPT_VERSION,
    MemoDraftingResult,
    MemoShapeRefusal,
    draft_memo,
)
from covenant_radar.ai.prompts.loader import PromptLoader
from covenant_radar.ai.shapes import CatalogueAction, normalise_catalogue_actions
from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, NotFound
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.workflow import Memo, MemoExport
from covenant_radar.db.repositories.memo import MemoRepository
from covenant_radar.db.repositories.trace import TraceRepository, TraceSubject
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.documents.render import (
    MemoExportContext,
    MemoLetterhead,
    MemoRenderer,
    memo_integrity_hash,
)
from covenant_radar.domain.memo.slots import (
    NO_SIMULATIONS_VALUE_TEXT,
    MemoRecord,
    MemoRecords,
    MemoSlot,
    MemoSlotMap,
    RecordReference,
    SlotState,
    absent_slot,
    present_slot,
    suppressed_slot,
)
from covenant_radar.domain.memo.template import DEFAULT_MEMO_TEMPLATE, MemoTemplate
from covenant_radar.domain.trace import Decider, TraceStage, stage_record
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize
from covenant_radar.services.registry import AuditWriter

_SITUATION_FIELDS: Final[tuple[str, ...]] = ("situation", "text", "summary")
_RATIO_NAME_FIELDS: Final[tuple[str, ...]] = ("ratio_name", "covenant_name", "name")
_VALUE_FIELDS: Final[tuple[str, ...]] = ("value", "current_value", "observed_value")
_THRESHOLD_FIELDS: Final[tuple[str, ...]] = ("threshold", "contractual_threshold")
_HEADROOM_FIELDS: Final[tuple[str, ...]] = ("headroom", "headroom_pct")
_CROSSING_DATE_FIELDS: Final[tuple[str, ...]] = (
    "crossing_date",
    "projected_cross_date",
)
_PROBABILITY_SUPPRESSION_FIELDS: Final[tuple[str, ...]] = (
    "probability_suppression_reason",
    "suppression_reason",
    "confidence_reason",
)
_DRIVER_NAME_FIELDS: Final[tuple[str, ...]] = ("name", "driver_name")
_EVIDENCE_COUNT_FIELDS: Final[tuple[str, ...]] = ("count", "evidence_count", "evidence_counts")
_EVIDENCE_CITATION_FIELDS: Final[tuple[str, ...]] = (
    "citation",
    "evidence_citation",
    "evidence_id",
)
_SIMULATION_CODE_FIELDS: Final[tuple[str, ...]] = ("code", "intervention_code")
_RECOMMENDATION_TEXT_FIELDS: Final[tuple[str, ...]] = ("text", "intervention_text")
_MAX_TEXT_LENGTH: Final[int] = 10_000


class MemoAssemblyService:
    """Assemble a complete, fixed-template slot map from stored facts."""

    def __init__(self, *, template: MemoTemplate = DEFAULT_MEMO_TEMPLATE) -> None:
        if not isinstance(template, MemoTemplate):
            raise TypeError("MemoAssemblyService.template must be a MemoTemplate.")
        if not template.is_fixed:
            raise ValueError("Memo assembly requires the fixed T-099 memo template.")
        self.template = template

    def assemble(self, records: MemoRecords | Mapping[str, object] | object) -> MemoSlotMap:
        """Build every fixed slot, including explicit degraded states.

        The method only selects or packages values supplied by ``records``.
        In particular, it never uses collection length as an evidence count,
        and it never turns an absent nullable fact into numeric zero.
        """

        source = MemoRecords.from_value(records)
        slots = [self._situation(source.situation)]
        slots.extend(self._covenant_position(source.covenant_position))
        slots.extend(
            (
                self._drivers(source.drivers),
                self._evidence(source.evidence),
                self._simulations(source.simulations),
                self._recommendations(source.recommendations),
                self._intervention_text(source.recommendations),
            )
        )
        result = MemoSlotMap(tuple(slots), template_version=self.template.version)
        if result.slot_names != self.template.slot_names:
            raise AssertionError("Memo assembly produced a slot map outside its fixed template.")
        return result

    assemble_slots = assemble
    build = assemble

    def _situation(self, record: MemoRecord | None) -> MemoSlot:
        if record is None:
            return absent_slot("situation", "the situation record is absent")
        value = _required_value(record, "situation", _SITUATION_FIELDS)
        return present_slot("situation", _required_text(value, "situation"), (record.reference,))

    def _covenant_position(self, record: MemoRecord | None) -> tuple[MemoSlot, ...]:
        names = (
            ("ratio_name", _RATIO_NAME_FIELDS),
            ("value", _VALUE_FIELDS),
            ("threshold", _THRESHOLD_FIELDS),
            ("headroom", _HEADROOM_FIELDS),
            ("confidence", ("confidence",)),
            ("crossing_date", _CROSSING_DATE_FIELDS),
        )
        if record is None:
            return tuple(
                absent_slot(slot_name, "the covenant position record is absent")
                for slot_name, _aliases in names
            ) + (absent_slot("probability", "the forecast record is absent"),)

        slots: list[MemoSlot] = []
        for slot_name, aliases in names:
            value = _required_value(record, slot_name, aliases)
            if value is None:
                slots.append(
                    absent_slot(
                        slot_name,
                        f"the covenant position record has no {slot_name} value",
                        references=(record.reference,),
                    )
                )
            elif slot_name in {"ratio_name"}:
                slots.append(
                    present_slot(
                        slot_name,
                        _required_text(value, slot_name),
                        (record.reference,),
                    )
                )
            else:
                slots.append(present_slot(slot_name, value, (record.reference,)))

        slots.insert(4, self._probability(record))
        return tuple(slots)

    def _probability(self, record: MemoRecord) -> MemoSlot:
        suppressed = record.values.get("probability_suppressed", False)
        if not isinstance(suppressed, bool):
            raise ValueError("probability_suppressed must be a boolean when supplied.")
        if suppressed:
            supplied_probability = record.values.get("probability")
            if supplied_probability is not None:
                raise ValueError("A suppressed forecast record must not carry a probability value.")
            reason = _required_value(
                record,
                "probability suppression reason",
                _PROBABILITY_SUPPRESSION_FIELDS,
            )
            return suppressed_slot(
                "probability",
                _required_text(reason, "suppression reason"),
                (record.reference,),
            )
        value = _required_value(record, "probability", ("probability",))
        if value is None:
            return absent_slot(
                "probability",
                "the forecast record has no probability value",
                references=(record.reference,),
            )
        return present_slot("probability", value, (record.reference,))

    def _drivers(self, records: tuple[MemoRecord, ...]) -> MemoSlot:
        if not records:
            return absent_slot("drivers", "no driver records are available")
        values: list[dict[str, object]] = []
        for record in records:
            name = _required_text(
                _required_value(record, "driver name", _DRIVER_NAME_FIELDS),
                "driver name",
            )
            item: dict[str, object] = {"name": name}
            if "share" in record.values:
                item["share"] = record.values["share"]
            values.append(item)
        return present_slot("drivers", tuple(values), _references(records))

    def _evidence(self, records: tuple[MemoRecord, ...]) -> MemoSlot:
        if not records:
            return absent_slot("evidence_counts", "no evidence records are available")
        values: list[dict[str, object]] = []
        for record in records:
            citation = _required_value(record, "evidence citation", _EVIDENCE_CITATION_FIELDS)
            count = _required_value(record, "evidence count", _EVIDENCE_COUNT_FIELDS)
            if count is None:
                raise ValueError("evidence count must be present on every evidence record.")
            values.append(
                {
                    "citation": _required_text(citation, "evidence citation"),
                    "count": count,
                }
            )
        return present_slot("evidence_counts", tuple(values), _references(records))

    def _simulations(self, records: tuple[MemoRecord, ...]) -> MemoSlot:
        if not records:
            return absent_slot(
                "simulation_options",
                "no simulations are recorded for this borrower",
                value_text=NO_SIMULATIONS_VALUE_TEXT,
            )
        values: list[dict[str, object]] = []
        for record in records:
            code = _required_text(
                _required_value(record, "simulation intervention code", _SIMULATION_CODE_FIELDS),
                "simulation intervention code",
            )
            assumptions = _required_value(record, "simulation assumptions", ("assumptions",))
            if assumptions is None:
                raise ValueError(
                    "simulation assumptions must be present on every simulation record."
                )
            if isinstance(assumptions, str | bytes | bytearray) or not assumptions:
                raise ValueError(
                    "simulation assumptions must be a non-empty collection on every "
                    "simulation record."
                )
            values.append(
                {
                    "code": code,
                    "text": _optional_text(
                        record.values.get("text", record.values.get("intervention_text"))
                    ),
                    "projected_cross_date": record.values.get("projected_cross_date"),
                    "probability": record.values.get("probability"),
                    "delta_days": record.values.get("delta_days"),
                    "delta_probability": record.values.get("delta_probability"),
                    "assumptions": assumptions,
                }
            )
        return present_slot("simulation_options", tuple(values), _references(records))

    def _recommendations(self, records: tuple[MemoRecord, ...]) -> MemoSlot:
        if not records:
            return absent_slot(
                "recommended_interventions",
                "no recommended intervention records are available",
            )
        values: list[dict[str, object]] = []
        for record in records:
            code = _required_text(
                _required_value(record, "recommendation code", _SIMULATION_CODE_FIELDS),
                "recommendation code",
            )
            role_tag = _required_text(
                _required_value(record, "recommendation role tag", ("role_tag",)),
                "recommendation role tag",
            )
            text = _required_text(
                _required_value(record, "recommendation text", _RECOMMENDATION_TEXT_FIELDS),
                "recommendation text",
            )
            values.append(
                {
                    "code": code,
                    "role_tag": role_tag,
                    "text": text,
                    "requires_approval": record.values.get("requires_approval"),
                }
            )
        return present_slot("recommended_interventions", tuple(values), _references(records))

    def _intervention_text(self, records: tuple[MemoRecord, ...]) -> MemoSlot:
        if not records:
            return absent_slot(
                "intervention_text",
                "no recommended intervention records are available",
            )
        texts = tuple(
            _required_text(
                _required_value(record, "intervention text", _RECOMMENDATION_TEXT_FIELDS),
                "intervention text",
            )
            for record in records
        )
        return present_slot("intervention_text", texts, _references(records))


def assemble_memo_slots(
    records: MemoRecords | Mapping[str, object] | object,
    *,
    template: MemoTemplate = DEFAULT_MEMO_TEMPLATE,
) -> MemoSlotMap:
    """Functional entry point for callers that do not need a service object."""

    return MemoAssemblyService(template=template).assemble(records)


assemble_slots = assemble_memo_slots


def _required_value(record: MemoRecord, label: str, aliases: Iterable[str]) -> object:
    try:
        return record.value(*tuple(aliases))
    except KeyError as error:
        raise ValueError(f"{record.reference.record_type} record is missing {label}.") from error


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-blank text.")
    normalized = value.strip()
    if len(normalized) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{label} must be at most {_MAX_TEXT_LENGTH} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{label} contains a control character.")
    return normalized


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value, "simulation text")


def _references(records: Iterable[MemoRecord]) -> tuple[RecordReference, ...]:
    return tuple(record.reference for record in records)


MemoService = MemoAssemblyService


# ---------------------------------------------------------------------------
# T-101: refusal, retry and persistence rules (`spec §R-17.b`, `plan.md §5.8`).
# ---------------------------------------------------------------------------

_REQUEST_ID_MAX_LENGTH: Final[int] = 40
_MAX_AUDIT_REASON_LENGTH: Final[int] = 2_000
_MAX_PROVIDER_NAME_LENGTH: Final[int] = 50

DEGRADED_MEMO_MESSAGE: Final[str] = (
    "The memo drafting service is temporarily unavailable. Please try again "
    "shortly; everything else on this screen is unaffected."
)
MODEL_GOVERNANCE_MEMO_MESSAGE: Final[str] = (
    "AI memo drafting is blocked by model governance: the stage-7 memo component is not "
    "registered and approved for this production deployment. Ask a model-risk approver to "
    "approve the configured provider, model, and prompt version; the stored prediction and "
    "its deterministic explanation remain available."
)


class MemoOutcomeKind(StrEnum):
    """The four distinct results `MemoGenerationService.generate` can produce."""

    GENERATED = "generated"
    REFUSED = "refused"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CEILING_REACHED = "ceiling_reached"


@dataclass(frozen=True, slots=True)
class MemoGenerationOutcome:
    """What one `generate` call produced — never a partially built memo.

    Exactly one of the four `kind` values applies, and the fields that go
    with it are validated to match. Only `GENERATED` ever carries a
    persisted `Memo`: `spec §R-17.b`'s "no record at all" applies to a
    shape refusal, a provider outage and a call ceiling alike, so none of
    the other three kinds is ever constructed with one.
    """

    kind: MemoOutcomeKind
    memo: Memo | None = None
    drafting: MemoDraftingResult | None = None
    message: str | None = None
    failed_checks: tuple[str, ...] = ()
    retry_at: datetime | None = None
    dimension: str | None = None

    def __post_init__(self) -> None:
        kind = MemoOutcomeKind(self.kind)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "failed_checks", tuple(self.failed_checks))
        if kind is MemoOutcomeKind.GENERATED:
            if not isinstance(self.memo, Memo):
                raise ValueError("A generated outcome requires its persisted Memo.")
            if not isinstance(self.drafting, MemoDraftingResult):
                raise ValueError("A generated outcome requires its MemoDraftingResult.")
            if self.message is not None or self.failed_checks or self.retry_at is not None:
                raise ValueError("A generated outcome carries no failure detail.")
        else:
            if self.memo is not None or self.drafting is not None:
                raise ValueError(f"A {kind.value} outcome must not carry a persisted memo.")
            if not isinstance(self.message, str) or not self.message.strip():
                raise ValueError(f"A {kind.value} outcome requires a non-blank message.")
        if kind is not MemoOutcomeKind.REFUSED and self.failed_checks:
            raise ValueError("Only a refused outcome carries failed_checks.")
        if kind is not MemoOutcomeKind.CEILING_REACHED and (
            self.retry_at is not None or self.dimension is not None
        ):
            raise ValueError("Only a ceiling-reached outcome carries retry_at/dimension.")

    @property
    def generated(self) -> bool:
        return self.kind is MemoOutcomeKind.GENERATED

    @property
    def refused(self) -> bool:
        return self.kind is MemoOutcomeKind.REFUSED

    @property
    def degraded(self) -> bool:
        return self.kind is MemoOutcomeKind.PROVIDER_UNAVAILABLE

    @property
    def queued(self) -> bool:
        return self.kind is MemoOutcomeKind.CEILING_REACHED


class MemoGenerationService:
    """Draft, check and either persist or refuse one borrower memo.

    This service never calls a model provider itself and never decides
    whether a draft passed — `ai.memo.draft_memo` (`T-100`) already owns the
    one permitted regeneration and the four stage-7 shape checks. What it
    owns is everything after that verdict: a passed draft is persisted with
    its slot map, drafted text, actions, verdict and versions in one
    transaction with its stage-7 trace row and audit event; a refused draft
    is never given a `Memo` row at all, though the refusal itself is still
    traced and audited so "why is there no memo" stays answerable
    (`spec §R-17.a`, `§R-17.b`). A provider outage or a model-call ceiling
    is returned as a typed outcome rather than raised — `client.call`
    (`C-51`) has already written the `model_call` row that explains either
    one, so this service adds no duplicate record for them, only a message
    a caller can render without losing the rest of its screen
    (`spec §R-17.c`).

    The service never commits; it participates in the caller's transaction
    exactly like `IntakeService` and `TriageService`, so the memo write and
    its trace/audit rows become visible — or roll back — together.
    """

    def __init__(
        self,
        session: Session,
        *,
        client: ModelClient,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        assembler: MemoAssemblyService | None = None,
    ) -> None:
        if not isinstance(session, Session):
            raise TypeError("MemoGenerationService requires a SQLAlchemy Session.")
        if not isinstance(client, ModelClient):
            raise TypeError("MemoGenerationService requires a ModelClient.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("MemoGenerationService requires an append-only audit writer.")
        if assembler is not None and not isinstance(assembler, MemoAssemblyService):
            raise TypeError("assembler must be a MemoAssemblyService.")
        self.session = session
        self.client = client
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = _validated_request_id(
            request_id if request_id is not None else get_request_id() or new_request_id()
        )
        self.assembler = assembler or MemoAssemblyService()
        self.memos = MemoRepository(session)
        self.traces = TraceRepository(session, clock=self.clock, request_id=self.request_id)

    def generate(
        self,
        *,
        borrower_id: UUID,
        records: MemoRecords | Mapping[str, object] | object,
        catalogue: Iterable[object] | Mapping[str, object] | object,
        run_id: UUID | None = None,
        case_id: UUID | None = None,
        actor_id: UUID | None = None,
        prompt_loader: PromptLoader | None = None,
        prompt_version: str = PROMPT_VERSION,
        max_attempts: int = 2,
    ) -> MemoGenerationOutcome:
        """Assemble, draft and check one memo, then persist or refuse it.

        Exactly one of four outcomes is returned. A shape refusal, a
        provider outage and a call ceiling are all reported through the
        return value rather than raised, so a caller can always finish
        rendering the rest of its screen (`spec §R-17.c`).
        """

        if not isinstance(borrower_id, UUID):
            raise TypeError("borrower_id must be a UUID.")
        if run_id is not None and not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID or None.")
        if case_id is not None and not isinstance(case_id, UUID):
            raise TypeError("case_id must be a UUID or None.")
        if actor_id is not None and not isinstance(actor_id, UUID):
            raise TypeError("actor_id must be a UUID or None.")

        slots = self.assembler.assemble(records)
        actions = normalise_catalogue_actions(catalogue)

        try:
            drafting = draft_memo(
                slots,
                actions,
                self.client,
                prompt_loader=prompt_loader,
                request_id=self.request_id,
                max_attempts=max_attempts,
                prompt_version=prompt_version,
            )
        except MemoShapeRefusal as error:
            return self._refuse(borrower_id, slots, actions, prompt_version, error)
        except ProviderUnavailable:
            return MemoGenerationOutcome(
                kind=MemoOutcomeKind.PROVIDER_UNAVAILABLE,
                message=DEGRADED_MEMO_MESSAGE,
            )
        except ModelGovernanceBlocked:
            # The guarded client already records the refusal on `model_call`.
            # A maker-checker control is an expected degraded state, not a 500
            # that should remove the rest of the borrower screen.
            return MemoGenerationOutcome(
                kind=MemoOutcomeKind.PROVIDER_UNAVAILABLE,
                message=MODEL_GOVERNANCE_MEMO_MESSAGE,
            )
        except CeilingReached as error:
            return MemoGenerationOutcome(
                kind=MemoOutcomeKind.CEILING_REACHED,
                message=_ceiling_message(error),
                retry_at=error.retry_at,
                dimension=error.dimension,
            )

        return self._persist(
            borrower_id=borrower_id,
            run_id=run_id,
            case_id=case_id,
            actor_id=actor_id,
            slots=slots,
            actions=actions,
            prompt_version=prompt_version,
            drafting=drafting,
        )

    def _persist(
        self,
        *,
        borrower_id: UUID,
        run_id: UUID | None,
        case_id: UUID | None,
        actor_id: UUID | None,
        slots: MemoSlotMap,
        actions: Sequence[CatalogueAction],
        prompt_version: str,
        drafting: MemoDraftingResult,
    ) -> MemoGenerationOutcome:
        now = self._now()
        simulation_slot = slots["simulation_options"]
        simulations = (
            {"items": simulation_slot.as_mapping()["value"]}
            if simulation_slot.state is SlotState.PRESENT
            else None
        )
        memo = Memo(
            id=new_id(),
            borrower_id=borrower_id,
            run_id=run_id,
            case_id=case_id,
            template_version=slots.template_version,
            prompt_version=prompt_version,
            provider=_provider_name(self.client),
            model_version=drafting.provider_result.model,
            slots=slots.as_mapping(),
            drafted_text=drafting.draft.text,
            actions={"items": drafting.draft.as_mapping()["actions"]},
            simulations=simulations,
            check_verdict=drafting.shape_report.verdict,
            generated_by_id=actor_id,
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=self.request_id,
        )
        self.memos.add(memo)
        self.session.flush()
        self.traces.write(
            TraceSubject("borrower", borrower_id),
            stage_record(
                TraceStage.MEMO,
                Decider.MODEL,
                inputs={
                    "template_version": slots.template_version,
                    "action_ids": [action.id for action in actions],
                },
                outputs={
                    "verdict": drafting.shape_report.verdict,
                    "attempts": drafting.attempts,
                    "memo_id": str(memo.id),
                    # Retain the checked model output in the stage record as
                    # well as on the Memo row. Explainability API consumers
                    # should not receive only a memo id and verdict when the
                    # model-authored explanation is the stage's actual output.
                    "explanation": drafting.draft.as_mapping(),
                },
                rule_or_prompt_version=prompt_version,
                thresholds_compared=(),
                confidence=Decimal("1"),
                sources=(str(drafting.model_call_id),),
            ),
            actor_id=actor_id,
        )
        self.audit.record(
            AuditEventType.MEMO_GENERATED.value,
            ("memo", memo.id),
            {
                "borrower_id": str(borrower_id),
                "run_id": str(run_id) if run_id is not None else None,
                "case_id": str(case_id) if case_id is not None else None,
                "attempts": drafting.attempts,
                "check_verdict": drafting.shape_report.verdict,
                "template_version": slots.template_version,
                "prompt_version": prompt_version,
                "model_call_id": str(drafting.model_call_id),
            },
            actor=actor_id,
            request_id=self.request_id,
        )
        return MemoGenerationOutcome(kind=MemoOutcomeKind.GENERATED, memo=memo, drafting=drafting)

    def _refuse(
        self,
        borrower_id: UUID,
        slots: MemoSlotMap,
        actions: Sequence[CatalogueAction],
        prompt_version: str,
        error: MemoShapeRefusal,
    ) -> MemoGenerationOutcome:
        self.traces.write(
            TraceSubject("borrower", borrower_id),
            stage_record(
                TraceStage.MEMO,
                Decider.MODEL,
                inputs={
                    "template_version": slots.template_version,
                    "action_ids": [action.id for action in actions],
                },
                outputs={
                    "verdict": error.report.verdict,
                    "failed_checks": list(error.report.failed_checks),
                    "attempts": error.attempts,
                },
                rule_or_prompt_version=prompt_version,
                thresholds_compared=(),
                confidence=Decimal("0"),
                sources=(),
            ),
        )
        self.audit.record(
            AuditEventType.MEMO_REFUSED.value,
            ("borrower", borrower_id),
            {
                "attempts": error.attempts,
                "failed_checks": list(error.report.failed_checks),
                "reason": str(error)[:_MAX_AUDIT_REASON_LENGTH],
            },
            actor=None,
            request_id=self.request_id,
        )
        return MemoGenerationOutcome(
            kind=MemoOutcomeKind.REFUSED,
            message=str(error),
            failed_checks=error.report.failed_checks,
        )

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Memo generation clock must return an aware datetime.")
        return now.astimezone(UTC)


class MemoExportStorage(Protocol):
    """The write/delete surface needed by the memo export service."""

    def put(self, content: bytes) -> str:
        """Persist one immutable export and return its storage key."""
        ...

    def delete(self, storage_key: str) -> None:
        """Remove an object when the database write cannot be staged."""
        ...


class MemoRendererPort(Protocol):
    """Rendering boundary that keeps export orchestration adapter-neutral."""

    def render(self, memo: object, context: MemoExportContext, format: str) -> bytes:
        """Render one stored memo in the requested format."""
        ...


@dataclass(frozen=True, slots=True)
class MemoExportResult:
    """The downloadable artefact and its persisted provenance row."""

    format: str
    content: bytes
    content_type: str
    filename: str
    integrity_hash: str
    exported_at: datetime
    storage_key: str
    export_record: MemoExport

    @property
    def record(self) -> MemoExport:
        """Compatibility-facing name for the persisted export row."""

        return self.export_record

    @property
    def data(self) -> bytes:
        """Compatibility-facing name for the downloadable bytes."""

        return self.content


class MemoExportService:
    """Export a scoped stored memo and record every rendering event.

    The service participates in the caller's SQLAlchemy transaction.  It
    writes the artefact first, stages the corresponding ``memo_export`` row,
    and flushes it before returning.  If row staging fails, an injected
    storage adapter is asked to remove the just-created object where that
    operation is available.  The caller still owns the final commit, so the
    memo export cannot be committed by this service independently of the
    surrounding use case.
    """

    def __init__(
        self,
        session: Session,
        *,
        storage: MemoExportStorage,
        clock: Clock | None = None,
        request_id: str | None = None,
        renderer: MemoRendererPort | None = None,
        letterhead: MemoLetterhead | Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(session, Session):
            raise TypeError("MemoExportService requires a SQLAlchemy Session.")
        if not callable(getattr(storage, "put", None)):
            raise TypeError("MemoExportService storage must provide a callable put method.")
        if renderer is not None and not callable(getattr(renderer, "render", None)):
            raise TypeError("MemoExportService renderer must provide a callable render method.")
        self.session = session
        self.storage = storage
        self.clock = clock or SystemClock()
        self.request_id = _validated_request_id(
            request_id if request_id is not None else get_request_id() or new_request_id()
        )
        self.renderer = renderer or MemoRenderer()
        self.letterhead = MemoLetterhead.from_value(letterhead)
        self.memos = MemoRepository(session)

    def export(
        self,
        memo: Memo | UUID,
        *,
        format: str,
        principal: Principal | None = None,
        actor: Principal | None = None,
        scope: Scope | None = None,
        exported_by_name: str | None = None,
        exported_at: datetime | None = None,
        request_id: str | None = None,
    ) -> MemoExportResult:
        """Render one in-scope memo in ``pdf`` or ``docx`` format.

        ``principal`` is mandatory in substance even though ``actor`` is
        accepted as a compatibility alias: an export without
        ``GENERATE_MEMO`` is refused before the memo is looked up.  When a
        scope is not supplied, it is resolved from the principal at this
        request boundary; this keeps direct service callers subject to the
        same row-level rule as an HTTP route.
        """

        caller = _export_actor(principal, actor)
        authorize(caller, Permission.GENERATE_MEMO)
        export_format = _export_format(format)
        effective_scope = scope or resolve_scope(caller, self.session)
        if effective_scope.principal_id != caller.id:
            raise AuthorizationError(
                "The supplied portfolio scope belongs to a different principal.",
                field="scope",
            )
        memo_id = _memo_id(memo)
        stored_memo = self.memos.get(memo_id, scope=effective_scope)
        if stored_memo is None:
            raise NotFound("The requested memo was not found within the current scope.")

        instant = _export_instant(exported_at if exported_at is not None else self.clock.now())
        digest = memo_integrity_hash(stored_memo)
        context = MemoExportContext(
            memo_id=stored_memo.id,
            integrity_hash=digest,
            exported_at=instant,
            exported_by=exported_by_name or _exporter_name(self.session, caller),
            letterhead=self.letterhead,
        )
        content = self.renderer.render(stored_memo, context, export_format)
        storage_key = self._store(content)
        request = self.request_id if request_id is None else _validated_request_id(request_id)
        actor_user_id = None if caller.is_api_key else caller.id
        row = MemoExport(
            id=new_id(),
            memo_id=stored_memo.id,
            format=export_format,
            storage_key=storage_key,
            integrity_hash=digest,
            exported_at=instant,
            exported_by_id=actor_user_id,
            created_at=instant,
            updated_at=instant,
            created_by_id=actor_user_id,
            updated_by_id=actor_user_id,
            request_id=request,
        )
        try:
            self.session.add(row)
            self.session.flush()
        except Exception:
            self._remove_after_failed_row(storage_key)
            raise

        return MemoExportResult(
            format=export_format,
            content=content,
            content_type=_content_type(export_format),
            filename=f"memo-{stored_memo.id}.{export_format}",
            integrity_hash=digest,
            exported_at=instant,
            storage_key=storage_key,
            export_record=row,
        )

    export_memo = export

    def _store(self, content: bytes) -> str:
        if not isinstance(content, bytes) or not content:
            raise ValueError("Memo renderer returned empty or non-binary content.")
        storage_key = self.storage.put(content)
        if not isinstance(storage_key, str) or not storage_key.strip():
            raise ValueError("Memo export storage returned an invalid storage key.")
        cleaned = storage_key.strip()
        if len(cleaned) > 500 or any(
            ord(character) < 32 or ord(character) == 127 for character in cleaned
        ):
            raise ValueError("Memo export storage returned an unsafe storage key.")
        return cleaned

    def _remove_after_failed_row(self, storage_key: str) -> None:
        remove = getattr(self.storage, "delete", None)
        if not callable(remove):
            return
        try:
            remove(storage_key)
        except Exception:
            # The original database exception is more actionable to the
            # caller.  Content-addressed stores can safely reconcile an
            # orphan during their normal retention sweep.
            return


def _ceiling_message(error: CeilingReached) -> str:
    suffix = ""
    if error.retry_at is not None:
        suffix = f" It will be retried automatically after {error.retry_at.isoformat()}."
    return (
        f"The model-call {error.dimension} limit has been reached; this memo request has "
        f"been queued and will resolve once capacity is available.{suffix}"
    )


def _provider_name(client: ModelClient) -> str:
    provider = client.provider
    name = getattr(provider, "provider_name", None) or type(provider).__name__
    return str(name)[:_MAX_PROVIDER_NAME_LENGTH]


def _validated_request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(
            f"Memo generation request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
        )
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Memo generation request_id must not be blank.")
    return cleaned


def _export_actor(principal: Principal | None, actor: Principal | None) -> Principal:
    if principal is not None and actor is not None and principal != actor:
        raise AuthorizationError("principal and actor identify different callers.", field="actor")
    caller = principal if principal is not None else actor
    if caller is None:
        raise AuthorizationError("Authentication required to export a memo.", field="principal")
    if not isinstance(caller, Principal):
        raise AuthorizationError("An authenticated principal is required to export a memo.")
    return caller


def _export_format(value: object) -> str:
    if not isinstance(value, str):
        raise NotFound("The requested memo export format was not found.")
    normalized = value.strip().lower()
    if normalized not in {"pdf", "docx"}:
        raise NotFound(f"The requested memo export format {value!r} was not found.")
    return normalized


def _content_type(export_format: str) -> str:
    return (
        "application/pdf"
        if export_format == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def _memo_id(value: Memo | UUID) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, Memo) and isinstance(value.id, UUID):
        return value.id
    raise TypeError("Memo export requires a persisted Memo or UUID memo id.")


def _export_instant(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Memo export timestamp must be timezone-aware.")
    return value.astimezone(UTC)


def _exporter_name(session: Session, principal: Principal) -> str:
    if not principal.is_api_key:
        name = session.scalar(select(AppUser.full_name).where(AppUser.id == principal.id))
        if isinstance(name, str) and name.strip():
            return name.strip()
    prefix = "API key" if principal.is_api_key else "User"
    return f"{prefix} {principal.id}"


__all__ = [
    "DEGRADED_MEMO_MESSAGE",
    "MODEL_GOVERNANCE_MEMO_MESSAGE",
    "MemoAssemblyService",
    "MemoGenerationOutcome",
    "MemoGenerationService",
    "MemoExportResult",
    "MemoRendererPort",
    "MemoExportService",
    "MemoExportStorage",
    "MemoOutcomeKind",
    "MemoService",
    "assemble_memo_slots",
    "assemble_slots",
]
