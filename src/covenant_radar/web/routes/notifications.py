"""Server-rendered in-app notification centre (`T-119`).

The centre is deliberately usable as ordinary HTML forms.  JavaScript can
enhance the experience later, but it is not part of the authorization or
read-state contract.  All content is supplied by the disclosure-safe service
and all mutations remain in the request transaction.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.core.clock import Clock
from covenant_radar.db.session import is_database_session
from covenant_radar.notifications.inapp import (
    InAppNotificationService,
    NotificationAuditWriter,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_PAGE_SIZE = 100
_READ = requires(Permission.VIEW_QUEUE)
_READ_DEP = Depends(_READ)
_WRITE_DEP = Depends(_READ)

_LABELS = {
    "title": "Notifications",
    "heading": "Notification centre",
    "subheading": "Durable notices for the work in your queue.",
    "unread_count": "Unread notifications",
    "status": "Status",
    "all": "All",
    "unread": "Unread",
    "read": "Read",
    "template": "Notification type",
    "all_templates": "All notification types",
    "apply": "Apply filters",
    "mark_all": "Mark all as read",
    "mark_read": "Mark as read",
    "read_status": "Read",
    "unread_status": "Unread",
    "received": "Received",
    "type": "Type",
    "open": "Open",
    "empty_title": "No notifications in this view",
    "empty_message": "New scoped warnings and workflow notices will appear here.",
    "no_access_message": (
        "This notice is retained, but its subject is no longer in your access scope."
    ),
    "previous": "Previous",
    "next": "Next",
    "page": "Page",
}


def create_notifications_router(
    session: Session,
    *,
    service: InAppNotificationService | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
    audit_writer: object | None = None,
    clock: Clock | None = None,
) -> APIRouter:
    """Build the protected notification-centre routes over one session."""

    if not is_database_session(session):
        raise TypeError("create_notifications_router requires a SQLAlchemy Session.")
    if service is None:
        service = InAppNotificationService(
            session,
            audit=cast(NotificationAuditWriter | None, audit_writer),
            clock=clock,
        )
    elif not isinstance(service, InAppNotificationService):
        raise TypeError("create_notifications_router service must be InAppNotificationService.")
    if service.session is not session:
        raise ValueError("create_notifications_router requires a service using the same session.")

    router = APIRouter(tags=["notifications-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/notifications", response_class=HTMLResponse, name="notifications")
    def notifications(
        request: Request,
        status: str = Query("all"),
        template: str | None = Query(None),
        filter_value: str | None = Query(None, alias="filter"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=_MAX_PAGE_SIZE),
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        view = service.list_notifications(
            principal,
            status=status,
            template=template or None,
            page=page,
            page_size=page_size,
            notification_filter=filter_value or None,
        )
        return _render(
            request,
            fallback_environment,
            principal=principal,
            view=view,
            template_options=tuple(item.name for item in service.template_registry),
            query=_query_string(view.status, view.template, filter_value, view.page_size),
        )

    @router.get(
        "/notifications/unread-count",
        response_class=HTMLResponse,
        name="notifications_unread_count",
    )
    def notifications_unread_count(
        request: Request,
        principal: Principal = _READ_DEP,
    ) -> HTMLResponse:
        environment = getattr(request.app.state, "template_env", fallback_environment)
        template = environment.get_template("_components/notification_link.html")
        unread_count = service.unread_count(principal)
        response = HTMLResponse(
            template.render(
                request=request,
                principal=principal,
                unread_count=unread_count,
                notification_label="Notifications",
                notification_aria_label=f"{unread_count} unread notifications",
            )
        )
        response.headers["Vary"] = "HX-Request, HX-Target"
        return response

    @router.post(
        "/notifications/{notification_id}/read",
        name="notification_read",
    )
    def notification_read(
        notification_id: UUID,
        principal: Principal = _WRITE_DEP,
    ) -> RedirectResponse:
        service.mark_read(principal, notification_id)
        return RedirectResponse("/notifications", status_code=303)

    @router.post("/notifications/read-all", name="notifications_read_all")
    def notifications_read_all(principal: Principal = _WRITE_DEP) -> RedirectResponse:
        service.mark_all_read(principal)
        return RedirectResponse("/notifications", status_code=303)

    return router


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
        and request.headers.get("HX-Target") == "notification-results"
    )
    template_name = (
        "_components/notification_results.html"
        if is_fragment
        else "screens/notifications/index.html"
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


def _query_string(
    status: str,
    template: str | None,
    filter_value: str | None,
    page_size: int,
) -> str:
    values: dict[str, str | int] = {"status": status, "page_size": page_size}
    if template:
        values["template"] = template
    if filter_value:
        values["filter"] = filter_value
    return urlencode(values)


__all__ = ["create_notifications_router"]
