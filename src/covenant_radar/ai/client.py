"""The single, guarded outbound model call site.

All model interaction enters through :func:`call` (or an explicitly injected
``ModelClient``).  Provider adapters remain deliberately small and do not
retry; this module owns the cross-provider guarantees that must not drift
between stage 1 and stage 7: masking and prompt-version verification,
timeouts, one retry, T7 ceilings, cassette fallback, and an append-only call
record for every attempt or refusal.
"""

from __future__ import annotations

import inspect
import queue
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import IntEnum
from pathlib import Path
from typing import Any, Final, Protocol, cast
from uuid import UUID

import structlog

from covenant_radar.ai.budget import BudgetLedger, BudgetLimits, BudgetReservation, CeilingReached
from covenant_radar.ai.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderUnavailable,
)
from covenant_radar.ai.masking import MASKING_MARKER, MaskedPrompt
from covenant_radar.ai.registry import (
    ComponentNotApproved,
    ComponentNotRegistered,
    ModelRegistryGuard,
)
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_job_run_id, get_request_id, new_request_id
from covenant_radar.core.ids import new_id
from covenant_radar.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMProvider,
    MessageInput,
    PromptMessage,
)

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_MAX_TOKENS: Final[int] = 2048
DEFAULT_CURRENCY: Final[str] = "INR"
_MAX_REQUEST_ID_LENGTH: Final[int] = 40
_MAX_RECORD_TEXT_LENGTH: Final[int] = 2000
_TOKENS_PER_THOUSAND: Final[Decimal] = Decimal(1000)
_PERMITTED_STAGES: Final[frozenset[int]] = frozenset({1, 7})
_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:prompt[-_ ]*)?version\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
    re.IGNORECASE,
)
_VERSION_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"v[0-9]+(?:\.[0-9]+)*$", re.IGNORECASE)


class Stage(IntEnum):
    """The only two stages permitted to use a language model."""

    ONE = 1
    STAGE_1 = 1
    STAGE1 = 1
    S1 = 1
    SEVEN = 7
    STAGE_7 = 7
    STAGE7 = 7
    S7 = 7


class ModelCallPersistenceError(RuntimeError):
    """The call cannot complete safely because its audit record was not saved."""


@dataclass(frozen=True, slots=True)
class CallContext:
    """Request-scoped data needed to build and account for one call."""

    request_id: str | None = None
    component: str | None = None
    model: str | None = None
    model_version: str | None = None
    provider: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None
    timeout: float | None = None
    estimated_cost: Decimal | None = None
    cost_estimate: Decimal | None = None
    input_price_per_1k: Decimal | None = None
    output_price_per_1k: Decimal | None = None
    currency: str | None = None
    cassette_key: str | None = None
    cassette_provider: LLMProvider | None = None


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    """Persistence-neutral shape mapped to the ``model_call`` row."""

    id: UUID
    request_id: str
    stage: str
    provider: str
    model_version: str
    prompt_version: str | None
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int | None
    cost: Decimal | None
    currency: str | None
    check_verdict: str | None
    retry_count: int
    refusal_reason: str | None
    from_cassette: bool
    created_at: datetime
    updated_at: datetime


class ModelCallWriter(Protocol):
    """The transaction-owned persistence seam for model-call records."""

    def record(self, record: ModelCallRecord) -> object:
        """Persist one immutable model-call record without committing."""


class AlertSink(Protocol):
    """Minimal alert seam used when a model ceiling queues a request."""

    def alert(self, event: str, payload: Mapping[str, object]) -> object:
        """Raise or persist one operational alert."""


@dataclass(frozen=True, slots=True)
class ModelResult:
    """Successful call result with the record id that explains its provenance."""

    response: CompletionResponse
    model_call_id: UUID
    retry_count: int
    cost: Decimal | None
    from_cassette: bool

    @property
    def text(self) -> str | None:
        return self.response.text

    @property
    def model(self) -> str | None:
        return self.response.model

    @property
    def input_tokens(self) -> int | None:
        return self.response.input_tokens

    @property
    def output_tokens(self) -> int | None:
        return self.response.output_tokens

    @property
    def call_id(self) -> UUID:
        """Short compatibility alias for callers rendering the call trail."""

        return self.model_call_id


