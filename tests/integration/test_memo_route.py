"""Integration coverage for the C-08 memo action and its record collection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from covenant_radar.ai.client import InMemoryModelCallWriter, ModelClient
from covenant_radar.ai.shapes import CatalogueAction
from covenant_radar.asgi import create_app
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.models import EvidenceItem, Intervention
from covenant_radar.db.models.forecast import ForecastDriver, Simulation
from covenant_radar.db.models.workflow import Memo
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.memo.slots import MemoRecords, SlotState
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.memo import (
    DEGRADED_MEMO_MESSAGE,
    MemoAssemblyService,
    MemoGenerationOutcome,
    MemoGenerationService,
    MemoOutcomeKind,
)
from covenant_radar.services.memo_records import collect_memo_records
from covenant_radar.web.routes.borrower import create_borrower_router
from covenant_radar.web.routes.why import create_why_router
from covenant_radar.web.view_models.memo import build_memo_block
from tests.integration.test_case_file import _AS_OF, _NOW, _Fixture

pytestmark = pytest.mark.integration

#: Immutable so it can safely be a default argument.
_ASSUMPTIONS: Mapping[str, object] = MappingProxyType({"basis": "approved plan"})


class _StubProvider:
    """Returns one canned reply so the real call site runs unchanged."""

    provider_name = "fixture"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(
            text=self.reply,
            model="fixture-model",
            input_tokens=20,
            output_tokens=40,
            latency_ms=1,
            raw_payload={"fixture": True},
        )


def _stub_client(reply: str) -> ModelClient:
    """The real call site over a provider that answers without a network."""

    return ModelClient(
        _StubProvider(reply),
        model="fixture-model",
        model_calls=InMemoryModelCallWriter(),
        clock=FixedClock(_NOW),
    )


def _good_reply() -> str:
    """A reply whose every figure is taken from the fixture's own rows.

    The figures carry their persisted scale (``2.80000000``, the eight places
    ``RatioValue`` stores, not ``2.80``): the stage-7 grounding check compares
    exact token forms deliberately, so that a model cannot quietly rescale a
    recorded number while appearing to cite it. The prompt shows the model
    those same forms.
    """

    return json.dumps(
        {
            "headline": (
                "Total Debt / Tangible Net Worth is projected to reach the action point "
                "on 2026-11-29."
            ),
            "summary": (
                "The recorded value is 2.80000000 against a threshold of 3.25000000, with "
                "headroom of 13.8462. The projected breach probability is 0.5825 at "
                "confidence 0.9000."
            ),
            "drivers": ["ROLE_DRIVER_1"],
            "actions": [{"id": "CREDIT-REDUCE", "role_tag": "credit"}],
            "recommended_next_step": "Review and reduce funded exposure.",
            "disclaimer": "human credit review is required before action",
        }
    )


class _MemoFixture(_Fixture):
    """The case-file world plus the rows a memo is grounded in."""

    def __init__(self) -> None:
        super().__init__()
        self.principal = Principal.user(
            self.principal.id,
            (Permission.VIEW_BORROWER, Permission.GENERATE_MEMO),
        )
        self.calls: list[dict[str, object]] = []

    def client(self, *, generator: object | None = None, principal: Principal | None = None):
        app = create_app(
            routers=(
                create_borrower_router(
                    self.session,
                    memo_generator=generator,  # type: ignore[arg-type]
                ),
            ),
            principal_resolver=lambda _request: principal or self.principal,
        )
        return TestClient(app)

    def triage_with_situation(
        self, what_changed: str | None = "Headroom fell for two quarters."
    ) -> None:
        entry = self.triage()
        entry.what_changed = what_changed
        self.session.flush()

    def driver(self, name: str = "Cash-flow pressure", share: str = "0.60") -> ForecastDriver:
        row = ForecastDriver(
            id=uuid4(),
            forecast_id=self.forecast_row.id,
            name=name,
            share=Decimal(share),
            is_other=False,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-c08-driver",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def evidence(self, *, count: int | None = 3, family: str = "payment") -> EvidenceItem:
        row = EvidenceItem(
            id=uuid4(),
            borrower_id=self.borrower.id,
            facility_id=None,
            family=family,
            evidence_type="delay",
            first_seen=date(2026, 7, 1),
            last_seen=date(2026, 8, 1),
            persistence_days=21,
            event_count_window=count,
            materiality_pct=Decimal("8"),
            decay_factor=Decimal("1"),
            state="sustained",
            counts_toward_pressure=True,
            source_event_ids=[str(uuid4()), str(uuid4()), str(uuid4())],
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-c08-evidence",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def intervention(
        self,
        code: str = "CREDIT-REDUCE",
        *,
        role_tag: str | None = "credit",
        classes: list[str] | None = None,
    ) -> Intervention:
        row = Intervention(
            id=uuid4(),
            code=code,
            role_tag=role_tag,
            text="Review and reduce funded exposure.",
            effect_model="level_shift",
            effect_parameters={"amount": "-0.10"},
            applicable_covenant_classes=classes,
            requires_approval=False,
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-c08-{code.lower()}",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def simulation(
        self,
        intervention: Intervention,
        *,
        assumptions: Mapping[str, object] | None = _ASSUMPTIONS,
    ) -> Simulation:
        row = Simulation(
            id=uuid4(),
            forecast_id=self.forecast_row.id,
            intervention_id=intervention.id,
            parameters={"amount": "-0.10"},
            assumptions=dict(assumptions) if assumptions is not None else None,
            projected_cross_date=date(2027, 2, 1),
            probability=Decimal("0.41"),
            delta_days=64,
            delta_probability=Decimal("-0.17"),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-c08-simulation",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def ground(self) -> None:
        """Persist the full set of facts a complete memo is drafted from.

        Committed, not merely flushed, so reads go through the database and
        back. That matters to the grounding check: the decimal types quantize
        on the way out, so a confidence written as ``0.90`` returns as
        ``0.9000``. A request in the running app always sees the round-tripped
        form, and a fixture that stops at flush would pin a reply shape
        production never produces.
        """

        self.forecast_row = self.forecast()
        self.test()
        self.triage_with_situation()
        self.driver()
        self.evidence()
        self.session.commit()

    def generation_service(self, reply: str) -> MemoGenerationService:
        """The real service with only the provider replaced."""

        client = _stub_client(reply)
        audit = AuditRecorder(
            AuditRepository(self.session), clock=FixedClock(_NOW), request_id="rq-c08-memo"
        )
        return MemoGenerationService(
            self.session,
            client=client,
            audit=audit,
            clock=FixedClock(_NOW),
            request_id="rq-c08-memo",
        )

    def records(self, simulation_ids: tuple[UUID, ...] = ()) -> MemoRecords:
        scope = resolve_scope(self.principal, self.session)
        return collect_memo_records(
            self.session,
            self.borrower,
            scope=scope,
            simulation_ids=simulation_ids,
        ).records


def _outcome(kind: MemoOutcomeKind, **extra: object) -> MemoGenerationOutcome:
    messages = {
        MemoOutcomeKind.REFUSED: "The draft did not pass its checks.",
        MemoOutcomeKind.PROVIDER_UNAVAILABLE: DEGRADED_MEMO_MESSAGE,
        MemoOutcomeKind.CEILING_REACHED: "The model-call daily limit has been reached.",
    }
    return MemoGenerationOutcome(kind=kind, message=messages[kind], **extra)


def _generator(outcome: MemoGenerationOutcome, sink: list[dict[str, object]]):
    def generate(**kwargs: object) -> MemoGenerationOutcome:
        sink.append(kwargs)
        return outcome

    return generate


# ---------------------------------------------------------------------------
# The route (`C-08`)
# ---------------------------------------------------------------------------


def test_memo_action_requires_the_permission() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        reader = Principal.user(fixture.principal.id, (Permission.VIEW_BORROWER,))
        with fixture.client(
            generator=_generator(_outcome(MemoOutcomeKind.REFUSED), fixture.calls),
            principal=reader,
        ) as client:
            response = client.post("/memos", data={"borrower_ref": fixture.borrower.reference})

        assert response.status_code == 403
        assert fixture.calls == []
    finally:
        fixture.close()


def test_out_of_scope_borrower_is_not_found() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        with fixture.client(
            generator=_generator(_outcome(MemoOutcomeKind.REFUSED), fixture.calls)
        ) as client:
            response = client.post("/memos", data={"borrower_ref": "B-UNKNOWN"})

        assert response.status_code == 404
        assert fixture.calls == []
    finally:
        fixture.close()


def test_without_a_provider_the_block_explains_itself() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        with fixture.client(generator=None) as client:
            response = client.post("/memos", data={"borrower_ref": fixture.borrower.reference})

        assert response.status_code == 200
        assert 'data-state="unavailable"' in response.text
        assert "no model provider is configured" in response.text
    finally:
        fixture.close()


def test_without_a_forecast_no_model_call_is_made() -> None:
    fixture = _MemoFixture()
    try:
        # Deliberately no forecast, triage entry or test: nothing to ground on.
        with fixture.client(
            generator=_generator(_outcome(MemoOutcomeKind.REFUSED), fixture.calls)
        ) as client:
            response = client.post("/memos", data={"borrower_ref": fixture.borrower.reference})

        assert response.status_code == 200
        assert 'data-state="unavailable"' in response.text
        assert "No completed forecast" in response.text
        assert fixture.calls == []
    finally:
        fixture.close()


def test_refusal_returns_200_and_names_the_failed_checks() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        outcome = _outcome(
            MemoOutcomeKind.REFUSED, failed_checks=("no_fabricated_numbers", "actions_in_catalogue")
        )
        with fixture.client(generator=_generator(outcome, fixture.calls)) as client:
            response = client.post("/memos", data={"borrower_ref": fixture.borrower.reference})

        assert response.status_code == 200
        assert 'data-state="refused"' in response.text
        assert "no_fabricated_numbers" in response.text
        assert "actions_in_catalogue" in response.text
    finally:
        fixture.close()


def test_provider_outage_degrades_without_losing_the_screen() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        with fixture.client(
            generator=_generator(_outcome(MemoOutcomeKind.PROVIDER_UNAVAILABLE), fixture.calls)
        ) as client:
            response = client.post("/memos", data={"borrower_ref": fixture.borrower.reference})

        assert response.status_code == 200
        assert 'data-state="degraded"' in response.text
        assert "temporarily unavailable" in response.text
    finally:
        fixture.close()


def test_ceiling_returns_a_queued_banner() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        outcome = _outcome(
            MemoOutcomeKind.CEILING_REACHED,
            retry_at=_NOW,
            dimension="daily",
        )
        with fixture.client(generator=_generator(outcome, fixture.calls)) as client:
            response = client.post("/memos", data={"borrower_ref": fixture.borrower.reference})

        assert response.status_code == 200
        assert 'data-state="queued"' in response.text
        assert _NOW.isoformat() in response.text
    finally:
        fixture.close()


def test_named_simulations_reach_the_generator() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        chosen = fixture.simulation(fixture.intervention())
        other = fixture.simulation(fixture.intervention("CREDIT-SECURE"))
        with fixture.client(
            generator=_generator(_outcome(MemoOutcomeKind.REFUSED), fixture.calls)
        ) as client:
            response = client.post(
                "/memos",
                data={
                    "borrower_ref": fixture.borrower.reference,
                    "simulation_ids": [str(chosen.id)],
                },
            )

        assert response.status_code == 200
        records = fixture.calls[0]["records"]
        assert isinstance(records, MemoRecords)
        cited = {
            reference.record_id
            for record in records.simulations
            for reference in (record.reference,)
        }
        assert cited == {chosen.id}
        assert other.id not in cited
    finally:
        fixture.close()


def test_unparseable_simulation_id_is_refused() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        with fixture.client(
            generator=_generator(_outcome(MemoOutcomeKind.REFUSED), fixture.calls)
        ) as client:
            response = client.post(
                "/memos",
                data={
                    "borrower_ref": fixture.borrower.reference,
                    "simulation_ids": ["not-a-uuid"],
                },
            )

        assert response.status_code == 422
        assert fixture.calls == []
    finally:
        fixture.close()


# ---------------------------------------------------------------------------
# Record collection (the DB -> MemoRecords mapping)
# ---------------------------------------------------------------------------


def test_collected_records_assemble_into_the_fixed_slot_map() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        fixture.simulation(fixture.intervention())

        slots = MemoAssemblyService().assemble(fixture.records())

        assert slots["ratio_name"].value == "Total Debt / Tangible Net Worth"
        assert slots["value"].value == Decimal("2.80")
        assert slots["threshold"].value == Decimal("3.25")
        assert slots["probability"].value == Decimal("0.5825")
        assert slots["crossing_date"].value == date(2026, 11, 29)
        assert slots["situation"].value == "Headroom fell for two quarters."
        # Provenance survives the whole way back to the row it came from.
        assert slots["value"].record_references[0].record_type == "forecast"
        assert slots["drivers"].record_references[0].record_type == "forecast_driver"
    finally:
        fixture.close()


def test_a_suppressed_probability_is_never_carried_as_a_number() -> None:
    fixture = _MemoFixture()
    try:
        fixture.forecast_row = fixture.forecast()
        fixture.forecast_row.below_confidence_floor = True
        fixture.test()
        fixture.triage_with_situation()
        fixture.session.flush()

        slots = MemoAssemblyService().assemble(fixture.records())

        probability = slots["probability"]
        assert probability.state is SlotState.SUPPRESSED
        assert "0.5825" not in str(probability.value)
        assert "confidence" in (probability.reason or "")
    finally:
        fixture.close()


def test_evidence_without_a_recorded_count_is_skipped() -> None:
    fixture = _MemoFixture()
    try:
        fixture.forecast_row = fixture.forecast()
        fixture.test()
        fixture.triage_with_situation()
        fixture.evidence(count=None, family="utilisation")
        counted = fixture.evidence(count=4, family="payment")

        records = fixture.records()

        assert len(records.evidence) == 1
        assert records.evidence[0].reference.record_id == counted.id
        assert records.evidence[0].values["count"] == 4
    finally:
        fixture.close()


def test_a_simulation_without_assumptions_is_not_reported() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        fixture.simulation(fixture.intervention("NO-ASSUMPTIONS"), assumptions=None)

        assert fixture.records().simulations == ()
    finally:
        fixture.close()


def test_an_intervention_without_a_role_tag_is_not_recommended() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        fixture.intervention("UNOWNED", role_tag=None)
        fixture.intervention("OWNED", role_tag="credit")

        codes = {record.values["code"] for record in fixture.records().recommendations}

        assert codes == {"OWNED"}
    finally:
        fixture.close()


def test_recommendations_are_limited_to_the_covenant_class() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        fixture.intervention("FINANCIAL", classes=["financial"])
        fixture.intervention("LIQUIDITY", classes=["liquidity"])
        fixture.intervention("ANY", classes=None)

        codes = {record.values["code"] for record in fixture.records().recommendations}

        assert codes == {"ANY", "FINANCIAL"}
    finally:
        fixture.close()


def test_blank_what_changed_becomes_a_stated_absence() -> None:
    fixture = _MemoFixture()
    try:
        fixture.forecast_row = fixture.forecast()
        fixture.test()
        fixture.triage_with_situation(None)

        slots = MemoAssemblyService().assemble(fixture.records())

        situation = slots["situation"]
        assert situation.state is SlotState.ABSENT
        assert situation.reason is not None
    finally:
        fixture.close()


def test_collection_is_scoped_to_the_caller() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        stranger = Principal.user(uuid4(), (Permission.GENERATE_MEMO,))
        scope = resolve_scope(stranger, fixture.session)

        facts = collect_memo_records(fixture.session, fixture.borrower, scope=scope)

        assert facts.forecast is None
        assert facts.records.evidence == ()
        assert not facts.has_forecast
    finally:
        fixture.close()


def test_collected_records_draft_and_persist_a_real_memo() -> None:
    """The whole path, with only the provider stubbed.

    This is the test that proves the collector's output is usable: real rows
    are read, assembled, masked, sent, checked against the four stage-7
    shapes and persisted. If the mapping supplied a slot the checks cannot
    ground, this refuses instead.
    """

    fixture = _MemoFixture()
    try:
        fixture.ground()
        intervention = fixture.intervention()
        fixture.simulation(intervention)
        service = fixture.generation_service(_good_reply())

        outcome = service.generate(
            borrower_id=fixture.borrower.id,
            records=fixture.records(),
            catalogue=(
                CatalogueAction(
                    id=intervention.code,
                    role_tag="credit",
                    text=intervention.text,
                ),
            ),
            run_id=fixture.run.id,
        )

        assert outcome.kind is MemoOutcomeKind.GENERATED, outcome.message
        assert outcome.memo is not None
        stored = fixture.session.get(Memo, outcome.memo.id)
        assert stored is not None
        assert stored.borrower_id == fixture.borrower.id
        # The slot map persisted with the memo carries the collector's
        # provenance, so the memo remains reconstructible from its sources.
        slots = stored.slots["slots"]
        assert slots["value"]["record_references"][0]["type"] == "forecast"
        assert slots["drivers"]["record_references"][0]["type"] == "forecast_driver"

        block = build_memo_block(outcome)
        assert block.generated
        assert block.headline
        assert block.label
        assert block.provider == "fixture"
        assert block.model_version == "fixture-model"
        assert block.prompt_version == "v2"
        assert block.check_verdict == "Passed"
        assert any(citation.source_type == "forecast" for citation in block.citations)
        assert any(citation.source_type == "evidence_item" for citation in block.citations)
    finally:
        fixture.close()


def test_the_service_accepts_what_the_composition_root_hands_it() -> None:
    """A `scoped_session` is what `web/application.py` actually holds.

    Every router and service there is composed once at startup around a
    `scoped_session` proxy, not a `Session`. `MemoGenerationService` requires
    a real `Session`, so the proxy has to be resolved before it is passed;
    handing the proxy over raises `TypeError` and turns the memo action into
    a 500. Fixture-built tests never see this, because they hold a `Session`
    directly.
    """

    fixture = _MemoFixture()
    try:
        proxy = scoped_session(sessionmaker(bind=fixture.session.get_bind()))
        assert not isinstance(proxy, Session)
        assert is_database_session(proxy)

        with pytest.raises(TypeError, match="requires a SQLAlchemy Session"):
            MemoGenerationService(
                proxy,  # type: ignore[arg-type]
                client=fixture.generation_service(_good_reply()).client,
                audit=AuditRecorder(
                    AuditRepository(fixture.session),
                    clock=FixedClock(_NOW),
                    request_id="rq-c08-proxy",
                ),
            )

        # Resolving the proxy is what the composition root does, and it works.
        service = MemoGenerationService(
            proxy(),
            client=fixture.generation_service(_good_reply()).client,
            audit=AuditRecorder(
                AuditRepository(fixture.session),
                clock=FixedClock(_NOW),
                request_id="rq-c08-proxy",
            ),
        )
        assert isinstance(service.session, Session)
    finally:
        fixture.close()


def test_generated_memo_remains_visible_after_borrower_page_reload() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        intervention = fixture.intervention()
        service = fixture.generation_service(_good_reply())
        outcome = service.generate(
            borrower_id=fixture.borrower.id,
            records=fixture.records(),
            catalogue=(
                CatalogueAction(
                    id=intervention.code,
                    role_tag="credit",
                    text=intervention.text,
                ),
            ),
            run_id=fixture.run.id,
        )
        assert outcome.kind is MemoOutcomeKind.GENERATED

        response = fixture.client(generator=service.generate).get(
            f"/borrowers/{fixture.borrower.reference}"
        )

        assert response.status_code == 200
        assert 'data-state="generated"' in response.text
        assert "Total Debt / Tangible Net Worth" in response.text
        assert intervention.text in response.text
    finally:
        fixture.close()


def test_generated_model_prose_is_visible_from_the_why_panel() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()
        intervention = fixture.intervention()
        outcome = fixture.generation_service(_good_reply()).generate(
            borrower_id=fixture.borrower.id,
            records=fixture.records(),
            catalogue=(
                CatalogueAction(
                    id=intervention.code,
                    role_tag="credit",
                    text=intervention.text,
                ),
            ),
            run_id=fixture.run.id,
        )
        assert outcome.kind is MemoOutcomeKind.GENERATED

        app = create_app(
            routers=(create_why_router(fixture.session),),
            principal_resolver=lambda _request: fixture.principal,
        )
        with TestClient(app) as client:
            # Queue rows open the why-panel for a forecast subject, whereas
            # the memo trace belongs to the borrower. The generated prose
            # must bridge that ownership boundary on the user-facing path.
            response = client.get(f"/why/forecast/{fixture.forecast_row.id}")

        assert response.status_code == 200
        assert 'class="why-ai-explanation" data-state="generated"' in response.text
        assert "Total Debt / Tangible Net Worth is projected" in response.text
        assert "The recorded value is 2.80000000" in response.text
        assert "Generated by fixture · fixture-model · prompt v2" in response.text
        assert f'href="/borrowers/{fixture.borrower.reference}#case-memo"' in response.text
    finally:
        fixture.close()


def test_memo_action_survives_a_request_through_scoped_session_wiring() -> None:
    """The 500 this reproduces was invisible to every fixture-built test.

    Those hold a `Session` and pass it straight through. The real app holds a
    `scoped_session` and composes every router around it once at startup, so
    the failure only appeared inside a live request. This wires the router the
    way `web/application.py` does and drives an actual POST through it.
    """

    fixture = _MemoFixture()
    try:
        fixture.ground()
        intervention = fixture.intervention()
        # A second Session on the same engine only sees committed rows; the
        # StaticPool in-memory database is private to this test.
        fixture.session.commit()
        code, text = intervention.code, intervention.text
        proxy = scoped_session(sessionmaker(bind=fixture.session.get_bind()))

        def memo_generator(**kwargs: object) -> MemoGenerationOutcome:
            service = MemoGenerationService(
                proxy(),
                client=_stub_client(_good_reply()),
                audit=AuditRecorder(
                    AuditRepository(proxy()),
                    clock=FixedClock(_NOW),
                    request_id="rq-c08-live",
                ),
                clock=FixedClock(_NOW),
                request_id="rq-c08-live",
            )
            return service.generate(
                catalogue=(CatalogueAction(id=code, role_tag="credit", text=text),),
                **kwargs,  # type: ignore[arg-type]
            )

        app = create_app(
            routers=(create_borrower_router(proxy, memo_generator=memo_generator),),
            principal_resolver=lambda _request: fixture.principal,
        )
        with TestClient(app) as client:
            response = client.post("/memos", data={"borrower_ref": fixture.borrower.reference})

        assert response.status_code == 200, response.text
        assert 'data-state="generated"' in response.text
        assert "Total Debt / Tangible Net Worth" in response.text
        assert "AI explanation provenance" in response.text
        assert "fixture-model" in response.text
        assert "Grounding checks" in response.text
        assert "Passed" in response.text
        assert "Grounding citations" in response.text
        assert f"<td>{text}</td>" in response.text
        assert f"/why/forecast/{fixture.forecast_row.id}" in response.text
        proxy.remove()
    finally:
        fixture.close()


def test_as_of_date_fixture_is_the_run_the_memo_reads() -> None:
    fixture = _MemoFixture()
    try:
        fixture.ground()

        facts = collect_memo_records(
            fixture.session,
            fixture.borrower,
            scope=resolve_scope(fixture.principal, fixture.session),
        )

        assert facts.run_id == fixture.run.id
        assert fixture.run.as_of_date == _AS_OF
    finally:
        fixture.close()
