"""Feature-flagged browser endpoint for live workspace enhancements."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.notifications.inapp import InAppNotificationService
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.live_activity import LiveActivityService

_READ = requires(Permission.VIEW_QUEUE)
_READ_DEP = Depends(_READ)


def create_live_router(
    session: Session,
    *,
    notifications: InAppNotificationService,
    cursor_secret: bytes,
) -> APIRouter:
    router = APIRouter(tags=["live-workspace"])

    @router.get("/live/updates", name="live_updates")
    def updates(
        request: Request,
        cursor: str | None = Query(None, max_length=1024),
        context: str | None = Query(None, max_length=120),
        principal: Principal = _READ_DEP,
    ) -> Response:
        # Context is intentionally advisory.  Authorization remains entirely
        # server side and the envelope contains only safe, durable events.
        del context
        if not request.app.state.settings.web.live_workspace_enabled:
            return Response(status_code=404)
        envelope = LiveActivityService(
            session, notifications, cursor_secret=cursor_secret
        ).updates(principal, cursor=cursor)
        etag = f'W/"{envelope.cursor}"'
        if request.headers.get("if-none-match") == etag and not envelope.items:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
        return JSONResponse(envelope.as_dict(), headers={"ETag": etag, "Cache-Control": "no-store"})

    return router


__all__ = ["create_live_router"]
