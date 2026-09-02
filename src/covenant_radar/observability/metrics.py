"""The process-wide Prometheus registry (`spec §20`), a cardinality guard
at registration time, and the instrumentation surface every layer calls
into.

Every label name a metric may carry is drawn from a small, reviewed
allowlist. A label built from a per-record identifier, a raw message or
any other unbounded value turns one metric into an unbounded number of
time series and takes the scrape target — and often the host — down under
load; refusing that at registration is cheaper than discovering it in
production (`spec §20`'s "a metric with a high-cardinality label → refused
at registration").

All metrics register into the default `prometheus_client` registry, the
same one `/metrics` (`web/routes/system.py`) serves with `generate_latest`.
Because this module is imported exactly once per process, every metric
below is registered exactly once — a second `create_app()` in the same
process (as tests do) reuses the same collector instead of re-registering.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Final, Literal

from prometheus_client import Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_MAX_LABELS_PER_METRIC: Final[int] = 3

# The reviewed, bounded vocabulary of label names any metric below may use.
# Every value ever placed under one of these names is drawn from a small,
# known set (a route template, a job name, an evidence family, ...) —
# never a request id, a borrower id, an email address or free text.
_ALLOWED_LABEL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "route",
        "method",
        "status",
        "outcome",
        "job_name",
        "band",
        "family",
        "state",
        "provider",
        "stage",
        "direction",
        "channel",
        "connector",
        "horizon_days",
    }
)


class CardinalityGuardError(ValueError):
    """Raised when a metric registration would allow unbounded label cardinality."""


def _guarded_labelnames(name: str, labelnames: Sequence[str]) -> tuple[str, ...]:
    if len(labelnames) > _MAX_LABELS_PER_METRIC:
        raise CardinalityGuardError(
            f"Metric {name!r} declares {len(labelnames)} labels; at most "
            f"{_MAX_LABELS_PER_METRIC} are permitted to keep its series count bounded."
        )
    unbounded = [label for label in labelnames if label not in _ALLOWED_LABEL_NAMES]
    if unbounded:
        raise CardinalityGuardError(
            f"Metric {name!r} may not register unbounded label(s) {tuple(unbounded)!r}: "
            "a label carrying a per-record identifier or free text turns one metric into an "
            "unbounded number of series and is refused at registration, not at scrape time."
        )
    return tuple(labelnames)


def register_counter(
    name: str, documentation: str, labelnames: Sequence[str] = ()
) -> Counter:
    """Register a `Counter`, refusing any label outside the reviewed allowlist."""
    return Counter(name, documentation, _guarded_labelnames(name, labelnames))


def register_gauge(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Gauge:
    """Register a `Gauge`, refusing any label outside the reviewed allowlist."""
    return Gauge(name, documentation, _guarded_labelnames(name, labelnames))


def register_histogram(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    *,
    buckets: Sequence[float] | None = None,
) -> Histogram:
    """Register a `Histogram`, refusing any label outside the reviewed allowlist."""
    guarded = _guarded_labelnames(name, labelnames)
    if buckets is not None:
        return Histogram(name, documentation, guarded, buckets=tuple(buckets))
    return Histogram(name, documentation, guarded)


# --------------------------------------------------------------------------
# `spec §20`'s metric list, one family per named signal.
# --------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = register_counter(
    "covenant_radar_http_requests_total",
    "Total HTTP requests, by route, method and status.",
    ("route", "method", "status"),
)
HTTP_REQUEST_DURATION_SECONDS = register_histogram(
    "covenant_radar_http_request_duration_seconds",
    "HTTP request latency in seconds, by route and method.",
    ("route", "method"),
)
AUTH_ATTEMPTS_TOTAL = register_counter(
    "covenant_radar_auth_attempts_total",
    "Authentication attempts, by outcome.",
    ("outcome",),
)
JOB_RUNS_TOTAL = register_counter(
    "covenant_radar_job_runs_total",
    "Scheduled job runs, by job name and outcome.",
    ("job_name", "outcome"),
)
JOB_DURATION_SECONDS = register_histogram(
    "covenant_radar_job_duration_seconds",
    "Scheduled job run duration in seconds, by job name.",
    ("job_name",),
)
JOB_LAG_SECONDS = register_gauge(
    "covenant_radar_job_lag_seconds",
    "Seconds between a job's scheduled and actual start, by job name.",
    ("job_name",),
)
QUEUE_DEPTH = register_gauge(
    "covenant_radar_queue_depth",
    "Open queue items, by urgency band.",
    ("band",),
)
EVIDENCE_VOLUME = register_gauge(
    "covenant_radar_evidence_volume",
    "Evidence items on hand, by family and state.",
    ("family", "state"),
)
FORECAST_RUNS_TOTAL = register_counter(
    "covenant_radar_forecast_runs_total",
    "Forecasts produced, by horizon in days.",
    ("horizon_days",),
)
FORECAST_CONFIDENCE = register_histogram(
    "covenant_radar_forecast_confidence",
    "Distribution of forecast confidence values.",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0),
)
MODEL_CALL_DURATION_SECONDS = register_histogram(
    "covenant_radar_model_call_duration_seconds",
    "Model provider call latency in seconds, by provider and stage.",
    ("provider", "stage"),
)
MODEL_TOKENS_TOTAL = register_counter(
    "covenant_radar_model_tokens_total",
    "Model tokens consumed, by provider, stage and direction.",
    ("provider", "stage", "direction"),
)
MODEL_COST_TOTAL = register_counter(
    "covenant_radar_model_cost_total",
    "Model spend in rupees, by provider and stage.",
    ("provider", "stage"),
)
MODEL_REFUSALS_TOTAL = register_counter(
    "covenant_radar_model_refusals_total",
    "Model calls whose verification refused the response, by provider and stage.",
    ("provider", "stage"),
)
CONNECTOR_LAG_SECONDS = register_gauge(
    "covenant_radar_connector_lag_seconds",
    "Seconds a connector is behind its expected cycle, by connector.",
    ("connector",),
)
QUARANTINE_DEPTH = register_gauge(
    "covenant_radar_quarantine_depth",
    "Rows held in quarantine, by evidence family.",
    ("family",),
)
NOTIFICATION_DELIVERIES_TOTAL = register_counter(
    "covenant_radar_notification_deliveries_total",
    "Notification delivery attempts, by channel and outcome.",
    ("channel", "outcome"),
)
DATABASE_POOL_SIZE = register_gauge(
    "covenant_radar_database_pool_size",
    "Configured database connection pool size.",
)
DATABASE_POOL_IN_USE = register_gauge(
    "covenant_radar_database_pool_in_use",
    "Database connections currently checked out of the pool.",
)
DOCUMENT_STORE_BYTES_USED = register_gauge(
    "covenant_radar_document_store_bytes_used",
    "Bytes of document content currently stored.",
)


# --------------------------------------------------------------------------
# Instrumentation surface. Every value below carries only a validated
# label from the allowlist above; the identifying detail (which borrower,
# which request) belongs in the structured log line and the trace span,
# never in a metric label.
# --------------------------------------------------------------------------


def _require_non_negative(value: float, field: str) -> None:
    if value < 0:
        raise ValueError(f"{field} must not be negative.")


def record_http_request(*, route: str, method: str, status: int, duration_seconds: float) -> None:
    """Record one completed HTTP request against its matched route template.

    `route` must be the route's path template (e.g. ``/api/v1/borrowers/{id}``),
    never the raw request path, or a distinct identifier in the path turns
    this metric unbounded despite passing the registration-time guard.
    """
    _require_non_negative(duration_seconds, "duration_seconds")
    HTTP_REQUESTS_TOTAL.labels(route=route, method=method, status=str(status)).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(route=route, method=method).observe(duration_seconds)


def record_auth_attempt(*, outcome: Literal["success", "failure"]) -> None:
    """Record one authentication attempt."""
    if outcome not in {"success", "failure"}:
        raise ValueError("Authentication outcome must be 'success' or 'failure'.")
    AUTH_ATTEMPTS_TOTAL.labels(outcome=outcome).inc()


def record_job_run(
    *,
    job_name: str,
    outcome: str,
    duration_seconds: float,
    lag_seconds: float | None = None,
) -> None:
    """Record one finished job attempt: its outcome, duration and, when known,
    how late it started against its schedule."""
    _require_non_negative(duration_seconds, "duration_seconds")
    JOB_RUNS_TOTAL.labels(job_name=job_name, outcome=outcome).inc()
    JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration_seconds)
    if lag_seconds is not None:
        _require_non_negative(lag_seconds, "lag_seconds")
        JOB_LAG_SECONDS.labels(job_name=job_name).set(lag_seconds)


def set_queue_depth(*, band: str, depth: int) -> None:
    """Set the current open-item count for one urgency band."""
    _require_non_negative(depth, "depth")
    QUEUE_DEPTH.labels(band=band).set(depth)


def set_evidence_volume(*, family: str, state: str, count: int) -> None:
    """Set the current evidence-item count for one family and state."""
    _require_non_negative(count, "count")
    EVIDENCE_VOLUME.labels(family=family, state=state).set(count)


def record_forecast(*, horizon_days: int, confidence: float) -> None:
    """Record one produced forecast and its confidence."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("Forecast confidence must be between 0 and 1.")
    FORECAST_RUNS_TOTAL.labels(horizon_days=str(horizon_days)).inc()
    FORECAST_CONFIDENCE.observe(confidence)


