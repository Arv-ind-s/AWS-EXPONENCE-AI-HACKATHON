"""Browser screens and form actions for T-023 master data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qs, quote, urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pydantic import ValidationError as PydanticValidationError

from covenant_radar.api.deps import requires
from covenant_radar.api.v1.schemas.master_data import (
    BorrowerCreate,
    BorrowerUpdate,
    FacilityCreate,
    FacilityUpdate,
    PortfolioCreate,
    PortfolioUpdate,
)
from covenant_radar.core.errors import DomainError, ValidationError
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.facility import (
    ALL_STATUSES,
    CURRENT_STATUS,
    FACILITY_STATUSES,
    SUPERSEDED_STATUS,
)
from covenant_radar.i18n.formatting import IST, format_indian_number
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.master_data import MasterDataService
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.facility import (
    facility_book_view,
    facility_detail_fields,
    facility_filter_options,
    facility_revision_rows,
    facility_table_rows,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 128 * 1024
#: Rows per page on the master-data list screens.
_PAGE_SIZE = 50
#: An upper bound on `?page=`, so a crafted value cannot ask the database for
#: an arbitrarily large offset.
_MAX_PAGE = 100_000
#: How many facilities the book summary will read into memory. The summary is
#: computed in Python to keep money exact (`db/types.py` stores it as text on
#: SQLite), so it needs a ceiling: past this, the screen says plainly that it
#: is summarising a sample rather than quietly reporting a partial book as if
#: it were the whole one.
_MAX_BOOK_ROWS = 50_000
_RowT = TypeVar("_RowT")
_READ = requires(Permission.VIEW_BORROWER)
_WRITE = requires(Permission.CORRECT_SOURCE_DATA)
_READ_DEP = Depends(_READ)
_WRITE_DEP = Depends(_WRITE)

_LABEL_KEYS = {
    "app_name": "app.name",
    "borrowers_title": "master.borrowers.title",
    "borrowers_heading": "master.borrowers.heading",
    "borrowers_new": "master.borrowers.new",
    "borrowers_empty": "master.borrowers.empty",
    "borrowers_reference": "master.borrowers.reference",
    "borrowers_legal_name": "master.borrowers.legal_name",
    "borrowers_portfolio": "master.borrowers.portfolio",
    "borrowers_status": "master.borrowers.status",
    "borrowers_filter_search": "master.borrowers.filter_search",
    "borrowers_filter_search_placeholder": "master.borrowers.filter_search_placeholder",
    "borrowers_filter_all_portfolios": "master.borrowers.filter_all_portfolios",
    "borrowers_filter_all_statuses": "master.borrowers.filter_all_statuses",
    "borrowers_filter_active": "master.borrowers.filter_active",
    "borrowers_filter_inactive": "master.borrowers.filter_inactive",
    "borrowers_filter_apply": "master.borrowers.filter_apply",
    "borrowers_filter_clear": "master.borrowers.filter_clear",
    "borrowers_no_matches": "master.borrowers.no_matches",
    "borrowers_actions": "master.common.actions",
    "borrowers_open": "master.common.open",
    "borrowers_active": "master.common.active",
    "borrowers_inactive": "master.common.inactive",
    "borrower_title": "master.borrower.title",
    "borrower_heading": "master.borrower.heading",
    "borrower_save": "master.common.save",
    "borrower_deactivate": "master.common.deactivate",
    "borrower_version": "master.common.version",
    "borrower_cin_present": "master.borrower.cin_present",
    "facilities_title": "master.facilities.title",
    "facilities_heading": "master.facilities.heading",
    "facilities_new": "master.facilities.new",
    "facilities_empty": "master.facilities.empty",
    "facilities_reference": "master.facilities.reference",
    "facilities_borrower": "master.facilities.borrower",
    "facilities_type": "master.facilities.type",
    "facilities_limit": "master.facilities.limit",
    "facilities_currency": "master.facilities.currency",
    "facilities_sanction_date": "master.facilities.sanction_date",
    "facilities_maturity": "master.facilities.maturity",
    "facilities_effective": "master.facilities.effective",
    "facilities_effective_to": "master.facilities.effective_to",
    "facilities_outstanding": "master.facilities.outstanding",
    "facilities_utilisation": "master.facilities.utilisation",
    "facilities_status": "master.facilities.status",
    "facilities_amount_unit": "master.facilities.amount_unit",
    "facilities_filter_search": "master.facilities.filter_search",
    "facilities_filter_search_placeholder": "master.facilities.filter_search_placeholder",
    "facilities_filter_all_types": "master.facilities.filter_all_types",
    "facilities_filter_all_currencies": "master.facilities.filter_all_currencies",
    "facilities_filter_status_current": "master.facilities.filter_status_current",
    "facilities_filter_status_superseded": "master.facilities.filter_status_superseded",
    "facilities_filter_status_all": "master.facilities.filter_status_all",
    "facilities_filter_apply": "master.facilities.filter_apply",
    "facilities_no_matches": "master.facilities.no_matches",
    "facilities_insights_link": "master.facilities.insights_link",
    "insights_title": "master.insights.title",
    "insights_heading": "master.insights.heading",
    "insights_intro": "master.insights.intro",
    "insights_empty": "master.insights.empty",
    "insights_headline": "master.insights.headline",
    "insights_bucket": "master.insights.bucket",
    "insights_count": "master.insights.count",
    "insights_share": "master.insights.share",
    "insights_chart": "master.insights.chart",
    "insights_back": "master.insights.back",
    "facility_title": "master.facility.title",
    "facility_heading": "master.facility.heading",
    "facility_record": "master.facility.record",
    "facility_history": "master.facility.history",
    "facility_history_intro": "master.facility.history_intro",
    "facility_history_change": "master.facility.history_change",
    "facility_history_empty": "master.facility.history_empty",
    "facility_save": "master.common.save",
    "facility_deactivate": "master.common.deactivate",
    "facility_version": "master.common.version",
    "common_field": "master.common.field",
    "common_value": "master.common.value",
    "portfolios_title": "master.portfolios.title",
    "portfolios_heading": "master.portfolios.heading",
    "portfolios_new": "master.portfolios.new",
    "portfolios_empty": "master.portfolios.empty",
    "portfolios_code": "master.portfolios.code",
    "portfolios_name": "master.portfolios.name",
    "portfolios_branch": "master.portfolios.branch",
    "portfolio_title": "master.portfolio.title",
    "portfolio_heading": "master.portfolio.heading",
    "portfolio_save": "master.common.save",
    "form_error": "master.common.form_error",
    "required": "master.common.required",
}


def create_master_data_router(
    service: MasterDataService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
    borrower_create_only: bool = False,
) -> APIRouter:
    """Build protected master-data browser routes.

    ``borrower_create_only`` exposes the static ``/borrowers/new`` routes so
    they can be registered before the case-file route at
    ``/borrowers/{reference}`` in the production application.
    """
    router = APIRouter(tags=["master-data-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get(
        "/borrowers/new",
        response_class=HTMLResponse,
        name="borrower_create_page",
        include_in_schema=not borrower_create_only,
    )
    async def borrower_create_page(
        request: Request, principal: Principal = _WRITE_DEP
    ) -> HTMLResponse:
        portfolios = service.list_portfolios(principal)
        return _render(
            request,
            fallback_environment,
            "screens/master_data/borrower_form.html",
            principal=principal,
            form={},
            mode="create",
            action="/borrowers/new",
            portfolio_options=_portfolio_options(portfolios),
        )

    @router.post(
        "/borrowers/new",
        response_class=HTMLResponse,
        name="borrower_create_submit",
        include_in_schema=not borrower_create_only,
    )
    async def borrower_create_submit(
        request: Request, principal: Principal = _WRITE_DEP
    ) -> Response:
        values = await _form_values(request)
        portfolios = service.list_portfolios(principal)
        try:
            payload = BorrowerCreate.model_validate(
                _none_for_empty(
                    values,
                    "cin",
                    "pan",
                    "industry_code",
                    "group_id",
                    "constitution",
                    "incorporation_date",
                )
            )
            row = service.create_borrower(principal, **payload.model_dump())
        except (DomainError, PydanticValidationError) as error:
            domain_error = _as_domain_error(error, resource="borrower")
            return _render(
                request,
                fallback_environment,
                "screens/master_data/borrower_form.html",
                status_code=422,
                principal=principal,
                form=values,
                mode="create",
                action="/borrowers/new",
                portfolio_options=_portfolio_options(portfolios),
                error=domain_error.message,
                error_field=domain_error.field or "",
            )
        return RedirectResponse(f"/borrowers/{row.reference}/master-data", status_code=303)

    if borrower_create_only:
        return router

    @router.get("/borrowers", response_class=HTMLResponse, name="borrower_list")
    async def borrower_list(
        request: Request,
        q: str | None = Query(None, max_length=100),
        portfolio: str | None = Query(None, max_length=36),
        status: str | None = Query(None, max_length=10),
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        page = _page_number(request)
        portfolio_id = _optional_uuid(portfolio, "portfolio")
        active_only = _borrower_status(status)
        fetched = service.list_borrowers(
            principal,
            active_only=active_only,
            portfolio_id=portfolio_id,
            search=q,
            limit=_PAGE_SIZE + 1,
            offset=(page - 1) * _PAGE_SIZE,
        )
        rows, has_next = _split_page(fetched)
        portfolios = service.list_portfolios(principal)
        labels = _labels(request)
        return _render(
            request,
            fallback_environment,
            "screens/master_data/borrowers.html",
            principal=principal,
            rows=rows,
            table_rows=_borrower_rows(rows, labels, detail_suffix="/master-data"),
            filters={
                "q": q.strip() if q else "",
                "portfolio": str(portfolio_id) if portfolio_id else "",
                "status": (
                    "active" if active_only is True else "inactive" if active_only is False else ""
                ),
            },
            borrower_filters_enabled=True,
            master_detail_suffix="/master-data",
            portfolio_filter_options=_portfolio_filter_options(portfolios),
            pagination=_pagination("/borrowers", request, page=page, has_next=has_next),
        )

    @router.get(
        "/borrowers/{reference}/master-data",
        response_class=HTMLResponse,
        name="borrower_master_detail",
    )
    @router.get("/borrowers/{reference}", response_class=HTMLResponse, name="borrower_detail")
    async def borrower_detail(
        request: Request, reference: str, principal: Principal = _READ_DEP
    ) -> HTMLResponse:
        borrower = service.get_borrower(principal, reference)
        facilities = service.list_facilities_for_borrower(principal, borrower.id)
        return _render(
            request,
            fallback_environment,
            "screens/master_data/borrower_detail.html",
            principal=principal,
            borrower=borrower,
            facilities=facilities,
            facility_rows=_facility_rows(facilities),
            form={"legal_name": borrower.legal_name, "expected_version": borrower.version},
        )

    @router.post("/borrowers/{reference}/edit", response_class=HTMLResponse, name="borrower_edit")
    async def borrower_edit(
        request: Request, reference: str, principal: Principal = _WRITE_DEP
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = BorrowerUpdate.model_validate(
                _none_for_empty(
                    values,
                    "cin",
                    "pan",
                    "industry_code",
                    "group_id",
                    "constitution",
                    "portfolio_id",
                    "incorporation_date",
                )
            )
            service.update_borrower(
                principal,
                reference,
                expected_version=payload.expected_version,
                **payload.model_dump(exclude={"expected_version"}, exclude_unset=True),
            )
        except PydanticValidationError as error:
            raise _as_domain_error(error, resource="borrower") from error
        return RedirectResponse(f"/borrowers/{reference}/master-data", status_code=303)

    @router.post(
        "/borrowers/{reference}/deactivate",
        response_class=HTMLResponse,
        name="borrower_deactivate",
    )
    async def borrower_deactivate(
        request: Request, reference: str, principal: Principal = _WRITE_DEP
    ) -> Response:
        values = await _form_values(request)
        service.deactivate_borrower(
            principal,
            reference,
            expected_version=_positive_int(values.get("expected_version"), "expected_version"),
        )
        return RedirectResponse(f"/borrowers/{reference}/master-data", status_code=303)

    @router.get("/facilities", response_class=HTMLResponse, name="facility_list")
    async def facility_list(
        request: Request,
        q: str | None = Query(None, max_length=100),
        facility_type: str | None = Query(None, max_length=50, alias="type"),
        currency: str | None = Query(None, max_length=3),
        status: str | None = Query(None, max_length=12),
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        page = _page_number(request)
        filters = {
            "q": (q or "").strip(),
            "type": (facility_type or "").strip(),
            "currency": (currency or "").strip().upper(),
            "status": _facility_status(status),
        }
        listings = service.list_facility_listings(
            principal,
            status=filters["status"],
            search=filters["q"] or None,
            facility_type=filters["type"] or None,
            currency=filters["currency"] or None,
            limit=_PAGE_SIZE + 1,
            offset=(page - 1) * _PAGE_SIZE,
        )
        rows, has_next = _split_page(listings)
        total = service.count_facilities(
            principal,
            status=filters["status"],
            search=filters["q"] or None,
            facility_type=filters["type"] or None,
            currency=filters["currency"] or None,
        )
        choices = service.facility_filter_values(principal)
        labels = _labels(request)
        return _render(
            request,
            fallback_environment,
            "screens/master_data/facilities.html",
            principal=principal,
            rows=[listing.facility for listing in rows],
            table_rows=facility_table_rows(rows, today=_today(service)),
            filters=filters,
            filters_active=bool(
                filters["q"]
                or filters["type"]
                or filters["currency"]
                or filters["status"] != CURRENT_STATUS
            ),
            type_options=facility_filter_options(
                choices["facility_type"],
                all_label=labels["facilities_filter_all_types"],
            ),
            currency_options=facility_filter_options(
                choices["currency"],
                all_label=labels["facilities_filter_all_currencies"],
                humanise=False,
            ),
            status_options=_facility_status_options(labels),
            result_summary=_result_summary(request, page=page, shown=len(rows), total=total),
            pagination=_pagination("/facilities", request, page=page, has_next=has_next),
        )

    @router.get("/facilities/insights", response_class=HTMLResponse, name="facility_insights")
    async def facility_insights(request: Request, principal: Principal = _READ_DEP) -> HTMLResponse:
        """Summarise the in-scope facility book: what is held, and how it moved."""
        book = service.facility_book(principal, limit=_MAX_BOOK_ROWS + 1)
        truncated = len(book) > _MAX_BOOK_ROWS
        return _render(
            request,
            fallback_environment,
            "screens/master_data/facility_insights.html",
            principal=principal,
            book=facility_book_view(book[:_MAX_BOOK_ROWS], today=_today(service)),
            truncated_note=(
                _message(
                    request,
                    "master.insights.truncated",
                    limit=format_indian_number(Decimal(_MAX_BOOK_ROWS)),
                )
                if truncated
                else ""
            ),
        )

    @router.get("/facilities/new", response_class=HTMLResponse, name="facility_create_page")
    async def facility_create_page(
        request: Request, principal: Principal = _WRITE_DEP
    ) -> HTMLResponse:
        return _render(
            request,
            fallback_environment,
            "screens/master_data/facility_form.html",
            principal=principal,
            form={},
            mode="create",
            action="/facilities/new",
        )

    @router.post("/facilities/new", response_class=HTMLResponse, name="facility_create_submit")
    async def facility_create_submit(
        request: Request, principal: Principal = _WRITE_DEP
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = FacilityCreate.model_validate(
                _none_for_empty(
                    values,
                    "drawing_power",
                    "outstanding",
                    "security_type",
                    "pricing_bps",
                    "maturity_date",
                )
            )
            row = service.create_facility(principal, **payload.model_dump())
        except (DomainError, PydanticValidationError) as error:
            domain_error = _as_domain_error(error, resource="facility")
            return _render(
                request,
                fallback_environment,
                "screens/master_data/facility_form.html",
                status_code=422,
                principal=principal,
                form=values,
                mode="create",
                action="/facilities/new",
                error=domain_error.message,
                error_field=domain_error.field or "",
            )
        return RedirectResponse(f"/facilities/{row.reference}", status_code=303)

    @router.get("/facilities/{reference}", response_class=HTMLResponse, name="facility_detail")
    async def facility_detail(
        request: Request, reference: str, principal: Principal = _READ_DEP
    ) -> HTMLResponse:
        facility = service.get_facility(principal, reference)
        borrower = service.get_borrower_by_id(principal, facility.borrower_id)
        revisions = service.facility_revisions(principal, reference)
        return _render(
            request,
            fallback_environment,
            "screens/master_data/facility_detail.html",
            principal=principal,
            facility=facility,
            borrower=borrower,
            detail_fields=facility_detail_fields(facility, borrower, today=_today(service)),
            revision_rows=facility_revision_rows(revisions, current_reference=facility.reference),
            form={
                "expected_version": facility.version,
                "sanctioned_limit": facility.sanctioned_limit,
            },
        )

    @router.post("/facilities/{reference}/edit", response_class=HTMLResponse, name="facility_edit")
    async def facility_edit(
        request: Request, reference: str, principal: Principal = _WRITE_DEP
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = FacilityUpdate.model_validate(
                _none_for_empty(
                    values,
                    "effective_from",
                    "new_reference",
                    "facility_type",
                    "currency",
                    "drawing_power",
                    "outstanding",
                    "security_type",
                    "pricing_bps",
                    "sanction_date",
                    "maturity_date",
                )
            )
        except PydanticValidationError as error:
            raise _as_domain_error(error, resource="facility") from error
        service.update_facility(
            principal,
            reference,
            expected_version=payload.expected_version,
            sanctioned_limit=payload.sanctioned_limit,
            effective_from=payload.effective_from,
            new_reference=payload.new_reference,
            **payload.model_dump(
                exclude={"expected_version", "sanctioned_limit", "effective_from", "new_reference"},
                exclude_unset=True,
            ),
        )
        return RedirectResponse(f"/facilities/{reference}", status_code=303)

    @router.post(
        "/facilities/{reference}/deactivate",
        response_class=HTMLResponse,
        name="facility_deactivate",
    )
    async def facility_deactivate(
        request: Request, reference: str, principal: Principal = _WRITE_DEP
    ) -> Response:
        values = await _form_values(request)
        service.deactivate_facility(
            principal,
            reference,
            expected_version=_positive_int(values.get("expected_version"), "expected_version"),
        )
        return RedirectResponse(f"/facilities/{reference}", status_code=303)

    @router.get("/portfolios", response_class=HTMLResponse, name="portfolio_list")
    async def portfolio_list(request: Request, principal: Principal = _READ_DEP) -> HTMLResponse:
        rows = service.list_portfolios(principal)
        labels = _labels(request)
        return _render(
            request,
            fallback_environment,
            "screens/master_data/portfolios.html",
            principal=principal,
            rows=rows,
            table_rows=_portfolio_rows(rows, labels),
        )

    @router.get("/portfolios/new", response_class=HTMLResponse, name="portfolio_create_page")
    async def portfolio_create_page(
        request: Request, principal: Principal = _WRITE_DEP
    ) -> HTMLResponse:
        return _render(
            request,
            fallback_environment,
            "screens/master_data/portfolio_form.html",
            principal=principal,
            form={},
            mode="create",
            action="/portfolios/new",
        )

    @router.post("/portfolios/new", response_class=HTMLResponse, name="portfolio_create_submit")
    async def portfolio_create_submit(
        request: Request, principal: Principal = _WRITE_DEP
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = PortfolioCreate.model_validate(
                _none_for_empty(values, "parent_id", "branch_code")
            )
            row = service.create_portfolio(principal, **payload.model_dump())
        except (DomainError, PydanticValidationError) as error:
            domain_error = _as_domain_error(error, resource="portfolio")
            return _render(
                request,
                fallback_environment,
                "screens/master_data/portfolio_form.html",
                status_code=422,
                principal=principal,
                form=values,
                mode="create",
                action="/portfolios/new",
                error=domain_error.message,
                error_field=domain_error.field or "",
            )
        return RedirectResponse(f"/portfolios/{row.id}", status_code=303)

    @router.get("/portfolios/{portfolio_id}", response_class=HTMLResponse, name="portfolio_detail")
    async def portfolio_detail(
        request: Request, portfolio_id: UUID, principal: Principal = _READ_DEP
    ) -> HTMLResponse:
        portfolio = service.get_portfolio(principal, portfolio_id)
        return _render(
            request,
            fallback_environment,
            "screens/master_data/portfolio_detail.html",
            principal=principal,
            portfolio=portfolio,
            form={"expected_version": portfolio.version},
        )

    @router.post(
        "/portfolios/{portfolio_id}/edit", response_class=HTMLResponse, name="portfolio_edit"
    )
    async def portfolio_edit(
        request: Request, portfolio_id: UUID, principal: Principal = _WRITE_DEP
    ) -> Response:
        values = await _form_values(request)
        try:
            payload = PortfolioUpdate.model_validate(
                _none_for_empty(values, "code", "name", "parent_id", "branch_code")
            )
        except PydanticValidationError as error:
            raise _as_domain_error(error, resource="portfolio") from error
        service.update_portfolio(
            principal,
            portfolio_id,
            expected_version=payload.expected_version,
            **payload.model_dump(exclude={"expected_version"}, exclude_unset=True),
        )
        return RedirectResponse(f"/portfolios/{portfolio_id}", status_code=303)

    return router


def _page_number(request: Request) -> int:
    """Read a 1-based `page` query parameter, ignoring anything unusable."""
    raw = request.query_params.get("page")
    if raw is None:
        return 1
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return 1
    return page if 1 <= page <= _MAX_PAGE else 1


def _split_page(fetched: Sequence[_RowT]) -> tuple[Sequence[_RowT], bool]:
    """Split a `limit + 1` fetch into the page and a has-more flag.

    Reading one row past the page is how the screen knows whether a next page
    exists without a second `COUNT(*)` over the whole book.
    """
    if len(fetched) > _PAGE_SIZE:
        return fetched[:_PAGE_SIZE], True
    return fetched, False


def _pagination(path: str, request: Request, *, page: int, has_next: bool) -> dict[str, object]:
    """Previous/next links for a paged list screen.

    Master-data lists are unbounded by nature — this deployment carries 12,000
    facilities — and rendering the whole book produced a multi-megabyte page
    that no browser, and no response-rewriting middleware, should be asked to
    handle.  Other query parameters are preserved so paging never silently
    drops a filter.
    """
    preserved = [(key, value) for key, value in request.query_params.multi_items() if key != "page"]

    def href(target: int) -> str:
        query = urlencode([*preserved, ("page", str(target))])
        return f"{path}?{query}"

    return {
        "page": page,
        "page_size": _PAGE_SIZE,
        "previous_href": href(page - 1) if page > 1 else None,
        "next_href": href(page + 1) if has_next else None,
    }


def _locale(request: Request) -> str:
    locale = request.cookies.get("covenant_radar_locale", "en").lower()
    return locale if locale in {"en", "hi"} else "en"


def _message(request: Request, key: str, **values: object) -> str:
    """Translate one key, falling back to the key when no catalogue is bound.

    The screens below take their static text from ``_LABEL_KEYS``; this is
    for the handful of strings that carry a number the route has just
    computed, which cannot be looked up ahead of time.
    """
    catalogue = getattr(request.app.state, "catalogue", None)
    translator = getattr(catalogue, "translate", None)
    if not callable(translator):
        return key
    return str(translator(key, locale=_locale(request), **values))


def _labels(request: Request) -> dict[str, str]:
    catalogue = getattr(request.app.state, "catalogue", None)
    translator = getattr(catalogue, "translate", None)
    locale = _locale(request)
    return {
        name: str(translator(key, locale=locale)) if callable(translator) else key
        for name, key in _LABEL_KEYS.items()
    }


def _today(service: MasterDataService) -> date:
    """Today in the deployment's own timezone, from the service's clock.

    Every "matures in N days" note on these screens is measured against this
    one value, so a request never mixes two notions of today.
    """
    return service.clock.now().astimezone(IST).date()


def _facility_status(value: str | None) -> str:
    """Normalise the effective-dating filter, defaulting to current rows.

    An unrecognised value falls back to ``current`` rather than raising: the
    filter is a browsing convenience, and the safe reading of a mistyped
    query string is the screen's own default, not an error page.
    """
    normalized = (value or "").strip().lower()
    if not normalized:
        return CURRENT_STATUS
    return normalized if normalized in FACILITY_STATUSES else CURRENT_STATUS


def _facility_status_options(labels: Mapping[str, str]) -> list[dict[str, str | bool]]:
    return [
        {
            "value": CURRENT_STATUS,
            "label": labels["facilities_filter_status_current"],
            "disabled": False,
        },
        {
            "value": SUPERSEDED_STATUS,
            "label": labels["facilities_filter_status_superseded"],
            "disabled": False,
        },
        {
            "value": ALL_STATUSES,
            "label": labels["facilities_filter_status_all"],
            "disabled": False,
        },
    ]


def _result_summary(request: Request, *, page: int, shown: int, total: int) -> str:
    """Return the "showing 1–50 of 12,000" line above a paged list.

    A master-data book is unbounded, so a page with no count in front of it
    tells a reader nothing about how much they have not seen.
    """
    if total == 0 or shown == 0:
        return _message(request, "master.facilities.result_empty")
    first = (page - 1) * _PAGE_SIZE + 1
    return _message(
        request,
        "master.facilities.result_summary",
        first=format_indian_number(Decimal(first)),
        last=format_indian_number(Decimal(first + shown - 1)),
        total=format_indian_number(Decimal(total)),
    )


def _render(
    request: Request,
    fallback_environment: Environment,
    template_name: str,
    *,
    principal: Principal,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template(template_name)
    locale = _locale(request)
    theme = theme_for_request(request)
    labels = _labels(request)
    values = {
        "request": request,
        "principal": principal,
        "locale": locale,
        "theme": theme,
        "text_direction": "ltr",
        "labels": labels,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "form": {},
        "error": "",
        "error_field": "",
        **context,
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


async def _form_values(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted form is too large.", field="form")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/x-www-form-urlencoded" or not content_type:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError("The submitted form is not valid UTF-8.", field="form") from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values}
    form = await request.form()
    values: dict[str, str] = {}
    for key, value in form.multi_items():
        if key != "csrf_token" and isinstance(value, str):
            values[key] = value
    return values


def _none_for_empty(values: Mapping[str, str], *optional_fields: str) -> dict[str, str | None]:
    normalized: dict[str, str | None] = dict(values)
    for field in optional_fields:
        current = normalized.get(field)
        if current is None or not current.strip():
            normalized[field] = None
    return normalized


def _positive_int(value: str | None, field: str) -> int:
    try:
        parsed = int(value or "")
    except ValueError as error:
        raise ValidationError(f"{field} must be a positive integer.", field=field) from error
    if parsed < 1:
        raise ValidationError(f"{field} must be a positive integer.", field=field)
    return parsed


def _optional_uuid(value: str | None, field: str) -> UUID | None:
    if value is None or not value.strip():
        return None
    try:
        return UUID(value.strip())
    except ValueError as error:
        raise ValidationError(f"{field} must be a UUID.", field=field) from error


def _borrower_status(value: str | None) -> bool | None:
    normalized = (value or "").strip().lower()
    if normalized in {"", "all"}:
        return None
    if normalized == "active":
        return True
    if normalized == "inactive":
        return False
    raise ValidationError("status must be active or inactive.", field="status")


def _as_domain_error(error: DomainError | PydanticValidationError, *, resource: str) -> DomainError:
    if isinstance(error, DomainError):
        return error
    errors = error.errors()
    if errors:
        location_parts = errors[0].get("loc", ())
        message = errors[0].get("msg", "invalid value")
    else:
        location_parts = ()
        message = "invalid value"
    location = ".".join(str(part) for part in location_parts if part != "body")
    field = f"{resource}.{location}" if location else resource
    return ValidationError(f"{field}: {message}.", field=field)


def _borrower_rows(
    rows: Sequence[Borrower], labels: Mapping[str, str], *, detail_suffix: str = ""
) -> list[dict[str, object]]:
    return [
        {
            "id": row.reference,
            "reference": row.reference,
            "legal_name": row.legal_name,
            "portfolio": str(row.portfolio_id),
            "status": "active" if row.is_active else "inactive",
            "actions": Markup(
                '<a class="button" href="/borrowers/{href}{suffix}">{label}</a>'
            ).format(
                href=quote(row.reference, safe=""),
                suffix=detail_suffix,
                label=labels["borrowers_open"],
            ),
        }
        for row in rows
    ]


def _facility_rows(rows: Sequence[Facility]) -> list[dict[str, object]]:
    return [
        {
            "id": row.reference,
            "reference": row.reference,
            "borrower": str(row.borrower_id),
            "limit": row.sanctioned_limit,
            "effective": row.effective_from,
        }
        for row in rows
    ]


def _portfolio_rows(
    rows: Sequence[Portfolio], labels: Mapping[str, str]
) -> list[dict[str, object]]:
    return [
        {
            "id": str(row.id),
            "code": row.code,
            "name": row.name,
            "branch": row.branch_code or "",
            "actions": Markup('<a class="button" href="/portfolios/{href}">{label}</a>').format(
                href=quote(str(row.id), safe=""),
                label=labels["borrowers_open"],
            ),
        }
        for row in rows
    ]


def _portfolio_options(rows: Sequence[Portfolio]) -> list[dict[str, str | bool]]:
    """Render only portfolios that the current user may assign to a borrower."""
    options: list[dict[str, str | bool]] = [
        {"value": "", "label": "Select a portfolio", "disabled": True}
    ]
    options.extend(
        {
            "value": str(row.id),
            "label": f"{row.code} — {row.name}",
            "disabled": False,
        }
        for row in rows
    )
    return options


def _portfolio_filter_options(rows: Sequence[Portfolio]) -> list[dict[str, str | bool]]:
    """Render the scoped portfolio choices used by the borrower filter."""
    options: list[dict[str, str | bool]] = [
        {"value": "", "label": "All portfolios", "disabled": False}
    ]
    options.extend(
        {
            "value": str(row.id),
            "label": f"{row.code} — {row.name}",
            "disabled": False,
        }
        for row in rows
    )
    return options


__all__ = ["create_master_data_router"]
