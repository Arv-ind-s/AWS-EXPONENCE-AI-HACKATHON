"""Browser simulator and ``POST /simulations`` route (T-079, C-11).

The route is intentionally thin.  It resolves a forecast and its catalogue
through the caller's portfolio scope, delegates every calculation to
``SimulationService``, and gives the resulting immutable facts to the view
model.  It does not accept effect models or effect parameters from the
browser: those are bank-owned catalogue configuration and cannot be replaced
by a request payload.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.errors import Conflict, DomainError, NotFound, ValidationError
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.forecast import Weights
from covenant_radar.domain.interventions.applicability import (
    InterventionNotApplicable,
    normalize_covenant_class,
)
from covenant_radar.domain.interventions.catalogue import CatalogueEntry
from covenant_radar.domain.interventions.simulate import SimulationComparison
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.catalogue import CatalogueService
from covenant_radar.services.simulation import SimulationAuditWriter, SimulationService
from covenant_radar.web.errors import status_for_error
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.simulation import (
    ComparisonColumnView,
    SimulationContext,
    SimulationScreenView,
    build_simulation_projection,
    build_simulation_view,
    load_simulation_context,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 128 * 1024
_MAX_COMPARISON_OPTIONS = 4
_RUN = requires(Permission.RUN_SIMULATION)
_RUN_DEP = Depends(_RUN)

_LABELS = {
    "title": "Intervention simulator",
    "heading": "Intervention simulator",
    "forecast": "Forecast",
    "borrower": "Borrower",
    "covenant": "Covenant",
    "covenant_class": "Covenant class",
    "threshold": "Threshold",
    "direction": "Direction",
    "horizon": "Horizon",
    "days": "days",
    "as_of": "As of",
    "unavailable": "Unavailable",
    "probability": "Probability",
    "confidence": "Confidence",
    "crossing": "Crossing date",
    "interventions_title": "Applicable interventions",
    "select_hint": "Select up to four options. Every assumption is shown before you run them.",
    "effect": "Effect model",
    "role": "Role",
    "parameters": "Parameters",
    "assumptions": "Assumptions",
    "requires_approval": "Requires approval",
    "yes": "Yes",
    "no": "No",
    "compare": "Compare with baseline",
    "comparison_title": "Comparison against doing nothing",
    "baseline": "Do nothing (baseline)",
    "selected_horizon": "Stored horizon",
    "delta_days": "Crossing delta",
    "delta_probability": "Probability delta",
    "status": "Status",
    "status_applied": "Applied",
    "status_no_effect": "No observable effect",
    "no_effect_reason": (
        "Zero observable effect for this forecast; the option remains valid and applicable."
    ),
    "assumption_list": "Assumptions for this option",
    "memo": "Carry selected simulations into memo generation",
    "empty_title": "Select an intervention",
    "empty_message": "Select an intervention to compare against doing nothing.",
    "no_options": "No applicable interventions are configured for this covenant.",
    "error_title": "Unable to run the simulation",
    "error_message": (
        "Correct the request and try again. If the problem continues, contact an administrator."
    ),
    "loading": "Loading intervention simulator",
    "not_available": "The simulator is unavailable for this forecast.",
}


def create_simulator_router(
    source: Session | SimulationService,
    *,
    simulation_service: SimulationService | None = None,
    catalogue_service: CatalogueService | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
    scope_resolver: Callable[[Principal], Scope] | None = None,
    audit_writer: SimulationAuditWriter | None = None,
) -> APIRouter:
    """Build the protected simulator screen and C-11 write route.

    ``source`` may be the application's session or an already-configured
    ``SimulationService``.  Supplying a service is useful when the caller has
    a configured clock or audit adapter; all injected services must still use
    the same session so one request cannot split its transaction boundary.
    """

    resolved_simulation_service: SimulationService | None
    if isinstance(source, SimulationService):
        session = source.session
        if session is None:
            raise TypeError("create_simulator_router requires a session-backed SimulationService.")
        resolved_simulation_service = simulation_service or source
    elif is_database_session(source):
        session = source
        resolved_simulation_service = simulation_service
    else:
        raise TypeError(
            "create_simulator_router requires a SQLAlchemy Session or SimulationService."
        )
    if resolved_simulation_service is not None:
        if not isinstance(resolved_simulation_service, SimulationService):
            raise TypeError("simulation_service must be a SimulationService.")
        if resolved_simulation_service.session is not session:
            raise ValueError("simulation_service must use the route's SQLAlchemy session.")
    resolved_catalogue = catalogue_service or CatalogueService(session)
    if not isinstance(resolved_catalogue, CatalogueService):
        raise TypeError("catalogue_service must be a CatalogueService.")
    if resolved_catalogue.session is not session:
        raise ValueError("catalogue_service must use the route's SQLAlchemy session.")
    if scope_resolver is not None and not callable(scope_resolver):
        raise TypeError("scope_resolver must be callable.")
    if audit_writer is not None and not callable(getattr(audit_writer, "record", None)):
        raise TypeError("audit_writer must provide a callable record method.")

    router = APIRouter(tags=["simulator-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/simulator", response_class=HTMLResponse, name="simulator")
    async def simulator(
        request: Request,
        forecast_id: str | None = None,
        principal: Principal = _RUN_DEP,
    ) -> HTMLResponse:
        selected_codes = _query_codes(request)
        if forecast_id is None or not forecast_id.strip():
            return _render(
                request,
                fallback_environment,
                principal=principal,
                view=build_simulation_view(None, selected_codes=selected_codes),
            )
        return _get_simulator(
            request,
            forecast_id,
            selected_codes=selected_codes,
            principal=principal,
            session=session,
            catalogue=resolved_catalogue,
            scope_resolver=scope_resolver,
            fallback_environment=fallback_environment,
        )

    @router.get(
        "/simulator/{forecast_id}",
        response_class=HTMLResponse,
        name="simulator_for_forecast",
    )
    async def simulator_for_forecast(
        request: Request,
        forecast_id: str,
        principal: Principal = _RUN_DEP,
    ) -> HTMLResponse:
        return _get_simulator(
            request,
            forecast_id,
            selected_codes=_query_codes(request),
            principal=principal,
            session=session,
            catalogue=resolved_catalogue,
            scope_resolver=scope_resolver,
            fallback_environment=fallback_environment,
        )

    @router.post("/simulations", name="create_simulation")
    async def create_simulation(
        request: Request,
        principal: Principal = _RUN_DEP,
    ) -> Response:
        context: SimulationContext | None = None
        selected_codes: tuple[str, ...] = ()
        try:
            values = await _submission_values(request)
            forecast_id = _required_uuid(values.get("forecast_id"), "forecast_id")
            selected_codes = _submission_codes(values)
            if len(selected_codes) > _MAX_COMPARISON_OPTIONS:
                raise ValidationError(
                    f"At most {_MAX_COMPARISON_OPTIONS} interventions may be compared; "
                    f"received {len(selected_codes)}.",
                    field="intervention_code",
                )
            scope = _scope_for(principal, session, scope_resolver)
            context = _load_context(session, forecast_id, scope, resolved_catalogue)
            if not selected_codes:
                raise ValidationError(
                    "Select at least one intervention to run a comparison.",
                    field="intervention_code",
                )
            entries = _resolve_entries(
                resolved_catalogue,
                selected_codes,
                context.covenant.covenant_class,
            )
            parameters = _parameters(values.get("parameters"))
            comparisons = _compare(
                context,
                entries,
                parameters,
                resolved_simulation_service,
            )
            simulation_ids = _persist(
                request,
                principal,
                context,
                entries,
                comparisons,
                scope=scope,
                session=session,
                simulation_service=resolved_simulation_service,
                audit_writer=audit_writer,
            )
            view = build_simulation_view(
                context,
                selected_codes=selected_codes,
                comparisons=comparisons,
                parameters=parameters,
                simulation_ids=simulation_ids,
            )
            if _wants_json(request):
                return JSONResponse(_json_payload(view), status_code=200)
            return _render(request, fallback_environment, principal=principal, view=view)
        except NotFound:
            raise
        except DomainError as error:
            view = build_simulation_view(
                context,
                selected_codes=selected_codes,
                error_message=error.message,
            )
            if _wants_json(request):
                return JSONResponse(
                    {"error": error.code, "message": error.message},
                    status_code=status_for_error(error),
                )
            return _render(
                request,
                fallback_environment,
                principal=principal,
                view=view,
                status_code=status_for_error(error),
            )
        except (TypeError, ValueError) as error:
            # Adapter-shaped request data and malformed persisted formula
            # inputs are validation failures at this boundary.  Keep their
            # details user-actionable without exposing a traceback.
            message = str(error) or _LABELS["error_message"]
            view = build_simulation_view(
                context,
                selected_codes=selected_codes,
                error_message=message,
            )
            if _wants_json(request):
                return JSONResponse(
                    {"error": "validation_error", "message": message}, status_code=422
                )
            return _render(
                request,
                fallback_environment,
                principal=principal,
                view=view,
                status_code=422,
            )

    return router


def _get_simulator(
    request: Request,
    forecast_id: str,
    *,
    selected_codes: Sequence[str],
    principal: Principal,
    session: Session,
    catalogue: CatalogueService,
    scope_resolver: Callable[[Principal], Scope] | None,
    fallback_environment: Environment,
) -> HTMLResponse:
    try:
        parsed_id = _required_uuid(forecast_id, "forecast_id")
        scope = _scope_for(principal, session, scope_resolver)
        context = _load_context(session, parsed_id, scope, catalogue)
        selected = _normalise_codes(selected_codes)
        view = build_simulation_view(
            context,
            selected_codes=selected,
            parameters=_default_parameters(context.forecast),
        )
    except NotFound:
        raise
    except (DomainError, TypeError, ValueError) as error:
        view = build_simulation_view(
            None,
            error_message=error.message if isinstance(error, DomainError) else str(error),
        )
    return _render(request, fallback_environment, principal=principal, view=view)


def _load_context(
    session: Session,
    forecast_id: UUID,
    scope: Scope,
    catalogue: CatalogueService,
) -> SimulationContext:
    initial = load_simulation_context(session, forecast_id, scope=scope)
    entries = catalogue.applicable(initial.covenant.covenant_class)
    return SimulationContext(
        forecast=initial.forecast,
        forecasts=initial.forecasts,
        paths=initial.paths,
        run=initial.run,
        covenant_version=initial.covenant_version,
        covenant=initial.covenant,
        facility=initial.facility,
        borrower=initial.borrower,
        entries=entries,
    )


def _resolve_entries(
    catalogue: CatalogueService,
    codes: Sequence[str],
    covenant_class: str,
) -> tuple[CatalogueEntry, ...]:
    resolved: list[CatalogueEntry] = []
    for code in codes:
        entry = catalogue.find(code, include_retired=True)
        if entry is None:
            raise ValidationError(
                f"Intervention {code!r} was not found in the catalogue.",
                field="intervention_code",
            )
        if entry.is_retired:
            raise ValidationError(
                f"Intervention {code!r} is retired and cannot be used for a new simulation.",
                field="intervention_code",
            )
        try:
            entry.for_simulation(covenant_class)
        except InterventionNotApplicable as error:
            # Keep the domain's precise applicable-class explanation intact;
            # this is the important distinction from a valid zero effect.
            raise ValidationError(
                f"Intervention {code!r} was refused: {error.message}",
                field="intervention_code",
            ) from error
        resolved.append(entry)
    return tuple(resolved)


def _compare(
    context: SimulationContext,
    entries: Sequence[CatalogueEntry],
    supplied_parameters: Mapping[str, object],
    configured_service: SimulationService | None,
) -> dict[UUID, SimulationComparison]:
    service = configured_service
    if service is None:
        # A service is created by the route only for the request's calculation
        # path.  Persistence gets its own explicitly audited instance below.
        service = SimulationService()
    facts = tuple(entry.for_simulation(context.covenant.covenant_class) for entry in entries)
    comparisons: dict[UUID, SimulationComparison] = {}
    for forecast in context.forecasts:
        parameters = _effective_parameters(forecast, context, supplied_parameters)
        try:
            projection = build_simulation_projection(context, forecast)
            comparison = service.compare(projection, facts, parameters)
        except (TypeError, ValueError) as error:
            raise ValidationError(str(error), field="parameters") from error
        comparisons[forecast.id] = comparison
    return comparisons


def _persist(
    request: Request,
    principal: Principal,
    context: SimulationContext,
    entries: Sequence[CatalogueEntry],
    comparisons: Mapping[UUID, SimulationComparison],
    *,
    scope: Scope,
    session: Session,
    simulation_service: SimulationService | None,
    audit_writer: SimulationAuditWriter | None,
) -> dict[tuple[UUID, str], UUID]:
    writer = audit_writer
    if writer is None:
        configured_writer = getattr(request.app.state, "audit_writer", None)
        if configured_writer is not None:
            if not callable(getattr(configured_writer, "record", None)):
                raise ValidationError("The configured audit writer is invalid.", field="audit")
            writer = cast(SimulationAuditWriter, configured_writer)
    if writer is None:
        writer = cast(SimulationAuditWriter, AuditRecorder(AuditRepository(session)))
    service = simulation_service
    if service is None or service.audit is None:
        service = SimulationService(session, audit=writer)
    if service.session is not session:
        raise ValueError("Simulation persistence service must use the route's session.")
    entry_by_code = {entry.code: entry for entry in entries}
    created_by_id = None if principal.is_api_key else principal.id
    request_id = getattr(request.state, "request_id", "web-simulation")
    if not isinstance(request_id, str) or not request_id.strip():
        request_id = "web-simulation"
    identifiers: dict[tuple[UUID, str], UUID] = {}
    try:
        with session.begin_nested():
            for forecast in context.forecasts:
                comparison = comparisons[forecast.id]
                for result in comparison.options:
                    entry = entry_by_code[result.intervention_code]
                    intervention_id = entry.database_id
                    if intervention_id is None:
                        raise ValidationError(
                            f"Intervention {entry.code!r} has no persisted catalogue id.",
                            field="intervention_code",
                        )
                    row = service.persist(
                        result,
                        forecast_id=forecast.id,
                        intervention_id=intervention_id,
                        scope=scope,
                        created_by_id=created_by_id,
                        actor_id=principal.id,
                        request_id=request_id,
                    )
                    identifiers[(forecast.id, result.intervention_code)] = row.id
    except Conflict:
        raise
    except (TypeError, ValueError) as error:
        raise ValidationError(str(error), field="simulation") from error
    return identifiers


def _scope_for(
    principal: Principal,
    session: Session,
    resolver: Callable[[Principal], Scope] | None,
) -> Scope:
    if resolver is None:
        scope = resolve_scope(principal, session)
    else:
        scope = resolver(principal)
    if not isinstance(scope, Scope) or scope.principal_id != principal.id:
        raise ValidationError("The resolved portfolio scope is invalid.", field="scope")
    return scope


async def _submission_values(request: Request) -> dict[str, object]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise ValidationError(
                "The submitted simulation has an invalid size.", field="form"
            ) from error
        if declared_length < 0:
            raise ValidationError("The submitted simulation has an invalid size.", field="form")
        if declared_length > _MAX_FORM_BYTES:
            raise ValidationError("The submitted simulation is too large.", field="form")
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted simulation is too large.", field="form")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json":
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError(
                "The simulation request is not valid JSON.", field="form"
            ) from error
        if not isinstance(payload, Mapping):
            raise ValidationError("The simulation request must be a JSON object.", field="form")
        return dict(payload)
    if content_type in {"", "application/x-www-form-urlencoded"}:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError(
                "The submitted simulation is not valid UTF-8.", field="form"
            ) from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values if len(values) > 1 else values[0] for key, values in parsed.items()}
    try:
        form = await request.form()
    except (TypeError, ValueError) as error:
        raise ValidationError("The submitted simulation form is invalid.", field="form") from error
    return {
        key: [value for value in values if isinstance(value, str)]
        for key, values in _group_form_items(form).items()
    }


def _group_form_items(form: object) -> dict[str, list[object]]:
    items = getattr(form, "multi_items", None)
    if not callable(items):
        raise ValidationError("The submitted simulation form is invalid.", field="form")
    grouped: dict[str, list[object]] = {}
    for key, value in items():
        if key == "csrf_token":
            continue
        grouped.setdefault(str(key), []).append(value)
    return grouped


def _parameters(raw: object) -> dict[str, object]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
        if len(raw) != 1:
            raise ValidationError("parameters may be supplied only once.", field="parameters")
        raw = raw[0]
    if not isinstance(raw, str):
        raise ValidationError("parameters must be a JSON object.", field="parameters")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValidationError("parameters must be valid JSON.", field="parameters") from error
    if not isinstance(decoded, Mapping):
        raise ValidationError("parameters must be a JSON object.", field="parameters")
    return dict(decoded)


def _effective_parameters(
    forecast: object,
    context: SimulationContext,
    supplied: Mapping[str, object],
) -> dict[str, object]:
    parameters = dict(supplied)
    expected_class = normalize_covenant_class(context.covenant.covenant_class)
    supplied_class = parameters.get("covenant_class")
    if (
        supplied_class is not None
        and normalize_covenant_class(str(supplied_class)) != expected_class
    ):
        raise ValidationError(
            "The simulation covenant_class must match the selected forecast.",
            field="parameters.covenant_class",
        )
    parameters["covenant_class"] = expected_class
    horizon = getattr(forecast, "horizon_days", None)
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 0:
        raise ValidationError("The selected forecast has an invalid horizon.", field="forecast_id")
    supplied_horizon = parameters.get("horizon_days")
    if supplied_horizon is not None and supplied_horizon != horizon:
        raise ValidationError(
            "parameters.horizon_days must match the selected forecast horizon.",
            field="parameters.horizon_days",
        )
    parameters["horizon_days"] = horizon
    stored_weights = _stored_weights(forecast)
    if stored_weights is not None:
        _validate_stored_weights(parameters, stored_weights)
        for alias in ("probability_weights", "probability"):
            parameters.pop(alias, None)
        parameters["weights"] = stored_weights
    if "as_of_date" not in parameters:
        forecast_date = getattr(forecast, "data_as_of", None)
        if forecast_date is None:
            forecast_date = context.run.as_of_date
        if forecast_date is not None:
            parameters["as_of_date"] = forecast_date
    return parameters


def _validate_stored_weights(
    parameters: Mapping[str, object],
    stored: Mapping[str, object],
) -> None:
    """Prevent a browser payload from changing the forecast's risk policy."""

    try:
        stored_weights = Weights.from_mapping(stored)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            "The selected forecast has invalid persisted probability weights.",
            field="forecast_inputs.probability.weights",
        ) from error
    for name in ("weights", "probability_weights", "probability"):
        if name not in parameters:
            continue
        raw = parameters[name]
        if not isinstance(raw, Mapping):
            raise ValidationError(
                "Simulation probability weights must be a JSON object.",
                field=f"parameters.{name}",
            )
        try:
            supplied = Weights.from_mapping(raw)
        except (TypeError, ValueError) as error:
            raise ValidationError(
                "Simulation probability weights are invalid.",
                field=f"parameters.{name}",
            ) from error
        if supplied.as_mapping() != stored_weights.as_mapping():
            raise ValidationError(
                "Simulation probability weights must match the selected forecast.",
                field=f"parameters.{name}",
            )


