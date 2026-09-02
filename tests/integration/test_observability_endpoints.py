"""Integration coverage for the T-143 metrics, health, readiness and
version endpoints (`C-23`)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

import covenant_radar
from covenant_radar.asgi import create_app
from covenant_radar.observability.metrics import CardinalityGuardError, register_counter

pytestmark = pytest.mark.integration

# `spec §20`'s metric list, one (exposition name, Prometheus type) pair per
# family `observability/metrics.py` registers. A `Counter`'s text-format
# HELP/TYPE line always carries the `_total` suffix even though the name is
# stored without it internally, so the suffix here matches what a scrape
# actually contains rather than the registration call's raw argument.
_EVERY_SPEC_METRIC = (
    ("covenant_radar_http_requests_total", "counter"),
    ("covenant_radar_http_request_duration_seconds", "histogram"),
    ("covenant_radar_auth_attempts_total", "counter"),
    ("covenant_radar_job_runs_total", "counter"),
    ("covenant_radar_job_duration_seconds", "histogram"),
    ("covenant_radar_job_lag_seconds", "gauge"),
    ("covenant_radar_queue_depth", "gauge"),
    ("covenant_radar_evidence_volume", "gauge"),
    ("covenant_radar_forecast_runs_total", "counter"),
    ("covenant_radar_forecast_confidence", "histogram"),
    ("covenant_radar_model_call_duration_seconds", "histogram"),
    ("covenant_radar_model_tokens_total", "counter"),
    ("covenant_radar_model_cost_total", "counter"),
    ("covenant_radar_model_refusals_total", "counter"),
    ("covenant_radar_connector_lag_seconds", "gauge"),
    ("covenant_radar_quarantine_depth", "gauge"),
    ("covenant_radar_notification_deliveries_total", "counter"),
    ("covenant_radar_database_pool_size", "gauge"),
    ("covenant_radar_database_pool_in_use", "gauge"),
    ("covenant_radar_document_store_bytes_used", "gauge"),
)


class _UnreachableDatabase:
    """A minimal stand-in for a `sqlalchemy.Engine` whose database is down."""

    def connect(self) -> None:
        raise SQLAlchemyError("simulated database outage")


def test_health_up_ready_false_when_database_down() -> None:
    app = create_app()
    app.state.database_engine = _UnreachableDatabase()

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json()["healthy"] is True

    body = ready.json()
    assert ready.status_code == 503
    assert body["ready"] is False
    assert body["checks"]["database"]["status"] == "not_ready"
    assert "outage" in body["checks"]["database"]["detail"].lower()


def test_unconfigured_capability_not_a_readiness_failure() -> None:
    # Default settings configure no database engine, document store, scheduler
    # or optional capability, which must read as "not configured" rather than
    # blocking readiness for every evaluation build that has none of them.
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 200
    assert body["ready"] is True
    assert body["checks"]["database"]["status"] == "not_configured"
    assert body["checks"]["capability:model_provider"]["status"] == "not_configured"
    assert body["checks"]["capability:sso"]["status"] == "not_configured"


def test_metrics_requires_authorisation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COVENANT_RADAR_METRICS_TOKEN", "a-shared-secret")
    app = create_app()
    external_client = ("203.0.113.5", 12345)

    with TestClient(app, client=external_client) as client:
        refused = client.get("/metrics")
        wrong_token = client.get("/metrics", headers={"x-metrics-token": "not-the-secret"})
        allowed = client.get(
            "/metrics", headers={"x-metrics-token": "a-shared-secret"}
        )

    assert refused.status_code == 403
    assert wrong_token.status_code == 403
    assert allowed.status_code == 200
    assert allowed.headers["content-type"].startswith("text/plain")


def test_metrics_open_to_loopback_without_a_configured_token() -> None:
    app = create_app()

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.get("/metrics")

    assert response.status_code == 200


def test_high_cardinality_label_refused() -> None:
    with pytest.raises(CardinalityGuardError):
        register_counter(
            "covenant_radar_test_unbounded_label",
            "A metric that would carry a per-record identifier.",
            ("borrower_id",),
        )

    with pytest.raises(CardinalityGuardError):
        register_counter(
            "covenant_radar_test_too_many_labels",
            "A metric declaring more labels than the bounded maximum.",
            ("route", "method", "status", "outcome"),
        )


def test_version_matches_package() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/version")

    body = response.json()
    assert response.status_code == 200
    assert body["version"] == covenant_radar.__version__
    assert body["commit"]
    assert body["build_time"]


def test_every_spec_metric_exported() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    body = response.text
    for name, metric_type in _EVERY_SPEC_METRIC:
        assert f"# TYPE {name} {metric_type}" in body, f"{name} was not exported."