class InMemoryModelCallWriter:
    """Thread-safe append-only writer for offline evaluation and focused tests."""

    def __init__(self) -> None:
        self._records: list[ModelCallRecord] = []
        self._lock = threading.Lock()

    def record(self, record: ModelCallRecord) -> ModelCallRecord:
        if not isinstance(record, ModelCallRecord):
            raise TypeError("InMemoryModelCallWriter accepts ModelCallRecord values.")
        with self._lock:
            self._records.append(record)
        return record

    @property
    def records(self) -> tuple[ModelCallRecord, ...]:
        with self._lock:
            return tuple(self._records)


class SqlAlchemyModelCallWriter:
    """Map call records into an existing SQLAlchemy transaction.

    The import is deliberately local: the client depends on a persistence
    protocol, not on a database session.  Applications construct this writer
    inside their unit of work and commit it with the rest of the use case.
    """

    def __init__(
        self,
        session: object,
        *,
        actor_id: UUID | None = None,
    ) -> None:
        if not callable(getattr(session, "add", None)):
            raise TypeError("SqlAlchemyModelCallWriter requires a session with add().")
        self._session: Any = session
        self._actor_id = actor_id

    def record(self, record: ModelCallRecord) -> object:
        if not isinstance(record, ModelCallRecord):
            raise TypeError("SqlAlchemyModelCallWriter accepts ModelCallRecord values.")
        from covenant_radar.db.models.operations import ModelCall

        row = ModelCall(
            id=record.id,
            stage=record.stage,
            provider=record.provider,
            model_version=record.model_version,
            prompt_version=record.prompt_version,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            latency_ms=record.latency_ms,
            cost=record.cost,
            currency=record.currency,
            check_verdict=record.check_verdict,
            retry_count=record.retry_count,
            refusal_reason=record.refusal_reason,
            from_cassette=record.from_cassette,
            created_by_id=self._actor_id,
            updated_by_id=self._actor_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            request_id=record.request_id,
        )
        self._session.add(row)
        return row