def _stored_weights(forecast: object) -> Mapping[str, object] | None:
    formula = getattr(forecast, "formula_inputs", None)
    if not isinstance(formula, Mapping):
        return None
    probability = formula.get("probability")
    if not isinstance(probability, Mapping):
        return None
    weights = probability.get("weights")
    return weights if isinstance(weights, Mapping) else None


def _default_parameters(forecast: object) -> Mapping[str, object]:
    weights = _stored_weights(forecast)
    horizon = getattr(forecast, "horizon_days", None)
    result: dict[str, object] = {}
    if weights is not None:
        result["weights"] = weights
    if isinstance(horizon, int) and not isinstance(horizon, bool):
        result["horizon_days"] = horizon
    return result


def _query_codes(request: Request) -> tuple[str, ...]:
    values = request.query_params.getlist("intervention_code")
    if not values:
        values = request.query_params.getlist("intervention_code[]")
    return _normalise_codes(values)


def _submission_codes(values: Mapping[str, object]) -> tuple[str, ...]:
    raw = values.get(
        "intervention_code",
        values.get(
            "intervention_code[]",
            values.get("intervention_codes", values.get("interventions")),
        ),
    )
    if raw is None:
        return ()
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise ValidationError(
                    "intervention_code must be valid text or JSON.",
                    field="intervention_code",
                ) from error
            raw = decoded
        else:
            raw = [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
        return _normalise_codes(tuple(cast(str, item) for item in raw))
    raise ValidationError(
        "intervention_code must be text or an array of text.", field="intervention_code"
    )


def _normalise_codes(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                "intervention_code must be non-blank text.", field="intervention_code"
            )
        normalized = value.strip()
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _required_uuid(value: object, field: str) -> UUID:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        if len(value) != 1:
            raise ValidationError(f"{field} must be supplied once.", field=field)
        value = value[0]
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required.", field=field)
    try:
        return UUID(value.strip())
    except ValueError as error:
        raise ValidationError(f"{field} must be a UUID.", field=field) from error


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    view: object,
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    is_fragment = (
        request.headers.get("HX-Request", "").lower() == "true"
        and request.headers.get("HX-Target") == "simulator-comparison-region"
    )
    template_name = (
        "_components/simulator_results.html" if is_fragment else "screens/simulator/index.html"
    )
    template = environment.get_template(template_name)
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    theme = theme_for_request(request)
    response = HTMLResponse(
        template.render(
            request=request,
            principal=principal,
            locale=locale,
            theme=theme,
            text_direction="ltr",
            labels=_LABELS,
            csrf_token=getattr(request.state, "csrf_token", ""),
            view=view,
        ),
        status_code=status_code,
    )
    response.headers["Vary"] = "HX-Request, HX-Target"
    return response


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept.lower() and "text/html" not in accept.lower()


def _json_payload(view: SimulationScreenView) -> dict[str, object]:
    # Keep the browser response and the machine response on one read model.
    screen = view
    comparison = screen.comparison
    return {
        "forecast_id": str(screen.forecast.id) if screen.forecast is not None else None,
        "baseline": _column_payload(comparison.baseline),
        "options": [_column_payload(option) for option in comparison.options],
        "memo_href": screen.memo_href,
    }


def _column_payload(column: ComparisonColumnView) -> dict[str, object]:
    return {
        "code": column.code,
        "label": column.label,
        "status": column.status.value,
        "assumptions": list(column.assumptions),
        "horizons": [
            {
                "horizon_days": item.horizon_days,
                "crossing": item.crossing_display,
                "probability": item.probability_display,
                "delta_days": item.delta_days_display,
                "delta_probability": item.delta_probability_display,
            }
            for item in column.horizons
        ],
        "simulation_ids": [str(value) for value in column.simulation_ids],
    }


__all__ = ["create_simulator_router"]