def record_model_call(
    *,
    provider: str,
    stage: str,
    duration_seconds: float,
    tokens_in: int,
    tokens_out: int,
    cost: float,
    refused: bool,
) -> None:
    """Record one completed model-provider call."""
    _require_non_negative(duration_seconds, "duration_seconds")
    _require_non_negative(tokens_in, "tokens_in")
    _require_non_negative(tokens_out, "tokens_out")
    _require_non_negative(cost, "cost")
    MODEL_CALL_DURATION_SECONDS.labels(provider=provider, stage=stage).observe(duration_seconds)
    MODEL_TOKENS_TOTAL.labels(provider=provider, stage=stage, direction="in").inc(tokens_in)
    MODEL_TOKENS_TOTAL.labels(provider=provider, stage=stage, direction="out").inc(tokens_out)
    MODEL_COST_TOTAL.labels(provider=provider, stage=stage).inc(cost)
    if refused:
        MODEL_REFUSALS_TOTAL.labels(provider=provider, stage=stage).inc()


def set_connector_lag(*, connector: str, lag_seconds: float) -> None:
    """Set how far behind its expected cycle one connector currently is."""
    _require_non_negative(lag_seconds, "lag_seconds")
    CONNECTOR_LAG_SECONDS.labels(connector=connector).set(lag_seconds)


