"""Browser route for the why-panel drawer over every decision stage (`T-071`).

`C-10` names one route — `GET /why/{subject_type}/{subject_id}` — serving
every kind of subject the why-panel can explain. This module resolves and
authorises the subject (`services/explain.py` is deliberately unscoped and
leaves that to its caller), then hands the ordered, named stage records
`T-070` already built to the drawer template. No stage name, threshold
column or field set is decided here or in the template: everything shown is
driven by `audit.trace_reader.ExplainStage`, so a future statistical stage
needs no template change.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.audit.trace_reader import (
    ExplainStage,
    ExplainSubjectType,
    validate_subject_type,
)
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast
from covenant_radar.db.repositories.base import RepositoryBase
from covenant_radar.db.repositories.memo import MemoRepository
from covenant_radar.db.repositories.trace import TraceSubject
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.trace import TRACE_STAGE_MIN
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.explain import explain
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.memo import MemoBlockView, build_persisted_memo_block

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_READ = requires(Permission.VIEW_BORROWER)
_READ_DEP = Depends(_READ)

# One place naming every subject type's owning model, so the route can
# resolve and scope-check the subject before any trace row is read.  Kept in
# lock-step with `ExplainSubjectType`; a subject type with no entry here is a
# configuration error, not a request the caller can trigger.
_SUBJECT_MODELS: Final[Mapping[str, type]] = {
    ExplainSubjectType.COVENANT_TEST.value: CovenantTest,
    ExplainSubjectType.BORROWER.value: Borrower,
    ExplainSubjectType.FORECAST.value: Forecast,
}

# Source-record types that already have a viewable route. A type absent here
# is shown without a link rather than a link that would 404 — the panel
# never fabricates a destination it cannot resolve.
_SOURCE_ROUTES: Final[Mapping[str, str]] = {
    "document": "/documents/{id}/view",
    "evidence_item": "/api/v1/evidence/{id}",
    "forecast": "/api/v1/forecasts/{id}",
}

_SOURCE_TYPE_LABELS: Final[Mapping[str, str]] = {
    "covenant_version": "Covenant version",
    "financial_period": "Financial period",
    "evidence_item": "Evidence item",
    "forecast": "Forecast",
    "forecast_run": "Forecast run",
    "document": "Document",
}

_LABELS: Final[Mapping[str, str]] = {
    "title": "Why this decision",
    "close": "Close",
    "stage_not_run": "This stage has not run.",
    "stage_decided_by": "Decided by",
    "decider_code": "Code",
    "decider_model": "Model",
    "decider_statistical": "Statistical",
    "version_label_code": "Rule version",
    "version_label_model": "Prompt version",
    "version_label_statistical": "Model version",
    "stage_confidence": "Confidence",
    "stage_received": "What it received",
    "stage_produced": "What it produced",
    "technical_detail": "Technical detail",
    "decision_summary": "Decision summary",
    "reasoning_summary": "Reason this result was produced",
    "thresholds_heading": "Thresholds compared",
    "thresholds_empty": "No thresholds were compared at this stage.",
    "threshold_col_name": "Threshold",
    "threshold_col_value": "Value",
    "threshold_col_observed": "Observed",
    "threshold_col_side": "Side",
    "side_above": "Above",
    "side_below": "Below",
    "side_at": "At",
    "sources_heading": "Source records",
    "no_sources": "No source records are attached to this stage.",
    "source_unresolvable": "Not directly viewable.",
    "suppressed_heading": "This forecast is suppressed.",
    "suppressed_default": "Confidence fell below the floor required to show a probability.",
    "suppressed_limiting_factor": "Limiting confidence factor: {name}.",
    "empty_value": "—",
    "value_yes": "Yes",
    "value_no": "No",
    "borrower": "Borrower",
    "covenant_test": "Covenant test",
    "forecast": "Forecast",
    "ai_explanation_label": "AI-generated explanation",
    "ai_explanation_empty_title": "No AI explanation has been generated yet",
    "ai_explanation_empty_message": (
        "The stages below explain the stored calculation. Generate the separate grounded AI "
        "explanation when you want model-drafted connecting prose."
    ),
    "ai_explanation_generate": "Generate AI explanation",
    "ai_explanation_view_full": "View full AI explanation",
    "ai_explanation_next_step": "Recommended next step",
    "ai_explanation_provenance": "Generated by {provider} · {model} · prompt {prompt}",
}

_SIDE_LABEL_KEYS: Final[Mapping[str, str]] = {
    "above": "side_above",
    "below": "side_below",
    "at": "side_at",
}

_VERSION_LABEL_KEYS: Final[Mapping[str, str]] = {
    "code": "version_label_code",
    "model": "version_label_model",
    "statistical": "version_label_statistical",
}

_DECIDER_LABEL_KEYS: Final[Mapping[str, str]] = {
    "code": "decider_code",
    "model": "decider_model",
    "statistical": "decider_statistical",
}

# Stages 1, 5 and 6 have no writer for the demo's directly-registered
# covenants (intake) or at all yet (intervention, triage) — `_LABELS`'s
# generic "This stage has not run." is truthful but leaves a reader no way
# to tell "nothing has happened" from "this decision exists, just not
# traced here." Each message below says which one it is, and where to look
# for the real decision when one exists.
_STAGE_NOT_RUN_MESSAGES: Final[Mapping[int, str]] = {
    1: (
        "No intake record exists for this borrower's covenants. Covenants added "
        "directly by a credit officer, rather than confirmed from an uploaded "
        "sanction letter, do not produce this stage."
    ),
    5: (
        "No intervention decision is traced at this stage yet. Simulated "
        "interventions for this borrower are available from the case file's "
        "simulator, and can be compared there against doing nothing."
    ),
    6: (
        "No triage decision is traced at this stage yet. This borrower's band "
        "and urgency, when ranked, are shown on the portfolio queue."
    ),
}

_VERDICT_LABELS: Final[Mapping[str, str]] = {
    "pass": "passing",
    "warning": "in warning",
    "breach": "in breach",
    "breach_cure_open": "in breach, with an open cure period",
    "stale": "stale",
    "not_computable": "not computable",
}


@dataclass(frozen=True, slots=True)
class ThresholdView:
    """One threshold comparison row, its four cells always populated."""

    name: str
    value_display: str
    observed_display: str
    side_display: str


@dataclass(frozen=True, slots=True)
class SourceView:
    """One source record, linked only when a viewable route is known."""

    label: str
    href: str


@dataclass(frozen=True, slots=True)
class StageView:
    """One stage, shaped for the template to render without deciding
    anything — every value here is already resolved from the record."""

    stage: int
    name: str
    not_run: bool
    not_run_message: str
    decider: str | None
    decider_label: str
    version_label: str
    rule_or_prompt_version: str | None
    confidence_display: str | None
    inputs: Mapping[str, object]
    outputs: Mapping[str, object]
    thresholds: tuple[ThresholdView, ...]
    sources: tuple[SourceView, ...]
    suppressed: bool
    suppression_reason: str | None
    limiting_factor: str | None
    decision_summary: str | None
    reasoning_summary: str | None


def create_why_router(
    session: Session,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected why-panel route over one database session."""
    if not is_database_session(session):
        raise TypeError("create_why_router requires a SQLAlchemy Session.")
    router = APIRouter(tags=["why-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/why/{subject_type}/{subject_id}", response_class=HTMLResponse, name="why_panel")
    async def why_panel(
        request: Request,
        subject_type: str,
        subject_id: str,
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        try:
            validated_type = validate_subject_type(subject_type)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            validated_id = UUID(subject_id)
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail="The why-panel subject id must be a UUID.",
            ) from error

        model = _SUBJECT_MODELS[validated_type]
        scope = resolve_scope(principal, session)
        record = RepositoryBase(session, model).get(validated_id, scope=scope)
        if record is None:
            raise NotFound(
                f"{validated_type} {validated_id} was not found within the current scope."
            )

        stages = explain(session, TraceSubject(validated_type, validated_id))
        borrower = _subject_borrower(session, validated_type, record, scope)
        if validated_type == ExplainSubjectType.BORROWER.value and borrower is not None:
            stages = _fill_borrower_stages(session, stages, borrower.id)
        ai_explanation = _ai_explanation(
            session,
            stages,
            scope,
            borrower_id=borrower.id if borrower is not None else None,
        )
        return _render(
            request,
            fallback_environment,
            principal=principal,
            subject_type=validated_type,
            subject_id=str(validated_id),
            subject_label=_subject_label(validated_type, record),
            stages=tuple(_build_stage_view(stage) for stage in stages),
            ai_explanation=ai_explanation,
            ai_explanation_href=(
                f"/borrowers/{borrower.reference}#case-memo" if borrower is not None else ""
            ),
            can_generate_ai_explanation=principal.has(Permission.GENERATE_MEMO),
        )

    return router


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template_name = (
        "screens/why/_drawer.html" if _is_htmx_request(request) else "screens/why/panel.html"
    )
    template = environment.get_template(template_name)
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    theme = theme_for_request(request)
    values = {
        "request": request,
        "principal": principal,
        "locale": locale,
        "theme": theme,
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        **context,
    }
    return HTMLResponse(
        template.render(**values),
        status_code=status_code,
        headers={"Vary": "HX-Request"},
    )


def _is_htmx_request(request: Request) -> bool:
    """Return whether HTMX explicitly requested a fragment response.

    Checking for the affirmative value avoids treating a malformed or
    explicitly false header as a fragment request, which keeps direct links
    and intermediary caches on the full-page representation.
    """

    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _subject_label(subject_type: str, record: object) -> str:
    if subject_type == ExplainSubjectType.BORROWER.value:
        legal_name = getattr(record, "legal_name", None)
        reference = getattr(record, "reference", None)
        return legal_name or reference or f"{_LABELS['borrower']} {getattr(record, 'id', '')}"
    if subject_type == ExplainSubjectType.COVENANT_TEST.value:
        as_of_date = getattr(record, "as_of_date", None)
        if as_of_date is not None:
            return f"{_LABELS['covenant_test']} — {as_of_date}"
        return f"{_LABELS['covenant_test']} {getattr(record, 'id', '')}"
    if subject_type == ExplainSubjectType.FORECAST.value:
        horizon_days = getattr(record, "horizon_days", None)
        if horizon_days is not None:
            return f"{_LABELS['forecast']} — {horizon_days}d horizon"
        return f"{_LABELS['forecast']} {getattr(record, 'id', '')}"
    return str(getattr(record, "id", record))


def _ai_explanation(
    session: Session,
    stages: Sequence[ExplainStage],
    scope: Scope,
    *,
    borrower_id: UUID | None,
) -> MemoBlockView | None:
    """Load the checked memo named by stage 7 for the visible subject.

    Stage traces intentionally retain compact audit facts. The actual model
    prose lives on the immutable, shape-checked ``Memo`` row, so the browser
    explanation reads that record instead of pretending the trace metadata is
    the explanation. This also makes already-generated memos visible without
    requiring them to be regenerated after this UI change.
    """

    memo_id: UUID | None = None
    for stage in stages:
        if stage.stage != 7 or stage.not_run:
            continue
        raw_memo_id = stage.outputs.get("memo_id")
        if not isinstance(raw_memo_id, str):
            continue
        try:
            memo_id = UUID(raw_memo_id)
        except ValueError:
            continue
    repository = MemoRepository(session)
    memo = repository.get(memo_id, scope=scope) if memo_id is not None else None
    if memo is None and borrower_id is not None:
        # Queue links explain a forecast subject, while stage 7 is correctly
        # traced against the borrower whose complete facts the memo covers.
        # Bridge those two explanation surfaces through their already-scoped
        # owner so the queue's "Why this score" drawer can show the latest
        # checked model prose instead of claiming no explanation exists.
        borrower_memos = repository.for_borrower(borrower_id, scope=scope)
        memo = borrower_memos[0] if borrower_memos else None
    if memo is None:
        return None
    try:
        return build_persisted_memo_block(memo)
    except (TypeError, ValueError):
        # A legacy or damaged row must not take the deterministic why-panel
        # down with it. The raw stage remains visible and the full borrower
        # page will report the same data issue through its normal error path.
        return None


def _subject_borrower(
    session: Session,
    subject_type: str,
    record: object,
    scope: Scope,
) -> Borrower | None:
    """Resolve the already-authorised subject to its owning borrower."""

    if subject_type == ExplainSubjectType.BORROWER.value:
        return record if isinstance(record, Borrower) else None
    version_id = getattr(record, "covenant_version_id", None)
    if not isinstance(version_id, UUID):
        return None
    borrower_id = session.scalar(
        select(Borrower.id)
        .join(Facility, Facility.borrower_id == Borrower.id)
        .join(Covenant, Covenant.facility_id == Facility.id)
        .join(CovenantVersion, CovenantVersion.covenant_id == Covenant.id)
        .where(CovenantVersion.id == version_id)
    )
    if not isinstance(borrower_id, UUID):
        return None
    return RepositoryBase(session, Borrower).get(borrower_id, scope=scope)


def _fill_borrower_stages(
    session: Session,
    stages: Sequence[ExplainStage],
    borrower_id: UUID,
) -> tuple[ExplainStage, ...]:
    """Complete a borrower-level why-panel with stages recorded elsewhere.

    Stage 2 (Covenant Engine) writes under its covenant test's own subject
    and stage 4 (Forecast) under its forecast's own subject (`T-037`,
    `T-058`) rather than the borrower's, so a plain borrower-subject read
    shows both as ``not_run`` even when they plainly ran. This borrows the
    borrower's own latest covenant test / forecast trace for exactly those
    two stage slots, and only when the borrower-subject read had nothing —
    a stage already present there is left untouched.
    """

    result = list(stages)
    by_stage = {stage.stage: index for index, stage in enumerate(result)}
    for stage_number, model, subject_type, order_column in (
        (2, CovenantTest, ExplainSubjectType.COVENANT_TEST, CovenantTest.as_of_date),
        (4, Forecast, ExplainSubjectType.FORECAST, Forecast.data_as_of),
    ):
        index = by_stage.get(stage_number)
        if index is None or not result[index].not_run:
            continue
        subject_id = session.scalar(
            select(model.id)
            .join(CovenantVersion, model.covenant_version_id == CovenantVersion.id)
            .join(Covenant, CovenantVersion.covenant_id == Covenant.id)
            .join(Facility, Covenant.facility_id == Facility.id)
            .where(Facility.borrower_id == borrower_id)
            .order_by(order_column.desc())
            .limit(1)
        )
        if not isinstance(subject_id, UUID):
            continue
        # `explain` always returns one padded entry per stage, in stage
        # order, so the wanted stage sits at a fixed offset from the start.
        replacement = explain(session, TraceSubject(subject_type.value, subject_id))[
            stage_number - TRACE_STAGE_MIN
        ]
        if not replacement.not_run:
            result[index] = replacement
    return tuple(result)


def _build_stage_view(stage: ExplainStage) -> StageView:
    if stage.not_run:
        return StageView(
            stage=stage.stage,
            name=stage.name,
            not_run=True,
            not_run_message=_STAGE_NOT_RUN_MESSAGES.get(stage.stage, _LABELS["stage_not_run"]),
            decider=None,
            decider_label="",
            version_label="",
            rule_or_prompt_version=None,
            confidence_display=None,
            inputs={},
            outputs={},
            thresholds=(),
            sources=(),
            suppressed=False,
            suppression_reason=None,
            limiting_factor=None,
            decision_summary=None,
            reasoning_summary=None,
        )

    decider = stage.decider or ""
    outputs = stage.outputs
    suppressed = bool(outputs.get("probability_suppressed")) or bool(
        outputs.get("below_confidence_floor")
    )
    suppression_reason = outputs.get("reason")
    if not isinstance(suppression_reason, str) or not suppression_reason.strip():
        suppression_reason = None
    limiting_factor = (
        _limiting_confidence_factor(stage.inputs.get("confidence_factors")) if suppressed else None
    )
    decision_summary, reasoning_summary = _stage_summaries(stage)

    return StageView(
        stage=stage.stage,
        name=stage.name,
        not_run=False,
        not_run_message="",
        decider=decider,
        decider_label=_LABELS.get(_DECIDER_LABEL_KEYS.get(decider, ""), decider.title()),
        version_label=_LABELS.get(_VERSION_LABEL_KEYS.get(decider, ""), ""),
        rule_or_prompt_version=stage.rule_or_prompt_version,
        confidence_display=(
            _percentage(stage.confidence) if isinstance(stage.confidence, Decimal) else None
        ),
        inputs=_format_nested(stage.inputs),
        outputs=_format_nested(outputs),
        thresholds=tuple(_threshold_view(entry) for entry in stage.thresholds_compared),
        sources=tuple(
            view for source in stage.sources if (view := _source_view(source)) is not None
        ),
        suppressed=suppressed,
        suppression_reason=suppression_reason,
        limiting_factor=limiting_factor,
        decision_summary=decision_summary,
        reasoning_summary=reasoning_summary,
    )


def _stage_summaries(stage: ExplainStage) -> tuple[str | None, str | None]:
    """Turn dense stage 2/4 trace fields into a reviewable plain-language lead.

    The raw inputs and outputs remain available (collapsed) below this
    summary. This layer only names stored values; it never asks a model to
    reinterpret an audited decision.
    """

    if stage.stage == 2:
        return _stage2_summary(stage)
    if stage.stage != 4:
        return None, None
    source = str(stage.outputs.get("probability_source", "deterministic")).strip().lower()
    probability = _percentage(stage.outputs.get("probability"))
    confidence = _percentage(stage.outputs.get("confidence"))
    method = "ML model" if source == "ml" else "deterministic rule"
    decision = f"The {method} supplied {probability} breach risk"
    horizon = stage.outputs.get("horizon_days", stage.inputs.get("horizon_days"))
    if isinstance(horizon, int) and not isinstance(horizon, bool):
        decision += f" at {horizon} days"
    decision += f", with {confidence} confidence."

    drivers = stage.outputs.get("drivers")
    driver_reason: str | None = None
    if isinstance(drivers, Sequence) and not isinstance(drivers, str | bytes | bytearray):
        named: list[tuple[Decimal, str]] = []
        for driver in drivers:
            if not isinstance(driver, Mapping):
                continue
            name = driver.get("name")
            share = _as_decimal(driver.get("share"))
            if (
                isinstance(name, str)
                and name.strip().lower() not in {"other", "neutral"}
                and share is not None
            ):
                named.append((share, name.strip().replace("_", " ")))
        if named:
            share, name = max(named, key=lambda item: (item[0], item[1]))
            driver_reason = f"The largest stored attribution is {name} at {_percentage(share)}."

    fallback = stage.outputs.get("fallback_reason")
    reason_parts = [
        "The stored probability mapping combines covenant-threshold distance, "
        "trajectory velocity and sustained-evidence pressure."
    ]
    if driver_reason:
        reason_parts.append(driver_reason)
    if isinstance(fallback, str) and fallback.strip():
        reason_parts.append(fallback.strip() + ".")
    return decision, " ".join(reason_parts)


def _stage2_summary(stage: ExplainStage) -> tuple[str | None, str | None]:
    outputs = stage.outputs
    verdict = str(outputs.get("verdict", "")).strip().lower()
    if not verdict:
        return None, None
    verdict_label = _VERDICT_LABELS.get(verdict, verdict.replace("_", " "))
    ratio_code = stage.inputs.get("ratio_code")
    ratio_name = (
        str(ratio_code).replace("_", " ").strip().capitalize() if ratio_code else "This covenant"
    )
    value = _as_decimal(outputs.get("value"))
    threshold = _as_decimal(outputs.get("threshold_used"))
    direction = str(stage.inputs.get("direction", "")).strip().lower()
    direction_word = {"min": "at least", "max": "at most"}.get(direction, "")

    decision = f"{ratio_name} is {verdict_label}"
    if value is not None and threshold is not None:
        decision += (
            f": {_trimmed_decimal(value)} observed against a {_trimmed_decimal(threshold)} "
            f"threshold"
        )
        if direction_word:
            decision += f" ({direction_word})"
    decision += "."

    reason_parts: list[str] = []
    headroom = _as_decimal(outputs.get("headroom_pct"))
    if headroom is not None:
        reason_parts.append(f"Headroom is {_trimmed_decimal(headroom)} percentage points.")
    cure_ends_on = outputs.get("cure_ends_on")
    if verdict == "breach_cure_open" and isinstance(cure_ends_on, str) and cure_ends_on.strip():
        reason_parts.append(f"A cure period is open until {cure_ends_on}.")
    if outputs.get("exception_applied"):
        reason_parts.append("An approved exception was applied to this test.")
    if outputs.get("waiver_applied"):
        reason_parts.append("An approved waiver was applied to this test.")
    stale_reason = outputs.get("stale_reason")
    if verdict == "stale" and isinstance(stale_reason, str) and stale_reason.strip():
        reason_parts.append(stale_reason.strip())
    reason_context = outputs.get("reason_context")
    if (
        verdict == "not_computable"
        and isinstance(reason_context, Mapping)
        and outputs.get("reason")
    ):
        reason_parts.append(str(outputs["reason"]).replace("_", " ") + ".")
    return decision, " ".join(reason_parts) if reason_parts else None


def _percentage(value: object) -> str:
    number = _as_decimal(value)
    if number is None:
        return "an unrecorded"
    percent = (number * Decimal("100")).quantize(Decimal("1"))
    return f"{percent}%"


def _threshold_view(entry: Mapping[str, object]) -> ThresholdView:
    side = entry.get("side")
    side_key = _SIDE_LABEL_KEYS.get(str(side), "")
    side_display = _LABELS.get(side_key, str(side) if side is not None else _LABELS["empty_value"])
    return ThresholdView(
        name=_humanize_key(str(entry.get("name", ""))),
        value_display=_display(entry.get("value")),
        observed_display=_display(entry.get("observed")),
        side_display=side_display,
    )


def _display(value: object) -> str:
    if value is None:
        return _LABELS["empty_value"]
    if isinstance(value, bool):
        return _LABELS["value_yes"] if value else _LABELS["value_no"]
    number = _as_decimal(value)
    if number is not None:
        return _trimmed_decimal(number)
    return str(value)


def _trimmed_decimal(number: Decimal) -> str:
    """Format a trace decimal without its stored 8-place scale.

    `RatioValue` columns persist every ratio/threshold at a fixed scale of 8
    (`db/models/_decimal.py`), and the trace JSON boundary keeps a `Decimal`
    as its exact text (`domain/trace.py`), so an unformatted threshold reads
    as ``1.08000000`` instead of ``1.08``.
    """

    try:
        number = number.quantize(Decimal("0.0001"))
    except InvalidOperation:
        pass
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _format_nested(value: object) -> object:
    """Recursively apply :func:`_trimmed_decimal` to a stage's stored payload.

    ``inputs``/``outputs`` are read straight from trace JSON, so any ratio or
    threshold buried in them carries the same 8-place scale as a threshold
    row. The macro that renders them (``why/_stage.html``) prints a scalar
    as-is, so the trim has to happen here rather than in the template.
    """

    if isinstance(value, Mapping):
        return {_humanize_key(key): _format_nested(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_format_nested(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    number = _as_decimal(value)
    if number is not None:
        return _trimmed_decimal(number)
    # A drafted sentence (stage 7's headline/summary) can carry the same
    # over-precise ratio embedded in free text rather than as a lone value —
    # trim those occurrences too rather than only whole-value matches.
    return _LONG_DECIMAL_RE.sub(_replace_long_decimal, value) if isinstance(value, str) else value


def _humanize_key(key: str) -> str:
    """Turn a trace field's internal snake_case name into a reader-facing label.

    The template prints every ``inputs``/``outputs`` key verbatim as a
    definition-list term, so an unrecognised field like ``exception_id``
    otherwise reaches the screen exactly as it is stored.
    """

    return key.replace("_", " ").strip().capitalize()


_LONG_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d+\.\d{4,})(?!\d)")


def _replace_long_decimal(match: re.Match[str]) -> str:
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return match.group(1)
    return _trimmed_decimal(number)


def _source_view(source: object) -> SourceView | None:
    if not isinstance(source, Mapping):
        text = str(source).strip()
        return SourceView(label=text, href="") if text else None
    type_name = source.get("type")
    identifier = source.get("id")
    if not isinstance(type_name, str) or not type_name.strip() or identifier is None:
        return None
    type_label = _SOURCE_TYPE_LABELS.get(type_name, type_name.replace("_", " ").capitalize())
    label = f"{type_label} — {identifier}"
    route_template = _SOURCE_ROUTES.get(type_name)
    href = route_template.format(id=identifier) if route_template else ""
    return SourceView(label=label, href=href)


def _limiting_confidence_factor(raw: object) -> str | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes | bytearray):
        return None
    best_name: str | None = None
    best_value: Decimal | None = None
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        value = _as_decimal(entry.get("value"))
        if not isinstance(name, str) or not name.strip() or value is None:
            continue
        if best_value is None or value < best_value:
            best_value = value
            best_name = name
    return best_name


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


__all__ = ["create_why_router"]