class ModelClient:
    """Guarded, provider-neutral model call boundary."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str | None = None,
        budget: BudgetLedger | BudgetLimits | Mapping[str, object] | None = None,
        model_calls: ModelCallWriter | object | None = None,
        recorder: ModelCallWriter | object | None = None,
        model_call_writer: ModelCallWriter | object | None = None,
        model_call_repo: ModelCallWriter | object | None = None,
        alerts: AlertSink | Callable[..., object] | object | None = None,
        alert_sink: AlertSink | Callable[..., object] | object | None = None,
        registry_guard: ModelRegistryGuard | None = None,
        clock: Clock | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = 0.0,
        cassette_provider: LLMProvider | None = None,
        cassette: LLMProvider | None = None,
        input_price_per_1k: Decimal | int | str | None = None,
        output_price_per_1k: Decimal | int | str | None = None,
        currency: str = DEFAULT_CURRENCY,
    ) -> None:
        if not isinstance(provider, LLMProvider):
            raise TypeError("provider must implement the LLMProvider protocol.")
        writers = [
            writer
            for writer in (model_calls, recorder, model_call_writer, model_call_repo)
            if writer is not None
        ]
        if len(writers) > 1:
            raise TypeError("Pass only one model-call writer.")
        if alerts is not None and alert_sink is not None:
            raise TypeError("Pass either alerts or alert_sink, not both.")
        if cassette_provider is not None and cassette is not None:
            raise TypeError("Pass either cassette_provider or cassette, not both.")
        self.provider = provider
        self.model = _optional_text(model, "model")
        self.budget = _make_budget(budget, clock)
        self.model_calls = writers[0] if writers else InMemoryModelCallWriter()
        self.alerts = alerts if alerts is not None else alert_sink
        self.registry_guard = registry_guard
        self.clock = clock or SystemClock()
        self.timeout_seconds = _timeout(timeout_seconds)
        self.max_tokens = _max_tokens(max_tokens)
        self.temperature = _temperature(temperature)
        self.cassette_provider = cassette_provider if cassette_provider is not None else cassette
        self.input_price_per_1k = _optional_decimal(input_price_per_1k, "input_price_per_1k")
        self.output_price_per_1k = _optional_decimal(output_price_per_1k, "output_price_per_1k")
        self.currency = _currency(currency)

    def call(
        self,
        stage: int | str | Stage,
        prompt: MaskedPrompt | object,
        prompt_version: str,
        context: CallContext | None = None,
    ) -> ModelResult:
        """Make one guarded model call and return its normalised response."""

        call_context = context or CallContext()
        if not isinstance(call_context, CallContext):
            raise TypeError("context must be a CallContext.")
        request_id = self._request_id(call_context)
        stage_value: int | None = None
        provider_name = self._provider_name(call_context)

        try:
            stage_value = _normalise_stage(stage)
            if self.registry_guard is not None and call_context.component is not None:
                self.registry_guard.ensure_permitted(call_context.component)
            expected_version = _validate_prompt_version(prompt_version)
            messages = _verify_masked_prompt(prompt, expected_version)
            model = self._model(call_context)
            request = CompletionRequest(
                messages=messages,
                model=model,
                max_tokens=call_context.max_tokens
                if call_context.max_tokens is not None
                else self.max_tokens,
                temperature=(
                    call_context.temperature
                    if call_context.temperature is not None
                    else self.temperature
                ),
                timeout_seconds=(
                    call_context.timeout_seconds
                    if call_context.timeout_seconds is not None
                    else call_context.timeout
                    if call_context.timeout is not None
                    else self.timeout_seconds
                ),
                prompt_version=expected_version,
                cassette_key=call_context.cassette_key,
            )
        except Exception as error:
            self._record_refusal(
                request_id=request_id,
                stage=_record_stage(stage_value if stage_value is not None else stage),
                provider=provider_name,
                model=self._record_model(call_context),
                prompt_version=_record_prompt_version(prompt_version),
                reason=_validation_reason(error),
            )
            raise

        retries = 0
        while True:
            estimated_cost = self._estimated_cost(call_context, request)
            try:
                reservation = self.budget.reserve(estimated_cost=estimated_cost)
            except CeilingReached as error:
                self._record_ceiling(
                    error,
                    request_id=request_id,
                    stage=stage_value,
                    provider=provider_name,
                    model=model,
                    prompt_version=expected_version,
                    retry_count=retries,
                )
                raise

            started = time.perf_counter()
            try:
                response = self._complete(self.provider, request, provider_name)
            except ProviderUnavailable as error:
                elapsed = _elapsed_ms(started)
                self._settle_failed(reservation)
                self._record_attempt(
                    request_id=request_id,
                    stage=stage_value,
                    provider=provider_name,
                    model=model,
                    prompt_version=expected_version,
                    response=None,
                    latency_ms=elapsed,
                    cost=reservation.estimated_cost,
                    currency=self.currency,
                    retry_count=retries,
                    refusal_reason=_provider_reason(error),
                    from_cassette=False,
                    check_verdict="provider_unavailable",
                )
                if retries < 1 and _retryable(error):
                    retries += 1
                    continue
                fallback = call_context.cassette_provider or self.cassette_provider
                if fallback is not None:
                    result = self._cassette_fallback(
                        fallback,
                        request,
                        request_id=request_id,
                        stage=stage_value,
                        model=model,
                        prompt_version=expected_version,
                        retry_count=retries,
                    )
                    if result is not None:
                        return result
                raise ProviderUnavailable(
                    provider_name,
                    reason=_safe_provider_reason(error),
                ) from error
            except ProviderAuthError:
                self._settle_failed(reservation)
                self._record_attempt(
                    request_id=request_id,
                    stage=stage_value,
                    provider=provider_name,
                    model=model,
                    prompt_version=expected_version,
                    response=None,
                    latency_ms=_elapsed_ms(started),
                    cost=reservation.estimated_cost,
                    currency=self.currency,
                    retry_count=retries,
                    refusal_reason="authentication_failure",
                    from_cassette=False,
                    check_verdict="provider_auth_refused",
                )
                raise
            except ProviderError as error:
                self._settle_failed(reservation)
                self._record_attempt(
                    request_id=request_id,
                    stage=stage_value,
                    provider=provider_name,
                    model=model,
                    prompt_version=expected_version,
                    response=None,
                    latency_ms=_elapsed_ms(started),
                    cost=reservation.estimated_cost,
                    currency=self.currency,
                    retry_count=retries,
                    refusal_reason=_provider_reason(error),
                    from_cassette=False,
                    check_verdict="provider_refused",
                )
                raise
            except Exception as error:
                self._settle_failed(reservation)
                self._record_attempt(
                    request_id=request_id,
                    stage=stage_value,
                    provider=provider_name,
                    model=model,
                    prompt_version=expected_version,
                    response=None,
                    latency_ms=_elapsed_ms(started),
                    cost=reservation.estimated_cost,
                    currency=self.currency,
                    retry_count=retries,
                    refusal_reason=f"provider_error:{type(error).__name__}",
                    from_cassette=False,
                    check_verdict="provider_error",
                )
                raise

            actual_cost = self._actual_cost(call_context, response, reservation)
            settled_cost = self.budget.settle(reservation, actual_cost)
            record = self._record_attempt(
                request_id=request_id,
                stage=stage_value,
                provider=provider_name,
                model=response.model or model,
                prompt_version=expected_version,
                response=response,
                latency_ms=max(response.latency_ms, _elapsed_ms(started)),
                cost=settled_cost,
                currency=self.currency if settled_cost is not None else None,
                retry_count=retries,
                refusal_reason=None,
                from_cassette=response.from_cassette,
                check_verdict="not_checked",
            )
            return ModelResult(
                response=response,
                model_call_id=record.id,
                retry_count=retries,
                cost=settled_cost,
                from_cassette=response.from_cassette,
            )

    def _complete(
        self,
        provider: LLMProvider,
        request: CompletionRequest,
        provider_name: str,
    ) -> CompletionResponse:
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put(("response", provider.complete(request)))
            except BaseException as error:
                result_queue.put(("error", error))

        worker = threading.Thread(
            target=invoke,
            name=f"covenant-radar-llm-{provider_name}",
            daemon=True,
        )
        worker.start()
        worker.join(request.timeout_seconds)
        if worker.is_alive():
            raise ProviderUnavailable(provider_name, reason="timeout")

        try:
            kind, value = result_queue.get_nowait()
        except queue.Empty as error:  # pragma: no cover - defensive worker invariant
            raise ProviderUnavailable(provider_name, reason="transport failure") from error
        if kind == "error":
            if isinstance(value, TimeoutError):
                raise ProviderUnavailable(provider_name, reason="timeout") from value
            if isinstance(value, BaseException):
                raise value
            raise ProviderUnavailable(provider_name, reason="transport failure")
        response = value
        if inspect.isawaitable(response):
            raise TypeError("LLMProvider.complete must be synchronous.")
        if not isinstance(response, CompletionResponse):
            raise TypeError("LLMProvider.complete returned an invalid response shape.")
        return response

    def _cassette_fallback(
        self,
        provider: LLMProvider,
        request: CompletionRequest,
        *,
        request_id: str,
        stage: int | str,
        model: str,
        prompt_version: str,
        retry_count: int,
    ) -> ModelResult | None:
        cassette_name = self._provider_name(CallContext(provider="recorded"), provider)
        started = time.perf_counter()
        try:
            response = self._complete(provider, request, cassette_name)
        except Exception as error:
            self._record_attempt(
                request_id=request_id,
                stage=stage,
                provider=cassette_name,
                model=model,
                prompt_version=prompt_version,
                response=None,
                latency_ms=_elapsed_ms(started),
                cost=Decimal(0),
                currency=self.currency,
                retry_count=retry_count,
                refusal_reason=_provider_reason(error),
                from_cassette=True,
                check_verdict="cassette_unavailable",
            )
            return None

        record = self._record_attempt(
            request_id=request_id,
            stage=stage,
            provider=cassette_name,
            model=response.model or model,
            prompt_version=prompt_version,
            response=response,
            latency_ms=max(response.latency_ms, _elapsed_ms(started)),
            cost=Decimal(0),
            currency=self.currency,
            retry_count=retry_count,
            refusal_reason=None,
            from_cassette=True,
            check_verdict="not_checked",
        )
        return ModelResult(
            response=response,
            model_call_id=record.id,
            retry_count=retry_count,
            cost=Decimal(0),
            from_cassette=True,
        )

    def _record_attempt(
        self,
        *,
        request_id: str,
        stage: int | str,
        provider: str,
        model: str,
        prompt_version: str | None,
        response: CompletionResponse | None,
        latency_ms: int | None,
        cost: Decimal | None,
        currency: str | None,
        retry_count: int,
        refusal_reason: str | None,
        from_cassette: bool,
        check_verdict: str,
    ) -> ModelCallRecord:
        now = self._now()
        record = ModelCallRecord(
            id=new_id(),
            request_id=request_id,
            stage=_bounded(str(stage), "stage"),
            provider=_bounded(provider, "provider"),
            model_version=_bounded(model, "model_version"),
            prompt_version=_bounded(prompt_version, "prompt_version")
            if prompt_version is not None
            else None,
            tokens_in=response.input_tokens if response is not None else None,
            tokens_out=response.output_tokens if response is not None else None,
            latency_ms=latency_ms,
            cost=cost,
            currency=currency,
            check_verdict=check_verdict,
            retry_count=retry_count,
            refusal_reason=_bounded(refusal_reason, "refusal_reason")
            if refusal_reason is not None
            else None,
            from_cassette=from_cassette,
            created_at=now,
            updated_at=now,
        )
        _write_record(self.model_calls, record)
        logger.info(
            "model_call",
            call_id=str(record.id),
            stage=record.stage,
            provider=record.provider,
            model_version=record.model_version,
            prompt_version=record.prompt_version,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            latency_ms=record.latency_ms,
            cost=str(record.cost) if record.cost is not None else None,
            currency=record.currency,
            check_verdict=record.check_verdict,
            retry_count=record.retry_count,
            refusal_reason=record.refusal_reason,
            from_cassette=record.from_cassette,
        )
        return record

    def _record_refusal(
        self,
        *,
        request_id: str,
        stage: str,
        provider: str,
        model: str,
        prompt_version: str | None,
        reason: str,
    ) -> None:
        self._record_attempt(
            request_id=request_id,
            stage=stage,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            response=None,
            latency_ms=0,
            cost=None,
            currency=None,
            retry_count=0,
            refusal_reason=reason,
            from_cassette=False,
            check_verdict="refused_before_send",
        )

    def _record_ceiling(
        self,
        error: CeilingReached,
        *,
        request_id: str,
        stage: int,
        provider: str,
        model: str,
        prompt_version: str,
        retry_count: int,
    ) -> None:
        self._record_attempt(
            request_id=request_id,
            stage=stage,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            response=None,
            latency_ms=0,
            cost=None,
            currency=self.currency if error.dimension == "budget" else None,
            retry_count=retry_count,
            refusal_reason=f"ceiling_reached:{error.dimension}",
            from_cassette=False,
            check_verdict="ceiling_reached",
        )
        payload: dict[str, object] = {
            "dimension": error.dimension,
            "limit": str(error.limit),
            "observed": str(error.observed),
            "queued": True,
            "request_id": request_id,
            "stage": str(stage),
        }
        if error.retry_at is not None:
            payload["retry_at"] = error.retry_at.isoformat()
        try:
            _raise_alert(self.alerts, "model_call_ceiling_reached", payload)
        except Exception as alert_error:
            logger.error(
                "model_call_ceiling_alert_failed",
                dimension=error.dimension,
                error_type=type(alert_error).__name__,
            )

    def _settle_failed(self, reservation: BudgetReservation) -> None:
        self.budget.settle(reservation, None)

    def _estimated_cost(self, context: CallContext, request: CompletionRequest) -> Decimal:
        configured_estimate = (
            context.estimated_cost if context.estimated_cost is not None else context.cost_estimate
        )
        if configured_estimate is not None:
            return _nonnegative_decimal(configured_estimate, "estimated_cost")
        input_price = (
            context.input_price_per_1k
            if context.input_price_per_1k is not None
            else self.input_price_per_1k
        )
        output_price = (
            context.output_price_per_1k
            if context.output_price_per_1k is not None
            else self.output_price_per_1k
        )
        if input_price is None and output_price is None:
            return Decimal(0)
        input_tokens = sum(len(message.content) for message in request.messages)
        estimated = Decimal(0)
        if input_price is not None:
            estimated += Decimal(input_tokens) * input_price / _TOKENS_PER_THOUSAND
        if output_price is not None:
            estimated += Decimal(request.max_tokens) * output_price / _TOKENS_PER_THOUSAND
        return estimated

    def _actual_cost(
        self,
        context: CallContext,
        response: CompletionResponse,
        reservation: BudgetReservation,
    ) -> Decimal:
        input_price = (
            context.input_price_per_1k
            if context.input_price_per_1k is not None
            else self.input_price_per_1k
        )
        output_price = (
            context.output_price_per_1k
            if context.output_price_per_1k is not None
            else self.output_price_per_1k
        )
        if input_price is None and output_price is None:
            return reservation.estimated_cost
        if response.input_tokens is None and response.output_tokens is None:
            return reservation.estimated_cost
        cost = Decimal(0)
        if input_price is not None and response.input_tokens is not None:
            cost += Decimal(response.input_tokens) * input_price / _TOKENS_PER_THOUSAND
        if output_price is not None and response.output_tokens is not None:
            cost += Decimal(response.output_tokens) * output_price / _TOKENS_PER_THOUSAND
        return cost

    def _model(self, context: CallContext) -> str:
        model = (
            context.model
            or context.model_version
            or self.model
            or getattr(self.provider, "model", None)
        )
        if not isinstance(model, str) or not model.strip():
            raise ValueError("A model identifier is required for an LLM call.")
        return model.strip()

    def _record_model(self, context: CallContext) -> str:
        try:
            return self._model(context)
        except Exception:
            return "unknown"

    def _provider_name(self, context: CallContext, provider: LLMProvider | None = None) -> str:
        selected = provider or self.provider
        value = (
            getattr(selected, "provider_name", None) or context.provider or type(selected).__name__
        )
        return _bounded(str(value), "provider")

    def _request_id(self, context: CallContext) -> str:
        value = context.request_id or get_request_id() or get_job_run_id() or new_request_id()
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_REQUEST_ID_LENGTH:
            raise ValueError("request_id must be a non-empty string no longer than 40 characters.")
        return value.strip()

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None:
            raise ValueError("Model-call clock returned a naive datetime.")
        return now.astimezone(UTC)


_CONFIGURED_CLIENT: ModelClient | None = None
_CONFIGURED_CLIENT_LOCK = threading.RLock()


def configure(client: ModelClient) -> None:
    """Install the application call site used by the module-level wrapper."""

    if not isinstance(client, ModelClient):
        raise TypeError("configure requires a ModelClient.")
    with _CONFIGURED_CLIENT_LOCK:
        global _CONFIGURED_CLIENT
        _CONFIGURED_CLIENT = client


def configured_client() -> ModelClient:
    """Return the configured module-level client or fail explicitly."""

    with _CONFIGURED_CLIENT_LOCK:
        if _CONFIGURED_CLIENT is None:
            raise RuntimeError("The model call site has not been configured.")
        return _CONFIGURED_CLIENT


def call(
    stage: int | str | Stage,
    prompt: MaskedPrompt | object,
    prompt_version: str,
    context: CallContext | None = None,
    *,
    client: ModelClient | None = None,
) -> ModelResult:
    """The public single call site required by C-51."""

    return (client or configured_client()).call(stage, prompt, prompt_version, context)


def _make_budget(
    budget: BudgetLedger | BudgetLimits | Mapping[str, object] | None,
    clock: Clock | None,
) -> BudgetLedger:
    if isinstance(budget, BudgetLedger):
        return budget
    if isinstance(budget, BudgetLimits):
        return BudgetLedger(budget, clock=clock)
    if isinstance(budget, Mapping):
        return BudgetLedger(budget, clock=clock)
    if budget is not None:
        if not callable(getattr(budget, "reserve", None)):
            raise TypeError("budget must provide reserve() or be a validated T7 configuration.")
        return cast(BudgetLedger, budget)
    return BudgetLedger(clock=clock)


def _normalise_stage(stage: int | str | Stage) -> int:
    if isinstance(stage, Stage):
        value = int(stage)
    elif isinstance(stage, int) and not isinstance(stage, bool):
        value = stage
    elif isinstance(stage, str):
        candidate = stage.strip().lower().replace("_", "-")
        candidate = candidate.removeprefix("stage-")
        try:
            value = int(candidate)
        except ValueError as error:
            raise ValueError("stage must be one of 1 or 7.") from error
    else:
        raise ValueError("stage must be one of 1 or 7.")
    if value not in _PERMITTED_STAGES:
        raise ValueError("stage must be one of 1 or 7.")
    return value


def _verify_masked_prompt(prompt: object, expected_version: str) -> tuple[PromptMessage, ...]:
    marker = getattr(prompt, "marker", None)
    if marker is None:
        marker = getattr(prompt, "masking_marker", None)
    content: str | None = None
    messages: object = getattr(prompt, "messages", None)
    if messages is None:
        content_value = getattr(prompt, "content", None)
        if content_value is None:
            content_value = getattr(prompt, "text", None)
        if isinstance(content_value, str):
            content = content_value
            messages = (PromptMessage(role="user", content=content_value),)
    elif isinstance(messages, str):
        content = messages
        messages = (PromptMessage(role="user", content=messages),)

    if marker != MASKING_MARKER:
        raise RuntimeError("Prompt is not marked as masked; refusing before network use.")
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes):
        raise RuntimeError("Masked prompt messages are missing; refusing before network use.")
    try:
        request = CompletionRequest(messages=cast(Sequence[MessageInput], messages), model="masked")
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "Masked prompt messages are invalid; refusing before network use."
        ) from error

    embedded_version = _embedded_version(
        prompt,
        content or "\n".join(item.content for item in request.messages),
    )
    if embedded_version is not None and not _versions_match(expected_version, embedded_version):
        raise ValueError(
            f"Prompt version {embedded_version!r} does not match requested {expected_version!r}."
        )
    source = _prompt_source(prompt)
    if source is not None:
        file_version = _version_from_file(source)
        if file_version is None:
            raise ValueError(f"Prompt file {source} has no embedded version.")
        if not _versions_match(expected_version, file_version):
            raise ValueError(
                f"Prompt file version {file_version!r} does not match requested "
                f"{expected_version!r}."
            )
    else:
        filename_version = _version_from_filename(prompt)
        if filename_version is not None and not _versions_match(expected_version, filename_version):
            raise ValueError(
                f"Prompt filename version {filename_version!r} does not match requested "
                f"{expected_version!r}."
            )
    return request.messages


def _embedded_version(prompt: object, content: str) -> str | None:
    for name in ("version", "prompt_version", "embedded_version"):
        value = getattr(prompt, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    first_line = content.splitlines()[0] if content.splitlines() else ""
    match = _VERSION_RE.search(first_line)
    return match.group(1) if match else None


def _prompt_source(prompt: object) -> Path | None:
    for name in ("source_path", "path"):
        value = getattr(prompt, name, None)
        if value is not None:
            return Path(value)
    return None


def _version_from_filename(prompt: object) -> str | None:
    filename = getattr(prompt, "filename", None)
    if not isinstance(filename, str) or not filename:
        return None
    match = re.search(r"(?:^|[._-])(v[0-9]+(?:\.[0-9]+)*)(?:[._-]|$)", filename, re.IGNORECASE)
    return match.group(1) if match else None


def _version_from_file(path: Path) -> str | None:
    try:
        first_line = path.read_text(encoding="utf-8", errors="strict").splitlines()[0]
    except (OSError, UnicodeError, IndexError) as error:
        raise ValueError(
            f"Prompt file {path} could not be read for version verification."
        ) from error
    match = _VERSION_RE.search(first_line)
    return match.group(1) if match else None


def _versions_match(expected: str, actual: str) -> bool:
    left = expected.strip().lower()
    right = actual.strip().lower()
    if left == right:
        return True
    left_token = left.rsplit(".", maxsplit=1)[-1]
    right_token = right.rsplit(".", maxsplit=1)[-1]
    return left_token == right_token or (
        _VERSION_TOKEN_RE.fullmatch(left_token) is not None
        and _VERSION_TOKEN_RE.fullmatch(right_token) is not None
        and left_token == right_token
    )


def _validate_prompt_version(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 50:
        raise ValueError("prompt_version must be a bounded non-empty string.")
    if any(character in value for character in "\r\n\\/"):
        raise ValueError("prompt_version must not contain path or control characters.")
    return value.strip()


def _write_record(writer: object, record: ModelCallRecord) -> None:
    target = cast(Any, writer)
    try:
        if callable(getattr(target, "record", None)):
            target.record(record)
        elif callable(getattr(target, "write", None)):
            target.write(record)
        elif callable(getattr(target, "add", None)):
            target.add(record)
        elif callable(target):
            target(record)
        elif callable(getattr(target, "append", None)):
            target.append(record)
        else:
            raise TypeError(
                "model_calls must provide record(), write(), add(), append(), or be callable."
            )
    except Exception as error:
        raise ModelCallPersistenceError(
            "The model call record could not be persisted; the model result is refused."
        ) from error


def _raise_alert(alerts: object, event: str, payload: Mapping[str, object]) -> None:
    if alerts is None:
        return
    target = cast(Any, alerts)
    if callable(getattr(target, "alert", None)):
        target.alert(event, payload)
        return
    if callable(getattr(target, "raise_alert", None)):
        target.raise_alert(event, payload)
        return
    if callable(getattr(target, "emit", None)):
        target.emit(event, payload)
        return
    if callable(target):
        try:
            target(event, payload)
        except TypeError:
            target(payload)
        return
    if callable(getattr(target, "append", None)):
        target.append({"event": event, **dict(payload)})
        return
    raise TypeError("alerts must provide alert(), raise_alert(), emit(), append(), or be callable.")


def _retryable(error: ProviderUnavailable) -> bool:
    return _safe_provider_reason(error) != "cassette miss"


def _provider_reason(error: BaseException) -> str:
    if isinstance(error, ProviderAuthError):
        return "authentication_failure"
    if isinstance(error, ProviderUnavailable):
        return f"provider_unavailable:{_safe_provider_reason(error)}"
    if isinstance(error, ProviderError):
        return "provider_refused"
    return f"provider_error:{type(error).__name__}"


def _safe_provider_reason(error: ProviderUnavailable) -> str:
    reason = error.reason
    if reason == "timeout":
        return "timeout"
    if reason == "cassette miss":
        return "cassette miss"
    if reason == "transport failure":
        return "transport failure"
    if isinstance(reason, str) and re.fullmatch(r"http status [0-9]{3}", reason):
        return reason
    return "provider unavailable"


def _validation_reason(error: BaseException) -> str:
    if isinstance(error, ComponentNotRegistered):
        return "component_not_registered"
    if isinstance(error, ComponentNotApproved):
        return "component_not_approved"
    if isinstance(error, RuntimeError):
        return "unmasked_prompt"
    if isinstance(error, ValueError) and "stage" in str(error).lower():
        return "invalid_stage"
    if isinstance(error, ValueError) and "version" in str(error).lower():
        return "prompt_version_mismatch"
    if isinstance(error, ValueError) and "model" in str(error).lower():
        return "model_invalid"
    return f"validation_refused:{type(error).__name__}"


def _record_prompt_version(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _record_stage(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _bounded(str(value), "stage")


def _record_model(context: CallContext) -> str:
    value = context.model
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _bounded(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string.")
    limit = _MAX_RECORD_TEXT_LENGTH if name == "refusal_reason" else 50
    if len(value) > limit:
        raise ValueError(f"{name} exceeds its persistence limit.")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when configured.")
    return value.strip()


def _optional_decimal(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative decimal.")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative decimal.") from error
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal.")
    return decimal_value


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    result = _optional_decimal(value, name)
    if result is None:
        raise ValueError(f"{name} is required.")
    return result


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 300:
        raise ValueError("timeout_seconds must be greater than zero and at most 300 seconds.")
    return float(value)


def _max_tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_tokens must be a positive integer.")
    return value


def _temperature(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 2:
        raise ValueError("temperature must be between zero and two.")
    return float(value)


def _currency(value: object) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
        raise ValueError("currency must be a three-letter code.")
    return value.upper()


__all__ = [
    "AlertSink",
    "CallContext",
    "DEFAULT_CURRENCY",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "InMemoryModelCallWriter",
    "LLMClient",
    "MASKING_MARKER",
    "MaskedPrompt",
    "ModelCallPersistenceError",
    "ModelCallRecord",
    "ModelCallWriter",
    "ModelClient",
    "ModelResult",
    "SqlAlchemyModelCallWriter",
    "Stage",
    "CeilingReached",
    "call",
    "configure",
    "configured_client",
]


LLMClient = ModelClient