def set_quarantine_depth(*, family: str, depth: int) -> None:
    """Set the current quarantine row count for one evidence family."""
    _require_non_negative(depth, "depth")
    QUARANTINE_DEPTH.labels(family=family).set(depth)


def record_notification_delivery(*, channel: str, outcome: str) -> None:
    """Record one notification delivery attempt."""
    NOTIFICATION_DELIVERIES_TOTAL.labels(channel=channel, outcome=outcome).inc()


def set_database_pool_saturation(*, size: int, in_use: int) -> None:
    """Set the configured pool size and the connections currently checked out."""
    _require_non_negative(size, "size")
    _require_non_negative(in_use, "in_use")
    DATABASE_POOL_SIZE.set(size)
    DATABASE_POOL_IN_USE.set(in_use)


def set_document_store_usage(*, bytes_used: int) -> None:
    """Set the current document-store usage in bytes."""
    _require_non_negative(bytes_used, "bytes_used")
    DOCUMENT_STORE_BYTES_USED.set(bytes_used)


class RequestMetricsMiddleware:
    """Record `covenant_radar_http_request*` for every HTTP request.

    A raw ASGI middleware, like `web/middleware.py`'s `RequestContextMiddleware`,
    so a streaming response is measured without buffering it in memory. The
    route label is read from `scope["route"]` — set by Starlette's router
    once a route has matched — after the inner application has finished
    handling the request, so it reflects the matched path template and never
    the raw request path. A request no route matched (a 404) is recorded
    under the fixed ``"unmatched"`` route rather than the arbitrary path the
    client sent, which would otherwise defeat the cardinality guard above.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_box: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_box["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            record_http_request(
                route=_route_label(scope),
                method=str(scope.get("method", "GET")),
                status=status_box.get("status", 500),
                duration_seconds=time.perf_counter() - started,
            )


def _route_label(scope: Scope) -> str:
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) and path else "unmatched"


__all__ = [
    "AUTH_ATTEMPTS_TOTAL",
    "CONNECTOR_LAG_SECONDS",
    "DATABASE_POOL_IN_USE",
    "DATABASE_POOL_SIZE",
    "DOCUMENT_STORE_BYTES_USED",
    "EVIDENCE_VOLUME",
    "FORECAST_CONFIDENCE",
    "FORECAST_RUNS_TOTAL",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "JOB_DURATION_SECONDS",
    "JOB_LAG_SECONDS",
    "JOB_RUNS_TOTAL",
    "MODEL_CALL_DURATION_SECONDS",
    "MODEL_COST_TOTAL",
    "MODEL_REFUSALS_TOTAL",
    "MODEL_TOKENS_TOTAL",
    "NOTIFICATION_DELIVERIES_TOTAL",
    "QUARANTINE_DEPTH",
    "QUEUE_DEPTH",
    "CardinalityGuardError",
    "RequestMetricsMiddleware",
    "record_auth_attempt",
    "record_forecast",
    "record_http_request",
    "record_job_run",
    "record_model_call",
    "record_notification_delivery",
    "register_counter",
    "register_gauge",
    "register_histogram",
    "set_connector_lag",
    "set_database_pool_saturation",
    "set_document_store_usage",
    "set_evidence_volume",
    "set_quarantine_depth",
    "set_queue_depth",
]
