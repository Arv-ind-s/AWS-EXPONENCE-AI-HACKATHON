"""Protected browser route for the global search resource (`T-137`)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import ValidationError
from covenant_radar.db.scoping import Scope
from covenant_radar.security.crypto import HMACFingerprinter
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.search import SearchAuditWriter, SearchPage, SearchService
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_READ = requires(Permission.VIEW_QUEUE)
_READ_DEP = Depends(_READ)
_MAX_PAGE_SIZE = 100

_LABELS = {
    "title": "Search",
    "heading": "Search across Covenant Radar",
    "subheading": (
        "Find scoped borrowers, facilities, covenants, documents, memos, cases and audit events."
    ),
    "query_label": "Search terms",
    "query_placeholder": "Search by name, reference or text",
    "type_filter": "Limit result types",
    "all_types": "All permitted types",
    "search": "Search",
    "recent_heading": "Recent items",
    "results_heading": "Search results",
    "results_count": "{count} matching results",
    "recent_count": "{count} recent items",
    "no_results": "No matching items are visible in your scope.",
    "no_recent": "No recent items are visible in your scope.",
    "next": "Next page",
    "match_source": "Matched in {source}",
    "entity_borrower": "Borrower",
    "entity_facility": "Facility",
    "entity_covenant": "Covenant",
    "entity_document": "Document",
    "entity_memo": "Memo",
    "entity_case": "Case",
    "entity_audit_event": "Audit event",
}


def create_search_router(
    session: Session,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
    audit_writer: object | None = None,
    scope_resolver: Callable[[Principal], Scope] | None = None,
    fingerprinter: HMACFingerprinter | None = None,
) -> APIRouter:
    """Build the authenticated global-search route over one database session."""
    service = SearchService(
        session,
        audit=cast(SearchAuditWriter | None, audit_writer),
        scope_resolver=scope_resolver,
        fingerprinter=fingerprinter,
    )
    router = APIRouter(tags=["search-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/search", response_class=HTMLResponse, name="search")
    def search(request: Request, principal: Principal = _READ_DEP) -> HTMLResponse:
        query = _query_parameter(request)
        entity_types = _type_parameters(request)
        page_size = _integer_parameter(request, "page_size", default=50)
        offset = _integer_parameter(request, "offset", default=0)
        page = service.search(
            principal,
            query,
            entity_types=entity_types,
            page_size=page_size,
            offset=offset,
            request_id=getattr(request.state, "request_id", None),
        )
        type_options = tuple(
            {
                "value": entity_type,
                "label": _LABELS[f"entity_{entity_type}"],
                "selected": entity_type in page.entity_types,
            }
            for entity_type in service.available_entity_types(principal)
        )
        summary = (
            _LABELS["recent_count"].format(count=page.total_count)
            if page.is_recent
            else _LABELS["results_count"].format(count=page.total_count)
        )
        return _render(
            request,
            fallback_environment,
            principal=principal,
            page=page,
            type_options=type_options,
            summary=summary,
            next_href=_next_href(request, page),
        )

    return router


def _query_parameter(request: Request) -> str:
    values = request.query_params.getlist("q") + request.query_params.getlist("query")
    unique = tuple(dict.fromkeys(values))
    if len(unique) > 1:
        raise ValidationError("Search accepts one query value.", field="q")
    return unique[0] if unique else ""


def _type_parameters(request: Request) -> tuple[str, ...] | None:
    values = request.query_params.getlist("type") + request.query_params.getlist("entity_type")
    selected = tuple(value for value in values if value.strip())
    return selected or None


def _integer_parameter(request: Request, name: str, *, default: int) -> int:
    values = request.query_params.getlist(name)
    if not values:
        return default
    if len(values) != 1:
        raise ValidationError(f"Search accepts one {name} value.", field=name)
    try:
        return int(values[0])
    except ValueError as error:
        raise ValidationError(f"Search {name} must be an integer.", field=name) from error


def _next_href(request: Request, page: SearchPage) -> str | None:
    if not page.has_more:
        return None
    values: list[tuple[str, str]] = []
    for key, value in request.query_params.multi_items():
        if key in {"offset", "page_size"}:
            continue
        values.append((key, value))
    values.append(("page_size", str(page.page_size)))
    values.append(("offset", str(page.offset + page.page_size)))
    return f"{request.url.path}?{urlencode(values)}"


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    is_fragment = (
        request.headers.get("HX-Request", "").lower() == "true"
        and request.headers.get("HX-Target") == "search-results"
    )
    template_name = (
        "_components/search_results.html" if is_fragment else "screens/search/index.html"
    )
    template = environment.get_template(template_name)
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    if locale not in {"en", "hi"}:
        locale = "en"
    values = {
        "request": request,
        "principal": principal,
        "locale": locale,
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        **context,
    }
    response = HTMLResponse(template.render(**values))
    response.headers["Vary"] = "HX-Request, HX-Target"
    return response


__all__ = ["create_search_router"]
