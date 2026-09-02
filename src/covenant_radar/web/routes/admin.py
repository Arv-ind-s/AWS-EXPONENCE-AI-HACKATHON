"""Browser routes for the administration identity console (T-113)."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from typing import cast
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, scoped_session

from covenant_radar.api.deps import requires
from covenant_radar.config.capabilities import Capabilities
from covenant_radar.config.thresholds import DEFAULT_THRESHOLD_PATH, ThresholdStore
from covenant_radar.core.clock import Clock
from covenant_radar.core.context import get_request_id, new_job_run_id
from covenant_radar.core.errors import (
    Conflict,
    DomainError,
    ExternalServiceError,
    NotFound,
    ValidationError,
)
from covenant_radar.db.models.operations import JobRun, RetentionPurgeLog
from covenant_radar.db.repositories.thresholds import SqlAlchemyThresholdRepository
from covenant_radar.db.session import is_database_session
from covenant_radar.scheduler.ledger import JobAlreadyRunningError, JobLedger
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.admin_users import AdminUsersService
from covenant_radar.services.catalogue import CatalogueService
from covenant_radar.web.errors import status_for_error
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.admin_config import (
    BandChangePreview,
    build_band_change_preview,
    load_admin_config_view,
)
from covenant_radar.web.view_models.admin_ops import (
    AuditWriter as AdminOpsAuditWriter,
)
from covenant_radar.web.view_models.admin_ops import (
    RetentionPolicy,
    RetentionPreviewView,
    apply_retention_policy,
    current_retention_policy,
    load_admin_ops_view,
    preview_retention,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 128 * 1024
_MAX_FIELD_BYTES = 8 * 1024
_ADMIN_DEP = Depends(requires(Permission.MANAGE_USERS))
_CONFIG_READ_DEP = Depends(requires(Permission.PROPOSE_THRESHOLDS))
_CONFIG_WRITE_DEP = Depends(requires(Permission.PROPOSE_THRESHOLDS))
_CONFIG_APPROVE_DEP = Depends(requires(Permission.APPROVE_THRESHOLDS))
_OPS_DEP = Depends(requires(Permission.MANAGE_JOBS))


def create_admin_users_router(
    service: AdminUsersService,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build protected identity-management routes over one service."""

    if not isinstance(service, AdminUsersService):
        raise TypeError("create_admin_users_router requires an AdminUsersService.")
    router = APIRouter(tags=["admin-users-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/admin/users", response_class=HTMLResponse, name="admin_users")
    async def admin_users(
        request: Request,
        user_id: UUID | None = None,
        principal: Principal = _ADMIN_DEP,
    ) -> HTMLResponse:
        return _render(
            request,
            fallback_environment,
            service,
            principal=principal,
            selected_user_id=user_id,
        )

    @router.get("/admin/users/{user_id}", response_class=HTMLResponse, name="admin_user_detail")
    async def admin_user_detail(
        request: Request,
        user_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> HTMLResponse:
        return _render(
            request,
            fallback_environment,
            service,
            principal=principal,
            selected_user_id=user_id,
        )

    @router.post("/admin/users", response_class=HTMLResponse, name="admin_user_create")
    async def admin_user_create(
        request: Request,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.create_user(
                principal,
                username=_one(values, "username"),
                email=_one(values, "email"),
                full_name=_one(values, "full_name"),
                password=_optional(values, "password"),
                role_codes=_csv(values, "role_codes", fallback_key="role"),
                portfolio_scopes=_scope_values(values),
                auth_source=_one(values, "auth_source", default="local"),
                external_subject=_optional(values, "external_subject"),
                locale=_one(values, "locale", default="en"),
                theme=_one(values, "theme", default="light"),
            )
        except DomainError as error:
            return _render(
                request,
                fallback_environment,
                service,
                principal=principal,
                form=values,
                error=error.message,
                status_code=status_for_error(error),
            )
        return RedirectResponse("/admin/users", status_code=303)

    @router.post(
        "/admin/users/{user_id}/roles",
        response_class=HTMLResponse,
        name="admin_user_roles",
    )
    async def admin_user_roles(
        request: Request,
        user_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            result = service.assign_roles(
                principal,
                user_id,
                _csv(values, "role_codes", fallback_key="role"),
                reason=_one(values, "reason"),
            )
        except DomainError as error:
            return _render_error_for_user(
                request,
                fallback_environment,
                service,
                principal,
                user_id,
                values,
                error,
            )
        destination = f"/admin/users/{user_id}"
        if result.pending:
            destination += "?pending=1"
        return RedirectResponse(destination, status_code=303)

    @router.post(
        "/admin/users/{user_id}/scope",
        response_class=HTMLResponse,
        name="admin_user_scope",
    )
    async def admin_user_scope(
        request: Request,
        user_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.set_portfolio_scope(
                principal,
                user_id,
                _scope_values(values),
                reason=_one(values, "reason"),
            )
        except DomainError as error:
            return _render_error_for_user(
                request,
                fallback_environment,
                service,
                principal,
                user_id,
                values,
                error,
            )
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @router.post(
        "/admin/users/{user_id}/deactivate",
        response_class=HTMLResponse,
        name="admin_user_deactivate",
    )
    async def admin_user_deactivate(
        request: Request,
        user_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.deactivate_user(principal, user_id, reason=_one(values, "reason"))
        except DomainError as error:
            return _render_error_for_user(
                request,
                fallback_environment,
                service,
                principal,
                user_id,
                values,
                error,
            )
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @router.post(
        "/admin/users/{user_id}/reactivate",
        response_class=HTMLResponse,
        name="admin_user_reactivate",
    )
    async def admin_user_reactivate(
        request: Request,
        user_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.reactivate_user(principal, user_id, reason=_one(values, "reason"))
        except DomainError as error:
            return _render_error_for_user(
                request,
                fallback_environment,
                service,
                principal,
                user_id,
                values,
                error,
            )
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @router.post(
        "/admin/users/{user_id}/password",
        response_class=HTMLResponse,
        name="admin_user_password_reset",
    )
    async def admin_user_password_reset(
        request: Request,
        user_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        if _one(values, "password") != _one(values, "password_confirmation"):
            error = ValidationError(
                "The replacement passwords do not match.", field="password_confirmation"
            )
            return _render_error_for_user(
                request,
                fallback_environment,
                service,
                principal,
                user_id,
                values,
                error,
            )
        try:
            service.reset_password(
                principal,
                user_id,
                password=_one(values, "password"),
                reason=_one(values, "reason"),
            )
        except DomainError as error:
            return _render_error_for_user(
                request,
                fallback_environment,
                service,
                principal,
                user_id,
                values,
                error,
            )
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @router.post(
        "/admin/users/{user_id}/sso",
        response_class=HTMLResponse,
        name="admin_user_sso_mapping",
    )
    async def admin_user_sso_mapping(
        request: Request,
        user_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.configure_sso_mapping(
                principal,
                user_id,
                auth_source=_one(values, "auth_source"),
                external_subject=_optional(values, "external_subject"),
                password=_optional(values, "password"),
                reason=_one(values, "reason"),
            )
        except DomainError as error:
            return _render_error_for_user(
                request,
                fallback_environment,
                service,
                principal,
                user_id,
                values,
                error,
            )
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @router.post(
        "/admin/users/{user_id}/sessions/{session_id}/revoke",
        response_class=HTMLResponse,
        name="admin_user_session_revoke",
    )
    async def admin_user_session_revoke(
        request: Request,
        user_id: UUID,
        session_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.revoke_session(
                principal,
                user_id,
                session_id,
                reason=_one(values, "reason"),
            )
        except DomainError as error:
            return _render_error_for_user(
                request,
                fallback_environment,
                service,
                principal,
                user_id,
                values,
                error,
            )
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @router.post(
        "/admin/users/role-approvals/{request_id}/approve",
        response_class=HTMLResponse,
        name="admin_role_approval",
    )
    async def admin_role_approval(
        request: Request,
        request_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        await _form_values(request)
        try:
            service.approve_role_assignment(principal, request_id)
        except DomainError as error:
            return _render(
                request,
                fallback_environment,
                service,
                principal=principal,
                error=error.message,
                status_code=status_for_error(error),
            )
        return RedirectResponse("/admin/users", status_code=303)

    @router.post(
        "/admin/users/role-approvals/{request_id}/reject",
        response_class=HTMLResponse,
        name="admin_role_rejection",
    )
    async def admin_role_rejection(
        request: Request,
        request_id: UUID,
        principal: Principal = _ADMIN_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            service.reject_role_assignment(
                principal,
                request_id,
                reason=_one(values, "reason"),
            )
        except DomainError as error:
            return _render(
                request,
                fallback_environment,
                service,
                principal=principal,
                error=error.message,
                status_code=status_for_error(error),
            )
        return RedirectResponse("/admin/users", status_code=303)

    # T-114 lives on the same protected admin router so the production
    # application, which already includes this router, exposes configuration
    # without a second assembly path.  The configuration services use the
    # same request-scoped session and audit writer as the identity service.
    router.include_router(
        create_admin_config_router(
            service.session,
            catalogue_service=CatalogueService(cast(Session, service.session), audit=service.audit),
            template_directory=template_directory,
        )
    )
    router.include_router(
        create_admin_ops_router(
            service.session,
            audit=service.audit,
            template_directory=template_directory,
        )
    )

    return router


def create_admin_ops_router(
    session: Session | scoped_session[Session],
    *,
    runtime: object | None = None,
    settings: object | None = None,
    capabilities: Capabilities | None = None,
    audit: AdminOpsAuditWriter | None = None,
    clock: Clock | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected operational admin surface.

    The production composition passes the request-scoped session and audit
    writer here, while the live nightly runtime is resolved from
    ``app.state`` at request time.  That keeps the route testable and avoids
    binding a background runner or a request session during application
    startup.
    """

    if not is_database_session(session):
        raise TypeError("create_admin_ops_router requires a SQLAlchemy Session.")
    if audit is not None and not callable(getattr(audit, "record", None)):
        raise TypeError("audit must provide record().")
    router = APIRouter(tags=["admin-operations-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )
    trigger_locks: dict[str, Lock] = {}

    @router.get("/admin/jobs", response_class=HTMLResponse, name="admin_jobs")
    async def admin_jobs(
        request: Request,
        principal: Principal = _OPS_DEP,
    ) -> HTMLResponse:
        current_preview = preview_retention(
            session,
            current_retention_policy(session),
            clock=clock,
        )
        return _render_ops(
            request,
            fallback_environment,
            session,
            principal=principal,
            runtime=_request_runtime(request, runtime),
            settings=_request_settings(request, settings),
            capabilities=capabilities,
            clock=clock,
            retention_preview=current_preview,
        )

    @router.post("/admin/jobs", response_class=HTMLResponse, name="admin_jobs_retention")
    async def admin_jobs_retention(
        request: Request,
        principal: Principal = _OPS_DEP,
    ) -> Response:
        values = await _form_values(request)
        resolved_runtime = _request_runtime(request, runtime)
        resolved_settings = _request_settings(request, settings)
        try:
            current = current_retention_policy(session)
            policy = _parse_retention_policy(values, current)
            action = _one(values, "action").strip().lower()
            if not action:
                action = "apply_retention" if _one(values, "preview_token") else "preview_retention"
            if action in {"preview", "preview_retention"}:
                retention_preview = preview_retention(session, policy, clock=clock)
                return _render_ops(
                    request,
                    fallback_environment,
                    session,
                    principal=principal,
                    runtime=resolved_runtime,
                    settings=resolved_settings,
                    capabilities=capabilities,
                    clock=clock,
                    retention_preview=retention_preview,
                    form=values,
                )
            if action not in {"apply", "apply_retention", "save_retention"}:
                raise ValidationError(
                    "Retention action must be preview_retention or apply_retention.",
                    field="action",
                )
            supplied_token = _one(values, "preview_token").strip()
            if not supplied_token:
                raise ValidationError(
                    "Preview the retention change before applying it.",
                    field="preview_token",
                )
            retention_preview = preview_retention(session, policy, clock=clock)
            if not _constant_time_equal(supplied_token, retention_preview.token):
                raise Conflict(
                    "The retention preview is stale; calculate a new preview before applying."
                )
            writer = _request_audit(request, audit)
            apply_retention_policy(
                session,
                policy,
                actor_id=principal.id,
                audit=writer,
                clock=clock,
                request_id=getattr(request.state, "request_id", None),
            )
        except DomainError as error:
            return _render_ops(
                request,
                fallback_environment,
                session,
                principal=principal,
                runtime=resolved_runtime,
                settings=resolved_settings,
                capabilities=capabilities,
                clock=clock,
                form=values,
                error=error.message,
                status_code=status_for_error(error),
            )
        return RedirectResponse("/admin/jobs?retention=applied", status_code=303)

    @router.post("/admin/jobs/{name}/run", response_class=JSONResponse, name="admin_job_run")
    async def admin_job_run(
        request: Request,
        name: str,
        principal: Principal = _OPS_DEP,
    ) -> Response:
        values = await _form_values(request)
        try:
            run_id, trigger = _queue_job_trigger(
                session,
                _request_runtime(request, runtime),
                name,
                values,
                principal,
                trigger_locks,
            )
        except DomainError as error:
            return JSONResponse(
                {
                    "error": error.message,
                    "code": error.code,
                    "request_id": getattr(request.state, "request_id", get_request_id()),
                },
                status_code=status_for_error(error),
            )
        return JSONResponse(
            {
                "job_name": name,
                "run_id": run_id,
                "trigger": trigger,
                "state": "queued",
                "request_id": getattr(request.state, "request_id", get_request_id()),
            },
            status_code=202,
        )

    return router


def _render_ops(
    request: Request,
    fallback_environment: Environment,
    session: Session | scoped_session[Session],
    *,
    principal: Principal,
    runtime: object | None,
    settings: object | None,
    capabilities: Capabilities | None,
    clock: Clock | None,
    form: Mapping[str, list[str]] | None = None,
    error: str = "",
    status_code: int = 200,
    retention_preview: RetentionPreviewView | None = None,
) -> HTMLResponse:
    """Render the operations screen from a fresh durable read model."""

    environment = getattr(request.app.state, "template_env", fallback_environment)
    view = load_admin_ops_view(
        session,
        settings=settings,
        capabilities=capabilities,
        runtime=runtime,
        clock=clock,
        retention_preview=retention_preview,
    )
    template = environment.get_template("screens/admin/ops/index.html")
    response = HTMLResponse(
        template.render(
            request=request,
            principal=principal,
            view=view,
            error=error,
            form=dict(form or {}),
            locale=request.cookies.get("covenant_radar_locale", "en"),
            theme=theme_for_request(request),
            text_direction="ltr",
            csrf_token=getattr(request.state, "csrf_token", ""),
        ),
        status_code=status_code,
    )
    response.headers["Vary"] = "HX-Request, HX-Target"
    return response


def _request_runtime(request: Request, configured: object | None) -> object | None:
    return (
        configured
        if configured is not None
        else getattr(request.app.state, "nightly_runtime", None)
    )


def _request_settings(request: Request, configured: object | None) -> object | None:
    return configured if configured is not None else getattr(request.app.state, "settings", None)


def _request_audit(request: Request, configured: AdminOpsAuditWriter | None) -> AdminOpsAuditWriter:
    writer = (
        configured if configured is not None else getattr(request.app.state, "audit_writer", None)
    )
    if not callable(getattr(writer, "record", None)):
        raise ExternalServiceError("The audit writer is not configured; the change was refused.")
    return cast(AdminOpsAuditWriter, writer)


def _parse_retention_policy(
    values: Mapping[str, list[str]], current: RetentionPolicy
) -> RetentionPolicy:
    raw_json = _one(values, "retention").strip()
    if raw_json:
        try:
            decoded = json.loads(raw_json, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as error:
            raise ValidationError(
                f"retention must be valid JSON: {error.msg}.", field="retention"
            ) from error
        except ValueError as error:
            raise ValidationError(str(error), field="retention") from error
        if not isinstance(decoded, Mapping):
            raise ValidationError("retention must be a JSON object.", field="retention")
        return RetentionPolicy.from_values(decoded, base=current)

    fields = (
        "regulatory_period_years",
        "regulatory_years",
        "logs_min_days",
        "logs_days",
        "raw_signal_months",
        "forecast_months",
        "notification_months",
        "notifications_months",
        "quarantine_days",
    )
    submitted = {field: _one(values, field) for field in fields if _one(values, field).strip()}
    return RetentionPolicy.from_values(submitted, base=current)


def _queue_job_trigger(
    session: Session | scoped_session[Session],
    runtime: object | None,
    name: str,
    values: Mapping[str, list[str]],
    principal: Principal,
    trigger_locks: dict[str, Lock],
) -> tuple[str, str]:
    if runtime is None:
        raise ExternalServiceError("The scheduler is not configured; the job was not started.")
    registry = getattr(runtime, "registry", None)
    runner = getattr(runtime, "runner", None)
    if (
        registry is None
        or runner is None
        or not callable(getattr(registry, "get", None))
        or not callable(getattr(runner, "submit", None))
    ):
        raise ExternalServiceError("The scheduler is not configured; the job was not started.")
    definition = registry.get(name)
    canonical_name = str(getattr(definition, "name", name))
    lock = trigger_locks.setdefault(canonical_name, Lock())
    with lock:
        db_session = session if isinstance(session, Session) else session()
        running = JobLedger(db_session).running_run(canonical_name)
        if running is not None:
            raise JobAlreadyRunningError(canonical_name, running)

        if canonical_name in {"retention.purge", "retention_purge"}:
            existing_purge = db_session.scalar(select(RetentionPurgeLog.id).limit(1))
            if existing_purge is not None:
                raise Conflict(
                    "A retention purge is already recorded and cannot be re-run "
                    "from the admin console."
                )

        retry_run_id = _optional(values, "retry_run_id") or _optional(values, "retry_of")
        trigger = "manual"
        attempt = 1
        if retry_run_id:
            try:
                failed_id = UUID(retry_run_id)
            except ValueError as error:
                raise ValidationError(
                    "retry_run_id must be a valid run identifier.", field="retry_run_id"
                ) from error
            failed = db_session.get(JobRun, failed_id)
            if failed is None or failed.job_name != canonical_name:
                raise NotFound("The failed job run was not found.")
            if failed.state != "failed":
                raise Conflict("Only a failed job run can be retried.")
            logical_run_id = failed.run_id
            attempt = failed.attempt + 1
            trigger = "retry"
        else:
            logical_run_id = new_job_run_id()

        runner.submit(
            canonical_name,
            trigger=trigger,
            run_id=logical_run_id,
            attempt=attempt,
            as_of=_optional(values, "as_of"),
            borrower_id=_optional(values, "borrower_id") or _optional(values, "borrower"),
            actor_id=principal.id,
        )
        return logical_run_id, trigger


def _constant_time_equal(left: str, right: str) -> bool:
    return bool(left) and hmac.compare_digest(left, right)


def create_admin_config_router(
    session: Session | scoped_session[Session],
    *,
    threshold_store: ThresholdStore | None = None,
    catalogue_service: CatalogueService | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the approval-gated threshold and catalogue admin surface."""

    if not is_database_session(session):
        raise TypeError("create_admin_config_router requires a SQLAlchemy Session.")
    if threshold_store is not None and not isinstance(threshold_store, ThresholdStore):
        raise TypeError("threshold_store must be a ThresholdStore.")
    if catalogue_service is not None and not isinstance(catalogue_service, CatalogueService):
        raise TypeError("catalogue_service must be a CatalogueService.")

    configured_store = threshold_store
    catalogue = catalogue_service or CatalogueService(cast(Session, session))
    router = APIRouter(tags=["admin-config-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    @router.get("/admin/config", response_class=HTMLResponse, name="admin_config")
    async def admin_config(
        request: Request,
        principal: Principal = _CONFIG_READ_DEP,
    ) -> HTMLResponse:
        return _render_config(
            request,
            fallback_environment,
            session,
            _request_threshold_store(session, configured_store),
            catalogue,
            principal=principal,
        )

    @router.post(
        "/admin/config/thresholds/preview",
        response_class=HTMLResponse,
        name="admin_config_threshold_preview",
    )
    async def admin_config_threshold_preview(
        request: Request,
        principal: Principal = _CONFIG_WRITE_DEP,
    ) -> Response:
        raw_values = await _form_values(request)
        form = _config_form(raw_values)
        try:
            patch = _parse_threshold_values(form.get("values", ""))
            note = _required_config_text(form.get("note"), "note")
            preview = _preview_threshold_patch(
                session,
                _request_threshold_store(session, configured_store),
                principal,
                patch,
                note,
            )
        except (DomainError, KeyError) as error:
            return _render_config_error(
                request,
                fallback_environment,
                session,
                _request_threshold_store(session, configured_store),
                catalogue,
                principal,
                form,
                error,
            )
        return _render_config(
            request,
            fallback_environment,
            session,
            _request_threshold_store(session, configured_store),
            catalogue,
            principal=principal,
            form=form,
            band_preview=preview,
        )

    @router.post(
        "/admin/thresholds",
        response_class=HTMLResponse,
        name="admin_threshold_propose",
    )
    async def admin_threshold_propose(
        request: Request,
        principal: Principal = _CONFIG_WRITE_DEP,
    ) -> Response:
        raw_values = await _form_values(request)
        form = _config_form(raw_values)
        try:
            patch = _parse_threshold_values(form.get("values", ""))
            note = _required_config_text(form.get("note"), "note")
            _request_threshold_store(session, configured_store).propose(patch, principal, note=note)
        except (DomainError, KeyError) as error:
            return _render_config_error(
                request,
                fallback_environment,
                session,
                _request_threshold_store(session, configured_store),
                catalogue,
                principal,
                form,
                error,
            )
        return RedirectResponse("/admin/config?threshold=pending", status_code=303)

    @router.post(
        "/admin/thresholds/proposals/{proposal_id}/approve",
        response_class=HTMLResponse,
        name="admin_threshold_approve",
    )
    async def admin_threshold_approve(
        request: Request,
        proposal_id: UUID,
        principal: Principal = _CONFIG_APPROVE_DEP,
    ) -> Response:
        await _form_values(request)
        try:
            _request_threshold_store(session, configured_store).approve(proposal_id, principal)
        except DomainError as error:
            return _render_config_error(
                request,
                fallback_environment,
                session,
                _request_threshold_store(session, configured_store),
                catalogue,
                principal,
                {},
                error,
            )
        return RedirectResponse("/admin/config?threshold=approved", status_code=303)

    @router.post(
        "/admin/config/catalogue",
        response_class=HTMLResponse,
        name="admin_config_catalogue_save",
    )
    async def admin_config_catalogue_save(
        request: Request,
        principal: Principal = _CONFIG_WRITE_DEP,
    ) -> Response:
        raw_values = await _form_values(request)
        form = _config_form(raw_values)
        try:
            catalogue.save(principal, _config_catalogue_values(form))
        except DomainError as error:
            return _render_config_error(
                request,
                fallback_environment,
                session,
                _request_threshold_store(session, configured_store),
                catalogue,
                principal,
                form,
                error,
            )
        return RedirectResponse("/admin/config?catalogue=pending", status_code=303)

    @router.post(
        "/admin/config/catalogue/{code}/retire",
        response_class=HTMLResponse,
        name="admin_config_catalogue_retire",
    )
    async def admin_config_catalogue_retire(
        request: Request,
        code: str,
        principal: Principal = _CONFIG_WRITE_DEP,
    ) -> Response:
        raw_values = await _form_values(request)
        form = _config_form(raw_values)
        try:
            catalogue.retire(
                principal,
                code,
                expected_version=_optional_config_int(form.get("expected_version")),
            )
        except DomainError as error:
            return _render_config_error(
                request,
                fallback_environment,
                session,
                _request_threshold_store(session, configured_store),
                catalogue,
                principal,
                form,
                error,
            )
        return RedirectResponse("/admin/config?catalogue=pending", status_code=303)

    @router.post(
        "/admin/config/catalogue/approvals/{request_id}",
        response_class=HTMLResponse,
        name="admin_config_catalogue_decide",
    )
    async def admin_config_catalogue_decide(
        request: Request,
        request_id: UUID,
        principal: Principal = _CONFIG_APPROVE_DEP,
    ) -> Response:
        raw_values = await _form_values(request)
        form = _config_form(raw_values)
        try:
            decision = form.get("decision")
            if decision not in {"approve", "reject"}:
                raise ValidationError("decision must be approve or reject.", field="decision")
            catalogue.decide_approval(
                principal,
                request_id,
                approved=decision == "approve",
                reason=form.get("reason"),
            )
        except DomainError as error:
            return _render_config_error(
                request,
                fallback_environment,
                session,
                _request_threshold_store(session, configured_store),
                catalogue,
                principal,
                form,
                error,
            )
        return RedirectResponse("/admin/config?catalogue=decided", status_code=303)

    return router


def _request_threshold_store(
    session: Session | scoped_session[Session],
    configured_store: ThresholdStore | None,
) -> ThresholdStore:
    """Bind a new store to the current request session when needed.

    The production application supplies a ``scoped_session``.  Constructing a
    SQLAlchemy repository at router-build time would bind it to the scope used
    during startup instead of the scope opened for a request, so the default
    store is deliberately resolved at request time.
    """

    if configured_store is not None:
        return configured_store
    concrete_session = session if isinstance(session, Session) else session()
    return ThresholdStore(
        repository=SqlAlchemyThresholdRepository(concrete_session),
        path=DEFAULT_THRESHOLD_PATH,
    )


def _render_config_error(
    request: Request,
    fallback_environment: Environment,
    session: Session | scoped_session[Session],
    store: ThresholdStore,
    catalogue: CatalogueService,
    principal: Principal,
    form: Mapping[str, str],
    error: DomainError | KeyError,
) -> HTMLResponse:
    message = error.message if isinstance(error, DomainError) else str(error)
    status_code = status_for_error(error) if isinstance(error, DomainError) else 422
    return _render_config(
        request,
        fallback_environment,
        session,
        store,
        catalogue,
        principal=principal,
        form=form,
        error=message,
        status_code=status_code,
    )


def _render_config(
    request: Request,
    fallback_environment: Environment,
    session: Session | scoped_session[Session],
    store: ThresholdStore,
    catalogue: CatalogueService,
    *,
    principal: Principal,
    form: Mapping[str, str] | None = None,
    error: str = "",
    status_code: int = 200,
    band_preview: BandChangePreview | None = None,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    # The first access persists the packaged default when the database has no
    # active snapshot.  The screen therefore always has an explicit baseline.
    store.values()
    view = load_admin_config_view(
        session,
        principal_id=principal.id,
        band_preview=band_preview,
    )
    template = environment.get_template("screens/admin/config/index.html")
    return HTMLResponse(
        template.render(
            request=request,
            principal=principal,
            view=view,
            can_propose=principal.has(Permission.PROPOSE_THRESHOLDS),
            can_approve=principal.has(Permission.APPROVE_THRESHOLDS),
            form=dict(form or {}),
            error=error,
            locale=request.cookies.get("covenant_radar_locale", "en"),
            theme=theme_for_request(request),
            text_direction="ltr",
            csrf_token=getattr(request.state, "csrf_token", ""),
        ),
        status_code=status_code,
    )


def _preview_threshold_patch(
    session: Session | scoped_session[Session],
    store: ThresholdStore,
    principal: Principal,
    patch: Mapping[str, object],
    note: str,
) -> BandChangePreview:
    """Validate a patch using the real store without leaving a proposal row."""

    savepoint = session.begin_nested()
    try:
        proposal = store.propose(patch, principal, note=note)
        return build_band_change_preview(
            session,
            before_values=proposal.before,
            after_values=proposal.after,
        )
    finally:
        savepoint.rollback()


def _parse_threshold_values(raw: str) -> Mapping[str, object]:
    text = raw.strip()
    if not text:
        raise ValidationError("A proposed value is required.", field="values")
    try:
        decoded = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as error:
        raise ValidationError(f"values must be valid JSON: {error.msg}.", field="values") from error
    except ValueError as error:
        raise ValidationError(str(error), field="values") from error
    if not isinstance(decoded, Mapping):
        raise ValidationError(
            "values must be a JSON object keyed by threshold name.", field="values"
        )
    return decoded


def _config_catalogue_values(values: Mapping[str, str]) -> dict[str, object]:
    raw_parameters = values.get("effect_parameters", "").strip()
    if not raw_parameters:
        raise ValidationError(
            "An effect model requires effect parameters.",
            field="intervention.effect_parameters",
        )
    try:
        parameters = json.loads(
            raw_parameters,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except json.JSONDecodeError as error:
        raise ValidationError(
            f"effect_parameters is not valid JSON: {error.msg}.",
            field="intervention.effect_parameters",
        ) from error
    except ValueError as error:
        raise ValidationError(str(error), field="intervention.effect_parameters") from error
    if not isinstance(parameters, Mapping):
        raise ValidationError(
            "effect_parameters must be a JSON object.",
            field="intervention.effect_parameters",
        )
    classes = tuple(
        item.strip()
        for item in values.get("applicable_covenant_classes", "").replace("\n", ",").split(",")
        if item.strip()
    )
    assumptions = tuple(
        item.strip() for item in values.get("assumptions", "").splitlines() if item.strip()
    )
    return {
        "id": values.get("id", values.get("code", "")),
        "role_tag": values.get("role_tag", ""),
        "text": values.get("text", ""),
        "effect_model": values.get("effect_model", ""),
        "effect_parameters": dict(parameters),
        "applicable_covenant_classes": classes,
        "assumptions": assumptions,
        "requires_approval": values.get("requires_approval") in {"1", "true", "on"},
        "is_active": True,
    }


def _config_form(values: Mapping[str, list[str]]) -> dict[str, str]:
    return {key: _one(values, key) for key in values}


def _required_config_text(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("A reason is required for this change.", field=field)
    return value.strip()


def _optional_config_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValidationError("expected_version must be an integer.", field="version") from error
    if parsed < 0:
        raise ValidationError("expected_version must be non-negative.", field="version")
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r} is not allowed.")
        result[key] = value
    return result


def _render_error_for_user(
    request: Request,
    fallback_environment: Environment,
    service: AdminUsersService,
    principal: Principal,
    user_id: UUID,
    form: Mapping[str, list[str]],
    error: DomainError,
) -> HTMLResponse:
    return _render(
        request,
        fallback_environment,
        service,
        principal=principal,
        selected_user_id=user_id,
        form=form,
        error=error.message,
        status_code=status_for_error(error),
    )


def _render(
    request: Request,
    fallback_environment: Environment,
    service: AdminUsersService,
    *,
    principal: Principal,
    selected_user_id: UUID | None = None,
    form: Mapping[str, list[str]] | None = None,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    selected = service.get_user(principal, selected_user_id) if selected_user_id else None
    sessions = service.list_sessions(principal, selected_user_id) if selected_user_id else ()
    current_session_id = _current_session_id(request)
    template = environment.get_template("screens/admin/users/index.html")
    users = service.list_users(principal)
    roles = service.list_roles(principal)
    portfolios = service.list_portfolios(principal)
    pending = service.pending_role_assignments(principal)
    values = {
        "request": request,
        "principal": principal,
        "users": users,
        "roles": roles,
        "portfolios": portfolios,
        "pending": pending,
        "selected": selected,
        "sessions": sessions,
        "current_session_id": current_session_id,
        "form": dict(form or {}),
        "error": error,
        "locale": request.cookies.get("covenant_radar_locale", "en"),
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "csrf_token": getattr(request.state, "csrf_token", ""),
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


async def _form_values(request: Request) -> dict[str, list[str]]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted form is too large.", field="form")
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError("The submitted form is not valid UTF-8.", field="form") from error
    parsed = parse_qs(decoded, keep_blank_values=True, strict_parsing=False)
    values: dict[str, list[str]] = {}
    for key, items in parsed.items():
        if len(key.encode("utf-8")) > _MAX_FIELD_BYTES or len(items) > 500:
            raise ValidationError("The submitted form is invalid.", field="form")
        for value in items:
            if len(value.encode("utf-8")) > _MAX_FIELD_BYTES:
                raise ValidationError("The submitted form is invalid.", field=key)
        if key != "csrf_token":
            values[key] = items
    return values


def _one(values: Mapping[str, list[str]], key: str, *, default: str = "") -> str:
    entries = values.get(key, [])
    return entries[-1] if entries else default


def _optional(values: Mapping[str, list[str]], key: str) -> str | None:
    value = _one(values, key).strip()
    return value or None


def _csv(
    values: Mapping[str, list[str]],
    key: str,
    *,
    fallback_key: str | None = None,
) -> tuple[str, ...]:
    raw = values.get(key, [])
    if not raw and fallback_key:
        raw = values.get(fallback_key, [])
    result: list[str] = []
    for item in raw:
        for value in item.replace("\n", ",").split(","):
            clean = value.strip()
            if clean and clean not in result:
                result.append(clean)
    return tuple(result)


def _scope_values(values: Mapping[str, list[str]]) -> tuple[dict[str, object], ...]:
    selected = values.get("scope", [])
    descendants = set(values.get("scope_descendants", []))
    return tuple(
        {"portfolio_id": portfolio_id, "include_descendants": portfolio_id in descendants}
        for portfolio_id in selected
    )


def _current_session_id(request: Request) -> UUID | None:
    session = getattr(request.state, "session", None)
    value = getattr(session, "id", None)
    return value if isinstance(value, UUID) else None


__all__ = [
    "create_admin_config_router",
    "create_admin_ops_router",
    "create_admin_users_router",
]
