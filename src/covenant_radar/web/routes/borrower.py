"""Scoped borrower case-file screen (T-075, contract C-02) and its memo action
(contract C-08).

The case-file route is intentionally read-only.  The view model owns the
case-file read shape and receives a scope before it can load any borrower or
child record.  It does not expose a fallback borrower or an unscoped lookup,
so an unknown borrower and an out-of-scope borrower have the same 404
behaviour.

``POST /memos`` is the one action this module adds.  It never constructs the
model client or the generation service itself — the composition root injects
a ``MemoGenerator``, exactly as it injects the stage-1 proposal generator into
the intake router — so the single model call site stays where it belongs and
this screen stays renderable when no provider is configured at all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import ExternalServiceError, NotFound, ValidationError
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.repositories.memo import MemoRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.memo.slots import MemoRecords
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.memo import MemoGenerationOutcome
from covenant_radar.services.memo_records import collect_memo_records
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.borrower import load_borrower_case_file
from covenant_radar.web.view_models.memo import (
    NO_FORECAST_MESSAGE,
    NOT_CONFIGURED_MESSAGE,
    MemoBlockView,
    build_memo_block,
    build_persisted_memo_block,
    memo_unavailable,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_READ = requires(Permission.VIEW_BORROWER)
_READ_DEP = Depends(_READ)
_MEMO = requires(Permission.GENERATE_MEMO)
_MEMO_DEP = Depends(_MEMO)
_MAX_FORM_BYTES = 64 * 1024
_MAX_SIMULATION_IDS = 20


class MemoExporter(Protocol):
    """The composition root's per-request ``MemoExportService.export``.

    Injected for the same reason as ``MemoGenerator``: the export service
    needs a byte store and a renderer, both of which the composition root
    owns. Declared structurally so this screen stays renderable — and the
    export control stays honestly absent — when neither is configured.
    """

    def __call__(
        self,
        memo_id: UUID,
        *,
        principal: Principal,
        scope: Scope,
        export_format: str,
    ) -> MemoExportDownload: ...


@dataclass(frozen=True, slots=True)
class MemoExportDownload:
    """One rendered memo artefact ready to be handed to the browser."""

    content: bytes
    content_type: str
    filename: str
    integrity_hash: str


class MemoGenerator(Protocol):
    """The composition root's bound ``MemoGenerationService.generate``.

    Declared structurally so this module never imports the model client or
    builds the generation service, both of which belong to the composition
    root (`tests/security/test_single_call_site.py`).
    """

    def __call__(
        self,
        *,
        borrower_id: UUID,
        records: MemoRecords,
        run_id: UUID | None,
        case_id: UUID | None,
        actor_id: UUID | None,
    ) -> MemoGenerationOutcome:
        """Draft, check and either persist or refuse one memo."""


_LABELS = {
    "title_suffix": "Case file",
    "reference": "Borrower reference",
    "header_label": "Borrower facts",
    "covenant_title": "Covenant position",
    "covenant_reference": "Covenant",
    "covenant_facility": "Facility",
    "covenant_value": "Value",
    "covenant_agreed_threshold": "Initially agreed",
    "covenant_threshold": "Threshold in force",
    "covenant_headroom": "Headroom",
    "covenant_verdict": "Verdict",
    "covenant_next_test": "Next test",
    "covenant_trajectory": "Trajectory",
    "forecast_title": "Forecast trajectory",
    "forecast_ledger": "Forecast figures",
    "forecast_horizon": "Horizon",
    "forecast_probability": "Probability",
    "forecast_confidence": "Confidence",
    "forecast_crossing": "Crossing",
    "forecast_method": "Decision method",
    "forecast_trajectory": "Stored daily trajectory",
    "forecast_explanations": "Prediction explanations and provenance",
    "forecast_explanations_note": (
        "Each explanation is reconstructed from stored inputs. LLM-drafted prose is confined "
        "to the separately labelled, grounded memo."
    ),
    "forecast_operational_source": "Operational source",
    "forecast_rule_version": "Rule version",
    "forecast_deterministic_probability": "Deterministic probability",
    "forecast_ml_probability": "ML probability",
    "forecast_model_version": "Model version",
    "forecast_artifact_checksum": "Artifact checksum",
    "forecast_feature_snapshot": "Feature snapshot hash",
    "forecast_rule_factors": "Deterministic rule factors",
    "forecast_factor": "Factor",
    "forecast_input": "Input",
    "forecast_normalized": "Normalised",
    "forecast_weight": "Weight",
    "forecast_contribution": "Contribution",
    "forecast_drivers": "Attributed drivers",
    "forecast_model_contributions": "ML feature contributions",
    "forecast_no_model_contributions": (
        "This model artifact did not disclose per-feature contributions; its version, checksum "
        "and feature snapshot remain recorded."
    ),
    "forecast_citations": "Citations",
    "forecast_explanation_provenance": "Why this explanation is supportable",
    # The case file runs to several thousand pixels per covenant, so the
    # evidence behind a forecast opens in a drawer rather than extending the
    # scroll. These name the controls that open them, the covenant switcher
    # above them, and the strip that selects between the evidence panels.
    "sections_label": "Case sections",
    # The financials tab. Its wording carries one job the other tabs do not:
    # keeping the covenanted figures (stored engine verdicts) and the
    # uncovenanted indicators (computed context) unmistakably apart, so no
    # reader can take an indicative ratio for a contractual position.
    "financials_title": "Financials",
    "financials_eyebrow": "Filed statements",
    "financials_manage": "Statement imports",
    "financials_empty_title": "No statements filed",
    "financials_covenanted": "Covenanted ratios",
    "financials_covenanted_note": (
        "Every value, threshold, headroom and verdict below is read from the covenant test "
        "the engine stored for that period. Nothing on this panel re-tests a covenant."
    ),
    "financials_context": "Indicative ratios — not covenanted",
    "financials_context_note": (
        "No covenant on this borrower tests these. They are computed from the same filed "
        "lines using the standard library formula, as context for the covenants above, and "
        "carry no contractual force."
    ),
    "financials_lines": "Filed statement lines",
    "financials_lines_note": (
        'Every ratio above is computed from these lines. "Feeds" names the covenant a '
        "movement in that line lands on."
    ),
    "financials_line": "Line",
    "financials_latest": "Latest",
    "financials_headroom": "Headroom",
    "financials_trend": "Trend",
    "financials_audited": "audited",
    "financials_feeds": "Feeds:",
    "financials_formula": "Formula:",
    "financials_what_moved": "What moved it:",
    "financials_chart_key": "Dashed line is the threshold; the shaded band is the breaching side.",
    "financials_no_threshold": "No threshold — this ratio is not covenanted.",
    "signals_title": "Seven signal families",
    "forecast_covenants_nav": "Covenants on this case",
    "forecast_open_explanations": "Explanations and provenance",
    "forecast_open_insights": "Actionable insights",
    "forecast_close": "Close",
    "forecast_actionable_insights": "Possible actionable insights",
    "forecast_actionable_note": (
        "These are active, bank-owned catalogue actions applicable to this covenant. They are "
        "advisory candidates, not automatic credit decisions; simulate before prioritising."
    ),
    "forecast_action_owner": "Owner",
    "forecast_action_effect": "Effect model",
    "forecast_action_assumptions": "Assumptions",
    "forecast_action_approval": "Human approval required",
    "forecast_action_simulate": "Test in simulator",
    "forecast_no_actions": "No active catalogue action is applicable to this covenant class.",
    "horizon_title": "Horizon",
    "horizon_selected_day": "Selected day",
    "horizon_move": "Move through stored days",
    "horizon_named": "Named horizons",
    "horizon_today": "Today",
    "horizon_days_30": "30 days",
    "horizon_days_60": "60 days",
    "horizon_days_90": "90 days",
    "horizon_no_javascript": "The named horizons remain available without JavaScript.",
    "horizon_projected_value": "Projected value",
    "horizon_headroom": "Headroom",
    "horizon_probability": "Probability",
    "horizon_confidence": "Confidence",
    "horizon_crossing": "Crossing",
    "horizon_not_selected": "Not selected",
    "horizon_drivers": "Drivers at selected day",
    "horizon_no_driver": "No driver record is selected yet.",
    "evidence_title": "Evidence",
    "evidence_ready": "Evidence has been recorded for this borrower.",
    "evidence_type": "Evidence type",
    "evidence_seen": "First / last seen",
    "evidence_persistence": "Persistence",
    "evidence_materiality": "Materiality",
    "evidence_decay": "Decay",
    "evidence_pressure": "Pressure",
    "evidence_state": "State",
    "evidence_counts": "Counts toward pressure",
    "evidence_not_counting": "Does not count toward pressure",
    "evidence_superseded_by": "Superseded by",
    "evidence_supersedes": "Supersedes",
    "documents_title": "Source documents",
    "documents_empty": "No source documents are available for this borrower.",
    "documents_upload": "Upload a document",
    "documents_uploaded": "Uploaded",
    "documents_extraction": "Extraction",
    "documents_open": "Open document",
    "actions_title": "Case actions",
    "actions_why": "Why this decision",
    "actions_simulate": "Run simulation",
    "actions_memo": "Generate AI explanation",
    "actions_log": "Log action",
    "actions_unavailable": "Unavailable for this case",
    "memo_drivers": "Drivers cited",
    "memo_actions": "Recommended actions",
    "memo_action_id": "Action",
    "memo_action_role": "Owning role",
    "memo_action_text": "Detail",
    "memo_next_step": "Recommended next step",
    "memo_ai_provenance": "AI explanation provenance",
    "memo_provider": "Provider",
    "memo_model": "Model",
    "memo_prompt": "Prompt",
    "memo_shape_checks": "Grounding checks",
    "memo_citations": "Grounding citations",
    "memo_citations_note": (
        "The model connected these stored facts into prose; it did not calculate the "
        "prediction or create new source records."
    ),
    "memo_failed_checks": "Checks that did not pass",
    "memo_retry_at": "Expected to resolve after",
    "memo_reference": "Memo reference",
    "memo_export": "Export this memo",
    "memo_export_pdf": "Download PDF",
    "memo_export_docx": "Download DOCX",
    "memo_export_note": (
        "The export carries a stable integrity digest over the stored memo, so the same "
        "memo always exports to the same document."
    ),
    "memo_empty_title": "AI-generated explanation",
    "memo_empty_message": (
        "No AI explanation has been generated yet. Use the action above to draft a grounded "
        "explanation from the stored forecast and evidence."
    ),
    "empty_covenants_title": "No active covenants",
    "empty_covenants_message": "No active covenant is recorded for this borrower.",
    "empty_evidence_title": "No evidence recorded",
    "empty_evidence_message": (
        "No evidence has been recorded for this borrower yet. Evidence will appear here after "
        "the next scored run."
    ),
    "loading": "Loading case file",
    "error_title": "Unable to load this case file",
    "error_message": "Reload the case file. If the problem continues, contact an administrator.",
    "degraded_capability": "Forecast display",
    "degraded_message": "Locally stored borrower and covenant facts remain available.",
}


def create_borrower_router(
    session: Session,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
    memo_generator: MemoGenerator | None = None,
    memo_exporter: MemoExporter | None = None,
) -> APIRouter:
    """Build the case-file read route and the ``POST /memos`` memo action.

    ``memo_generator`` is optional on purpose: with no model provider
    configured the screen still renders and the memo action explains itself
    rather than failing.  ``memo_exporter`` is optional on the same terms: an
    application composed without a byte store simply does not offer the
    download.
    """
    if not is_database_session(session):
        raise TypeError("create_borrower_router requires a SQLAlchemy Session.")
    if memo_generator is not None and not callable(memo_generator):
        raise TypeError("memo_generator must be callable.")
    if memo_exporter is not None and not callable(memo_exporter):
        raise TypeError("memo_exporter must be callable.")
    router = APIRouter(tags=["borrower-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/borrowers/{reference}", response_class=HTMLResponse, name="borrower_case_file")
    def borrower_case_file(
        request: Request,
        reference: str,
        day: Annotated[int, Query(ge=0)] = 0,
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        scope = resolve_scope(principal, session)
        view = load_borrower_case_file(
            session,
            reference,
            scope=scope,
            can_run_simulation=principal.has(Permission.RUN_SIMULATION),
            can_generate_memo=principal.has(Permission.GENERATE_MEMO),
            can_log_action=principal.has(Permission.LOG_ACTION),
            can_upload_document=principal.has(Permission.UPLOAD_DOCUMENT),
            can_ingest_financial_statements=principal.has(Permission.INGEST_FINANCIAL_STATEMENTS),
        )
        if view is None:
            raise NotFound(f"Borrower {reference!r} was not found within the current scope.")
        borrower = BorrowerRepository(session).by_reference(reference, scope=scope)
        assert borrower is not None
        memos = MemoRepository(session).for_borrower(borrower.id, scope=scope)
        return _render(
            request,
            fallback_environment,
            principal=principal,
            view=view,
            selected_day=day,
            latest_memo=(build_persisted_memo_block(memos[0]) if memos else None),
        )

    @router.get(
        "/memos/{memo_id}/export",
        response_class=Response,
        name="borrower_memo_export",
    )
    def borrower_memo_export(
        memo_id: UUID,
        format: Annotated[str, Query(pattern="^(pdf|docx)$")] = "pdf",
        principal: Principal = _MEMO_DEP,
    ) -> Response:
        """Download one stored memo as PDF or DOCX (`README` "Exports").

        The renderer is the only thing that decides whether a format can be
        produced here.  A host without the native PDF libraries raises from
        `MemoRenderer.render_pdf`; that is surfaced as an explicit
        capability failure rather than a crash, so DOCX stays usable and the
        reader is told which one is missing.
        """
        if memo_exporter is None:
            raise ExternalServiceError(
                "Memo export is not configured on this deployment.",
            )
        scope = resolve_scope(principal, session)
        try:
            download = memo_exporter(
                memo_id,
                principal=principal,
                scope=scope,
                export_format=format,
            )
        except (ImportError, OSError, RuntimeError) as error:
            # WeasyPrint's native stack is the usual absentee. Name the
            # format that is unavailable and leave the other one working.
            raise ExternalServiceError(
                f"{format.upper()} memo export is unavailable on this host: {error}"
            ) from error
        return Response(
            content=download.content,
            media_type=download.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{download.filename}"',
                "X-Memo-Integrity-SHA256": download.integrity_hash,
            },
        )

    @router.post("/memos", response_class=HTMLResponse, name="borrower_memo")
    async def borrower_memo(
        request: Request,
        principal: Principal = _MEMO_DEP,
    ) -> HTMLResponse:
        """Draft one grounded memo and return the block for this borrower.

        Every designed failure returns 200 with an explanatory block: a shape
        refusal, a provider outage and a call ceiling all leave the rest of
        the case file intact (`spec §R-17.c`). Only an unknown or
        out-of-scope borrower is a 404, matching the read route above.
        """
        values = await _form_values(request)
        reference = _required_text(values.get("borrower_ref"), "borrower_ref")
        simulation_ids = _simulation_ids(values.get("simulation_ids"))
        scope = resolve_scope(principal, session)
        borrower = BorrowerRepository(session).by_reference(reference, scope=scope)
        if borrower is None:
            raise NotFound(f"Borrower {reference!r} was not found within the current scope.")
        return _render_memo(
            request,
            fallback_environment,
            principal=principal,
            memo=_memo_block(
                borrower,
                scope,
                simulation_ids,
                generator=memo_generator,
                actor_id=None if principal.is_api_key else principal.id,
            ),
        )

    def _memo_block(
        borrower: Borrower,
        scope: Scope,
        simulation_ids: tuple[UUID, ...],
        *,
        generator: MemoGenerator | None,
        actor_id: UUID | None,
    ) -> MemoBlockView:
        if generator is None:
            return memo_unavailable(NOT_CONFIGURED_MESSAGE)
        facts = collect_memo_records(
            session,
            borrower,
            scope=scope,
            simulation_ids=simulation_ids,
        )
        if not facts.has_forecast:
            # Without a forecast there is no covenant position to ground a
            # memo in, so no model call is made at all.
            return memo_unavailable(NO_FORECAST_MESSAGE)
        return build_memo_block(
            generator(
                borrower_id=borrower.id,
                records=facts.records,
                run_id=facts.run_id,
                case_id=facts.case_id,
                actor_id=actor_id,
            )
        )

    return router


async def _form_values(request: Request) -> Mapping[str, list[str]]:
    """Read a bounded form or JSON body into repeatable string values.

    ``simulation_ids`` is a repeated field (`C-08`), so values stay as lists
    rather than collapsing to the last one.
    """

    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted memo request is too large.", field="form")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json":
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError(
                "The submitted memo request is not valid JSON.", field="form"
            ) from error
        if not isinstance(payload, Mapping):
            raise ValidationError("The submitted memo request must be an object.", field="form")
        return {
            str(key): [str(item) for item in _as_sequence(value)]
            for key, value in payload.items()
            if key != "csrf_token"
        }
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError(
            "The submitted memo request is not valid UTF-8.", field="form"
        ) from error
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {key: values for key, values in parsed.items() if key != "csrf_token"}


def _as_sequence(value: object) -> Sequence[object]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        return (value,)
    return value


def _required_text(values: list[str] | None, field: str) -> str:
    if not values or not isinstance(values[0], str) or not values[0].strip():
        raise ValidationError(f"{field} is required.", field=field)
    text = values[0].strip()
    if len(text) > 200:
        raise ValidationError(f"{field} is too long.", field=field)
    return text


def _simulation_ids(values: list[str] | None) -> tuple[UUID, ...]:
    """Parse the optional repeated ``simulation_ids`` field.

    An unparseable identifier is refused rather than skipped: silently
    dropping one would produce a memo that cites fewer options than the
    caller asked for, with nothing to say so.
    """

    if not values:
        return ()
    if len(values) > _MAX_SIMULATION_IDS:
        raise ValidationError(
            f"At most {_MAX_SIMULATION_IDS} simulations can be cited in one memo.",
            field="simulation_ids",
        )
    identifiers: list[UUID] = []
    for value in values:
        text = value.strip()
        if not text:
            continue
        try:
            identifiers.append(UUID(text))
        except ValueError as error:
            raise ValidationError(
                "simulation_ids must contain identifiers.", field="simulation_ids"
            ) from error
    return tuple(dict.fromkeys(identifiers))


def _render_memo(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    memo: MemoBlockView,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/borrower/_memo.html")
    return HTMLResponse(
        template.render(
            request=request,
            principal=principal,
            labels=_LABELS,
            memo=memo,
            csrf_token=getattr(request.state, "csrf_token", ""),
        )
    )


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/borrower/index.html")
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
    return HTMLResponse(template.render(**values))


__all__ = ["MemoGenerator", "create_borrower_router"]
