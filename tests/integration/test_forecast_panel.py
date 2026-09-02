"""Integration coverage for the T-076 forecast panel and trajectory boundary."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastDriver,
    ForecastPath,
    ForecastRun,
    Intervention,
)
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.svg.trajectory import (
    TrajectoryPoint,
    render_trajectory_svg,
)
from tests.integration.test_case_file import _AS_OF, _Fixture

pytestmark = pytest.mark.integration


def _forecast(
    fixture: _Fixture,
    horizon: int,
    *,
    probability: Decimal | None = Decimal("0.5825"),
    confidence: Decimal | None = Decimal("0.9000"),
    below_floor: bool = False,
    crossing_date: date | None = None,
    crossing_day: int | None = None,
    formula_inputs: dict[str, object] | None = None,
    direction: str = "max",
) -> Forecast:
    inputs = formula_inputs or {}
    if crossing_day is not None:
        inputs = {**inputs, "crossing": {"crossing_day": crossing_day}}
    row = Forecast(
        id=uuid4(),
        run_id=fixture.run.id,
        covenant_version_id=fixture.version.id,
        horizon_days=horizon,
        probability=probability,
        confidence=confidence,
        below_confidence_floor=below_floor,
        projected_cross_date=crossing_date,
        direction=direction,
        formula_inputs=inputs,
        data_as_of=_AS_OF,
        created_at=fixture.run.created_at,
        updated_at=fixture.run.updated_at,
        request_id=f"rq-t076-forecast-{horizon}",
    )
    fixture.session.add(row)
    fixture.session.flush()
    return row


def _path(fixture: _Fixture) -> None:
    for day_offset, value in (
        (0, Decimal("2.80")),
        (30, Decimal("3.02")),
        (60, Decimal("3.25")),
        (90, Decimal("3.55")),
    ):
        fixture.session.add(
            ForecastPath(
                id=uuid4(),
                run_id=fixture.run.id,
                covenant_version_id=fixture.version.id,
                day_offset=day_offset,
                projected_value=value,
                headroom_pct=Decimal("10") - Decimal(day_offset) / Decimal("10"),
                created_at=fixture.run.created_at,
                updated_at=fixture.run.updated_at,
                request_id="rq-t076-path",
            )
        )
    fixture.session.flush()


def test_three_horizons_rendered() -> None:
    fixture = _Fixture()
    try:
        fixture.triage()
        for horizon in (30, 60, 90):
            _forecast(
                fixture,
                horizon,
                crossing_date=date(2026, 10, 29) if horizon >= 60 else None,
                crossing_day=60 if horizon >= 60 else None,
            )
        _path(fixture)

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        assert body.count('data-horizon="30"') == 1
        assert body.count('data-horizon="60"') == 1
        assert body.count('data-horizon="90"') == 1
        assert "58%" in body
        assert "90%" in body
        assert "29 Oct 2026" in body
    finally:
        fixture.close()


def test_below_floor_shows_text_and_limiting_factor() -> None:
    fixture = _Fixture()
    try:
        _forecast(
            fixture,
            30,
            probability=None,
            confidence=Decimal("0.4000"),
            below_floor=True,
            formula_inputs={"confidence": {"limiting_factor": "staleness"}},
        )
        _path(fixture)

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert "Suppressed" in body
        assert "staleness is the limiting factor" in body
        assert 'data-horizon="30" data-state="suppressed"' in body
        assert 'data-trajectory="stored"' in body
        assert "0.4000" not in body
    finally:
        fixture.close()


def test_no_crossing_states_direction() -> None:
    fixture = _Fixture()
    try:
        _forecast(fixture, 30, crossing_date=None, direction="max")

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert "No projected crossing in 30 days" in body
        assert "toward the maximum threshold" in body
        assert 'class="trajectory__crossing-tick"' not in body
    finally:
        fixture.close()


def test_trajectory_requires_ledger_figures() -> None:
    rendered = render_trajectory_svg(
        "trajectory-test",
        (TrajectoryPoint(0, Decimal("1")), TrajectoryPoint(1, Decimal("2"))),
        Decimal("3"),
        (),
    )

    assert "ledger figures are required" in str(rendered)
    assert "<svg" not in rendered


def test_svg_has_text_equivalent() -> None:
    fixture = _Fixture()
    try:
        fixture.triage()
        _forecast(fixture, 30)
        _forecast(fixture, 60, crossing_date=date(2026, 10, 29), crossing_day=60)
        _forecast(fixture, 90, crossing_date=date(2026, 10, 29), crossing_day=60)
        _path(fixture)

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        trajectory_id = f"forecast-trajectory-{fixture.covenant.id}"
        assert f'aria-describedby="{trajectory_id}-ledger {trajectory_id}-description"' in body
        assert f'id="{trajectory_id}-ledger"' in body
        assert "Threshold in force: 3.25x" in body
        assert "30-day: 58%" in body
    finally:
        fixture.close()


def test_no_forecast_panel_states_why() -> None:
    fixture = _Fixture()
    try:
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        assert "No forecast is recorded for this horizon" in body
        assert "Trajectory unavailable" in body
        assert 'data-trajectory="stored"' not in body
    finally:
        fixture.close()


def test_prediction_explanation_discloses_rule_ml_shadow_and_citations() -> None:
    fixture = _Fixture()
    try:
        evidence = EvidenceItem(
            id=uuid4(),
            borrower_id=fixture.borrower.id,
            family="payment",
            evidence_type="payment_delay",
            first_seen=_AS_OF,
            last_seen=_AS_OF,
            persistence_days=14,
            event_count_window=4,
            materiality_pct=Decimal("20"),
            decay_factor=Decimal("0.80"),
            state="sustained",
            counts_toward_pressure=True,
            source_event_ids=["event-1"],
            last_scored_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
            version=1,
            created_at=fixture.run.created_at,
            updated_at=fixture.run.updated_at,
            request_id="rq-forecast-explanation-evidence",
        )
        fixture.session.add(evidence)
        forecast = _forecast(
            fixture,
            90,
            formula_inputs={
                "scoring_rule_version": "forecast.scoring.v1",
                "probability_source": "deterministic",
                "predictor_mode": "shadow",
                "challenger_probability": "0.7100",
                "feature_snapshot_hash": "a" * 64,
                "fallback_reason": (
                    "ML challenger runs in shadow mode; deterministic probability retained"
                ),
                "probability": {
                    "distance": "0.12",
                    "velocity": "0.03",
                    "pressure": "0.20",
                    "probability": "0.5825",
                    "terms": {
                        "distance": {
                            "input_value": "0.12",
                            "normalized_value": "0.88",
                            "weight": "0.50",
                            "contribution": "0.44",
                        },
                        "velocity": {
                            "input_value": "0.03",
                            "normalized_value": "0.03",
                            "weight": "0.25",
                            "contribution": "0.0075",
                        },
                        "pressure": {
                            "input_value": "0.20",
                            "normalized_value": "0.20",
                            "weight": "0.25",
                            "contribution": "0.05",
                        },
                    },
                },
                "ml_prediction": {
                    "model_version": "stage4:v2",
                    "artifact_checksum": "b" * 64,
                    "probability": "0.7100",
                    "contributions": [
                        {"name": "evidence_pressure", "value": "0.42"}
                    ],
                },
            },
        )
        forecast.probability_source = "deterministic"
        forecast.fallback_reason = (
            "ML challenger runs in shadow mode; deterministic probability retained"
        )
        fixture.session.add(
            ForecastDriver(
                id=uuid4(),
                forecast_id=forecast.id,
                name="payment_delay",
                share=Decimal("0.60"),
                evidence_id=evidence.id,
                is_other=False,
                created_at=fixture.run.created_at,
                updated_at=fixture.run.updated_at,
                request_id="rq-forecast-explanation-driver",
            )
        )
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        assert "Deterministic rule with ML challenger" in body
        assert "Shadow comparison — it did not affect the displayed risk band or case." in body
        assert "forecast.scoring.v1" in body
        assert "stage4:v2" in body
        assert "b" * 64 in body
        assert "Sustained evidence pressure" in body
        assert f'href="#evidence-item-{evidence.id}"' in body
        assert f'href="/why/forecast/{forecast.id}"' in body
        assert "An LLM did not calculate or rewrite this explanation" in body
    finally:
        fixture.close()


def test_operational_ml_without_governance_mode_is_visibly_flagged() -> None:
    fixture = _Fixture()
    try:
        forecast = _forecast(
            fixture,
            90,
            formula_inputs={
                "probability_source": "ml",
                "ml_prediction": {
                    "model_version": "legacy-ml:v1",
                    "artifact_checksum": "c" * 64,
                    "probability": "0.5825",
                    "contributions": [],
                },
            },
        )
        forecast.probability_source = "ml"
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        assert response.status_code == 200
        assert "ML operational probability" in response.text
        assert "does not record an approved champion mode" in response.text
        assert 'role="alert"' in response.text
    finally:
        fixture.close()


def test_bank_owned_actionable_insight_links_to_simulator() -> None:
    fixture = _Fixture()
    try:
        fixture.principal = Principal.user(
            fixture.principal.id,
            (Permission.VIEW_BORROWER, Permission.RUN_SIMULATION),
        )
        fixture.covenant.covenant_class = "leverage"
        forecast = _forecast(fixture, 90)
        action = Intervention(
            id=uuid4(),
            code="CREDIT-REDUCE-EXPOSURE",
            role_tag="credit",
            text="Review and reduce funded exposure through the bank approval process.",
            effect_model="level_shift",
            effect_parameters={
                "amount": "-0.10",
                "_assumptions": ["An approved reduction is available immediately."],
            },
            applicable_covenant_classes=["leverage"],
            requires_approval=True,
            is_active=True,
            version=1,
            created_at=fixture.run.created_at,
            updated_at=fixture.run.updated_at,
            request_id="rq-forecast-action",
        )
        fixture.session.add(action)
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        assert "Possible actionable insights" in body
        assert "CREDIT-REDUCE-EXPOSURE" in body
        assert "Owner: Credit" in body
        assert "Human approval required" in body
        assert "An approved reduction is available immediately." in body
        assert f'/simulator/{forecast.id}?intervention_code=CREDIT-REDUCE-EXPOSURE' in body
        assert "not automatic credit decisions" in body
    finally:
        fixture.close()


def test_universal_bank_catalogue_action_is_not_hidden() -> None:
    fixture = _Fixture()
    try:
        fixture.principal = Principal.user(
            fixture.principal.id,
            (Permission.VIEW_BORROWER, Permission.RUN_SIMULATION),
        )
        forecast = _forecast(fixture, 90)
        fixture.session.add(
            Intervention(
                id=uuid4(),
                code="RM-REQUEST-INFORMATION",
                role_tag="relationship_manager",
                text="Request updated management information through the bank workflow.",
                effect_model="information_request",
                effect_parameters={},
                applicable_covenant_classes=[],
                requires_approval=False,
                is_active=True,
                version=1,
                created_at=fixture.run.created_at,
                updated_at=fixture.run.updated_at,
                request_id="rq-forecast-universal-action",
            )
        )
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        assert response.status_code == 200
        assert "RM-REQUEST-INFORMATION" in response.text
        assert "applicable to all covenant classes" in response.text
        assert f"/simulator/{forecast.id}?intervention_code=RM-REQUEST-INFORMATION" in response.text
    finally:
        fixture.close()


def test_newer_run_for_another_scope_does_not_hide_borrower_prediction() -> None:
    fixture = _Fixture()
    try:
        fixture.triage()
        _forecast(fixture, 90)
        newer_run = ForecastRun(
            id=uuid4(),
            as_of_date=_AS_OF + timedelta(days=1),
            started_at=fixture.run.started_at + timedelta(days=1),
            finished_at=fixture.run.finished_at + timedelta(days=1),
            covenant_count=0,
            state="complete",
            created_at=fixture.run.created_at + timedelta(days=1),
            updated_at=fixture.run.updated_at + timedelta(days=1),
            request_id="rq-unrelated-newer-run",
        )
        fixture.session.add(newer_run)
        fixture.session.flush()

        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        assert response.status_code == 200
        assert "58%" in response.text
        assert "Deterministic forecast rule" in response.text
    finally:
        fixture.close()
