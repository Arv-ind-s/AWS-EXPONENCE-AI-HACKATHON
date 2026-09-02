"""Production browser-application composition.

Feature routers are intentionally testable in isolation, but they still need
one composition root that gives them a request transaction, signed browser
sessions, RBAC, audit writes, and the configured service adapters.  This
module is that boundary; no screen owns its own database connection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from covenant_radar.ai import create_provider
from covenant_radar.ai.client import ModelClient, SqlAlchemyModelCallWriter
from covenant_radar.ai.intake import propose_candidates
from covenant_radar.ai.registry import ModelRegistryGuard
from covenant_radar.api.deps import (
    RequestPrincipalResolver,
    configure_audit_writer,
    configure_principal_resolver,
)
from covenant_radar.api.v1.routers import (
    create_audit_events_router,
    create_borrowers_router,
    create_covenant_tests_router,
    create_evidence_router,
    create_explain_router,
    create_facilities_router,
    create_forecast_router,
    create_ingest_router,
    create_memos_router,
    create_simulations_router,
)
from covenant_radar.api.v1.routers import create_cases_router as create_api_cases_router
from covenant_radar.api.v1.routers import create_covenants_router as create_api_covenants_router
from covenant_radar.asgi import create_app
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.config.settings import (
    Settings,
    SettingsError,
    get_settings,
    load_deployment_environment,
)
from covenant_radar.core.context import get_request_id
from covenant_radar.core.errors import ExternalServiceError
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.repositories.identity import SqlAlchemyIdentityStore
from covenant_radar.db.session import (
    DatabaseCircuitBreaker,
    create_database_engine,
    create_session_factory,
)
from covenant_radar.documents.store import FileSystemDocumentStore
from covenant_radar.domain.intake.candidates import ClauseCandidate
from covenant_radar.domain.intake.proposal import StageOneProposal
from covenant_radar.domain.memo.slots import MemoRecords
from covenant_radar.lifecycle import (
    ApplicationLifecycle,
    MaintenanceModeMiddleware,
    clock_skew_self_check,
    configuration_self_check,
    database_self_check,
    document_store_self_check,
    install_lifecycle,
    migrations_at_head_self_check,
    scheduler_self_check,
)
from covenant_radar.notifications.inapp import InAppNotificationService, InAppNotifier
from covenant_radar.ports.document_store import DocumentStore
from covenant_radar.security.crypto import FieldEncryptor, HMACFingerprinter
from covenant_radar.security.rbac import RolePermissionResolver
from covenant_radar.security.sessions import SessionManager, SessionSettings
from covenant_radar.services.admin_users import AdminUsersService
from covenant_radar.services.auth import AuthService
from covenant_radar.services.bulk import BulkService
from covenant_radar.services.cases import CaseService
from covenant_radar.services.catalogue import CatalogueService
from covenant_radar.services.certificates import CertificateService
from covenant_radar.services.dispositions import DispositionService
from covenant_radar.services.documents import DocumentService
from covenant_radar.services.export import ExportService, ExportStore
from covenant_radar.services.ingestion import SignalIngestionService
from covenant_radar.services.intake import IntakeService
from covenant_radar.services.master_data import MasterDataService
from covenant_radar.services.memo import MemoGenerationOutcome, MemoGenerationService
from covenant_radar.services.model_governance import SqlAlchemyModelRegistryRepository
from covenant_radar.services.nightly_runtime import NightlyRuntime, build_nightly_runtime
from covenant_radar.services.notifications import NotificationService
from covenant_radar.services.overrides import OverrideService
from covenant_radar.services.reconstruction import ReconstructionService
from covenant_radar.services.registry import RegistryService
from covenant_radar.services.simulation import SimulationService
from covenant_radar.services.statements import StatementImportService
from covenant_radar.services.views import ViewService
from covenant_radar.web.preferences import create_preferences_router
from covenant_radar.web.routes.admin import create_admin_users_router
from covenant_radar.web.routes.audit import create_audit_router
from covenant_radar.web.routes.auth import create_auth_router
from covenant_radar.web.routes.borrower import MemoGenerator, create_borrower_router
from covenant_radar.web.routes.bulk import create_bulk_router
from covenant_radar.web.routes.cases import create_cases_router
from covenant_radar.web.routes.catalogue import create_catalogue_router
from covenant_radar.web.routes.certificates import create_certificates_router
from covenant_radar.web.routes.covenants import create_covenants_router
from covenant_radar.web.routes.dispositions import create_dispositions_router
from covenant_radar.web.routes.documents import create_documents_router
from covenant_radar.web.routes.governance import create_governance_router
from covenant_radar.web.routes.intake import create_intake_router
from covenant_radar.web.routes.live import create_live_router
from covenant_radar.web.routes.master_data import create_master_data_router
from covenant_radar.web.routes.notifications import create_notifications_router
from covenant_radar.web.routes.overrides import create_overrides_router
from covenant_radar.web.routes.queue import create_queue_router
from covenant_radar.web.routes.search import create_search_router
from covenant_radar.web.routes.simulator import create_simulator_router
from covenant_radar.web.routes.statements import create_statements_router
from covenant_radar.web.routes.views import create_views_router
from covenant_radar.web.routes.why import create_why_router


def _deployment_environment(environ: Mapping[str, str] | None = None) -> str:
    """Return the environment the model-registry guard enforces against.

    Resolution lives with the rest of the environment loading so exported
    variables and `.env` have one consistent precedence rule. Any value other than the
    literal `"development"` — including an unset variable — collapses to the
    `PRODUCTION` default, so a misconfigured or forgotten setting can never
    accidentally relax the guard.
    """
    return load_deployment_environment(environ)


class DisabledDocumentStore(DocumentStore):
    """A deliberate degraded document adapter for ``documents.store = none``."""

    _message = "Document storage is not configured for this deployment."

    def put(
        self,
        content: bytes | bytearray | memoryview | BinaryIO,
        *,
        content_hash: str | None = None,
    ) -> str:
        raise ExternalServiceError(self._message, field="documents.storage")

    def get(self, storage_key: str) -> bytes:
        raise ExternalServiceError(self._message, field="documents.storage")

    def delete(self, storage_key: str) -> None:
        raise ExternalServiceError(self._message, field="documents.storage")

    def stream(self, storage_key: str, *, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
        raise ExternalServiceError(self._message, field="documents.storage")


class RequestAuditRecorder:
    """Make audit provenance follow the live request rather than startup."""

    def __init__(self, session: scoped_session[Session]) -> None:
        self._store = AuditRepository(session)

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        current_request_id = get_request_id() or request_id
        return AuditRecorder(self._store, request_id=current_request_id).record(
            event_type,
            subject,  # type: ignore[arg-type]
            payload,
            actor=actor,
            request_id=current_request_id,
        )


class RequestSessionMiddleware(BaseHTTPMiddleware):
    """Open exactly one SQLAlchemy session and transaction per HTTP request."""

    def __init__(
        self,
        app: Any,
        *,
        sessions: scoped_session[Session],
        request_scope: ContextVar[str | None],
    ) -> None:
        super().__init__(app)
        self.sessions = sessions
        self.request_scope = request_scope

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        token = self.request_scope.set(uuid4().hex)
        try:
            response = await call_next(request)
            if self.sessions.registry.has():
                self.sessions.commit()
            return response
        except Exception:
            if self.sessions.registry.has():
                self.sessions.rollback()
            raise
        finally:
            self.sessions.remove()
            self.request_scope.reset(token)


@dataclass(frozen=True, slots=True)
class ProductionDependencies:
    """The application-owned long-lived adapters, exposed for diagnostics."""

    sessions: scoped_session[Session]
    identity: SqlAlchemyIdentityStore
    audit: RequestAuditRecorder
    roles: RolePermissionResolver
    nightly: NightlyRuntime


def create_production_app(settings: Settings | None = None) -> FastAPI:
    """Create the complete browser application served by ``radarctl serve``.

    The factory refuses to start without a session signing secret.  A browser
    UI that silently falls back to an unsigned development identity would
    violate the product's access boundary, so local operators must supply a
    32-byte secret through the documented environment setting.
    """

    resolved = settings or get_settings()
    secret = resolved.security.session_secret
    if secret is None:
        raise SettingsError(
            "COVENANT_RADAR_SECURITY_SESSION_SECRET is required to serve the browser UI."
        )

    request_scope: ContextVar[str | None] = ContextVar("covenant_radar_request_session")
    engine = create_database_engine(resolved.database)
    circuit_breaker = DatabaseCircuitBreaker()
    session_factory = create_session_factory(engine)
    sessions = scoped_session(
        session_factory,
        scopefunc=request_scope.get,
    )

    def role_permissions() -> dict[str, tuple[str, ...]]:
        with session_factory() as startup_session:
            return SqlAlchemyIdentityStore(startup_session).permissions_by_role()

    def theme_resolver(principal: object) -> str | None:
        principal_id = getattr(principal, "id", None)
        if not isinstance(principal_id, UUID):
            return None
        user = sessions.get(AppUser, principal_id)
        return user.theme if user is not None and user.is_active else None

    identity = SqlAlchemyIdentityStore(sessions)
    audit = RequestAuditRecorder(sessions)
    roles = RolePermissionResolver(identity)
    session_manager = SessionManager(
        identity,
        settings=SessionSettings(
            cookie_name=resolved.security.session_cookie_name,
            secret=secret.get_secret_value(),
            # The default configuration listens only on loopback.  Deployments
            # behind TLS should set a non-loopback host and receive Secure
            # cookies automatically.
            secure_cookie=not _is_local_host(resolved.web.host),
        ),
        audit=audit,
    )
    auth = AuthService(identity, session_manager, audit=audit)
    admin_users = AdminUsersService(
        sessions,
        audit=audit,
        passwords=auth.passwords,
        roles=roles,
    )
    registry = RegistryService(sessions, audit=audit)
    document_store = _document_store(resolved)
    document_service = DocumentService(sessions, store=document_store, audit=audit)
    intake = IntakeService(sessions, audit=audit, registry=registry)
    catalogue = CatalogueService(sessions, audit=audit)
    simulation = SimulationService(sessions, audit=audit)
    reconstruction = ReconstructionService(
        sessions,
        document_store=document_store,
        audit=audit,
    )
    master_data = MasterDataService(sessions, audit=audit)
    overrides = OverrideService(sessions, audit=audit)
    notification_delivery = NotificationService(
        sessions,
        notifier=InAppNotifier(),
        audit=audit,
    )
    inapp_notifications = InAppNotificationService(sessions, audit=audit)
    cases = CaseService(sessions, audit=audit)
    certificates = CertificateService(sessions, audit=audit)
    dispositions = DispositionService(sessions, audit=audit)
    statement_imports = StatementImportService(sessions, audit=audit)
    views = ViewService(sessions, audit=audit)
    bulk = BulkService(sessions, audit=audit)
    # Bulk and export share one request session because `create_bulk_router`
    # refuses a split transaction: a bulk action and the export it produces
    # must commit or roll back together.
    exports = ExportService(
        sessions,
        store=_export_store(resolved, document_store),
        audit=audit,
    )
    signal_ingestion = SignalIngestionService(sessions, audit=audit)
    # Derived from the session secret rather than stored separately: the API's
    # pagination cursors need a stable signing key across restarts and
    # workers, and inventing a second secret to configure would be one more
    # thing an operator can forget and silently break paging with.
    api_cursor_secret = sha256(
        b"covenant-radar/api-cursor/v1:" + secret.get_secret_value().encode("utf-8")
    ).digest()

    ai_provider = None
    proposal_generator: Callable[[Sequence[ClauseCandidate]], Sequence[StageOneProposal]] | None = (
        None
    )
    memo_generator: MemoGenerator | None = None
    if resolved.ai.provider != "none":
        ai_provider = create_provider(resolved.ai)
        model_client = ModelClient(
            ai_provider,
            model=resolved.ai.model or "covenant-radar",
            model_calls=SqlAlchemyModelCallWriter(sessions),
            registry_guard=ModelRegistryGuard(
                SqlAlchemyModelRegistryRepository(sessions),
                environment=_deployment_environment(),
            ),
        )

        def proposal_generator(
            candidates: Sequence[ClauseCandidate],
        ) -> Sequence[StageOneProposal]:
            return propose_candidates(
                candidates,
                model_client,
                request_id=get_request_id(),
            )

        def memo_generator(
            *,
            borrower_id: UUID,
            records: MemoRecords,
            run_id: UUID | None,
            case_id: UUID | None,
            actor_id: UUID | None,
        ) -> MemoGenerationOutcome:
            # Built per request: the service stamps its trace rows with the
            # request id it is constructed with, so a long-lived instance
            # would attribute every memo to the first request it ever saw.
            #
            # `sessions()` resolves the request's real Session. Handing the
            # `scoped_session` proxy straight over does not work:
            # `MemoGenerationService` requires an actual `Session`, and the
            # proxy is not an instance of one, so it is refused at
            # construction.
            service = MemoGenerationService(
                sessions(),
                client=model_client,
                audit=audit,
                request_id=get_request_id(),
            )
            return service.generate(
                borrower_id=borrower_id,
                records=records,
                catalogue=catalogue,
                run_id=run_id,
                case_id=case_id,
                actor_id=actor_id,
            )

    nightly = build_nightly_runtime(session_factory, resolved)

    routers = (
        create_auth_router(auth),
        create_admin_users_router(admin_users),
        create_queue_router(sessions),
        create_search_router(
            cast(Session, sessions),
            audit_writer=audit,
            fingerprinter=_optional_fingerprinter(),
        ),
        # Static master-data creation paths must precede the generic case-file
        # route at `/borrowers/{reference}` or `/borrowers/new` is treated as
        # a borrower reference and returns a 404.
        create_master_data_router(master_data, borrower_create_only=True),
        create_borrower_router(sessions, memo_generator=memo_generator),
        create_why_router(sessions),
        create_simulator_router(
            sessions,
            simulation_service=simulation,
            catalogue_service=catalogue,
            audit_writer=audit,
        ),
        create_intake_router(
            intake,
            document_service,
            proposal_generator=proposal_generator,
        ),
        create_audit_router(sessions, reconstruction_service=reconstruction, audit_writer=audit),
        create_documents_router(document_service),
        create_covenants_router(registry),
        create_catalogue_router(catalogue),
        create_governance_router(sessions),
        create_master_data_router(master_data),
        create_overrides_router(overrides),
        create_preferences_router(sessions),
        create_notifications_router(sessions, service=inapp_notifications),
        create_live_router(
            sessions,
            notifications=inapp_notifications,
            cursor_secret=api_cursor_secret,
        ),
        create_cases_router(sessions, case_service=cases, audit_writer=audit),
        create_certificates_router(certificates),
        create_dispositions_router(dispositions),
        create_statements_router(statement_imports),
        create_views_router(sessions, service=views),
        create_bulk_router(sessions, bulk_service=bulk, export_service=exports, audit_writer=audit),
        # The documented public REST surface (`spec §R-32`, pillar S5).  Every
        # router already carries its own `/api/v1` prefix.  Pagination cursors
        # are signed with the deployment's session secret so a link survives a
        # restart and stays valid across workers, rather than falling back to
        # `api/pagination.py`'s per-process key.
        create_borrowers_router(master_data),
        create_facilities_router(master_data),
        create_api_covenants_router(registry),
        create_api_cases_router(sessions, cursor_secret=api_cursor_secret),
        create_covenant_tests_router(sessions, cursor_secret=api_cursor_secret),
        create_evidence_router(sessions, cursor_secret=api_cursor_secret),
        create_forecast_router(sessions, cursor_secret=api_cursor_secret),
        create_memos_router(sessions, cursor_secret=api_cursor_secret),
        create_simulations_router(sessions, cursor_secret=api_cursor_secret),
        create_audit_events_router(sessions, cursor_secret=api_cursor_secret),
        create_explain_router(sessions),
        create_ingest_router(signal_ingestion, statements=statement_imports),
    )
    app = create_app(
        resolved,
        routers=routers,
        principal_resolver=RequestPrincipalResolver(sessions=session_manager, roles=roles),
        audit_writer=audit,
        role_permissions=role_permissions,
        theme_resolver=theme_resolver,
    )
    app.add_middleware(RequestSessionMiddleware, sessions=sessions, request_scope=request_scope)
    # Added after `RequestSessionMiddleware` so it is the outermost guard: a
    # request arriving while the database circuit is open never reaches
    # session handling at all, and instead gets an immediate maintenance
    # response (`spec §N-06.b`) rather than queuing behind its own timeout.
    app.add_middleware(MaintenanceModeMiddleware, circuit_breaker=circuit_breaker)
    configure_principal_resolver(
        app,
        RequestPrincipalResolver(sessions=session_manager, roles=roles),
    )
    configure_audit_writer(app, audit)
    app.state.production_dependencies = ProductionDependencies(
        sessions, identity, audit, roles, nightly
    )
    app.state.ai_provider = ai_provider
    app.state.document_store = document_store
    app.state.notification_service = inapp_notifications
    app.state.notification_delivery_service = notification_delivery
    app.state.nightly_runtime = nightly
    app.state.database_engine = engine
    app.state.database_circuit_breaker = circuit_breaker
    lifecycle = _build_lifecycle(resolved, engine, circuit_breaker, document_store, nightly)
    install_lifecycle(app, lifecycle)
    return app


def _build_lifecycle(
    settings: Settings,
    engine: Engine,
    circuit_breaker: DatabaseCircuitBreaker,
    document_store: DocumentStore,
    nightly: NightlyRuntime,
) -> ApplicationLifecycle:
    """The startup self-checks and live scheduler every production process
    ties to its ASGI lifespan (`spec §N-06.b`)."""
    checks = (
        configuration_self_check(settings),
        migrations_at_head_self_check(settings.database.url),
        database_self_check(engine, circuit_breaker=circuit_breaker),
        document_store_self_check(document_store),
        scheduler_self_check(nightly.registry),
        clock_skew_self_check(engine),
    )
    return ApplicationLifecycle(checks=checks, scheduler=nightly.scheduler)


def _is_local_host(host: str) -> bool:
    return host.lower() in {"127.0.0.1", "localhost", "::1"}


def _optional_fingerprinter() -> HMACFingerprinter | None:
    """Load the CIN lookup key when configured without weakening startup.

    Search never falls back to plaintext matching.  A deployment that has
    not provisioned the field-crypto secrets therefore gets public search
    immediately, while personal-class lookup remains unavailable until the
    same secrets used by master-data writes are supplied.
    """
    try:
        return HMACFingerprinter.from_environment()
    except (RuntimeError, ValueError):
        return None


def _document_store(settings: Settings) -> DocumentStore:
    """Select the configured byte store once at the composition boundary."""

    if settings.documents.store == "none":
        return DisabledDocumentStore()
    if settings.documents.store == "local":
        if settings.documents.local_path is None:  # validated settings invariant
            raise SettingsError("documents.local_path is required when documents.store=local.")
        try:
            encryptor = FieldEncryptor.from_environment()
        except Exception as error:
            raise SettingsError(
                "Encrypted local documents require field-encryption and CIN fingerprint "
                "secrets in the environment or keyring."
            ) from error
        return FileSystemDocumentStore(settings.documents.local_path, encryptor=encryptor)
    raise SettingsError("documents.store=s3 is not available in this build; choose none or local.")


def _export_store(settings: Settings, document_store: DocumentStore) -> ExportStore:
    """Select the byte store completed exports are written to.

    Exports are encrypted at rest on the same terms as documents and follow
    the same configuration switch, but they live under their own directory so
    a retention purge over exports can never reach a source document.  When
    document storage is disabled the disabled adapter is returned unchanged:
    it satisfies the `ExportStore` protocol and refuses at call time with the
    configuration error, rather than letting the application start with an
    export path that silently drops bytes.
    """

    if settings.documents.store != "local":
        return cast(ExportStore, document_store)
    if settings.documents.local_path is None:  # validated settings invariant
        raise SettingsError("documents.local_path is required when documents.store=local.")
    try:
        encryptor = FieldEncryptor.from_environment()
    except Exception as error:
        raise SettingsError(
            "Encrypted local exports require field-encryption and CIN fingerprint "
            "secrets in the environment or keyring."
        ) from error
    return FileSystemDocumentStore(settings.documents.local_path / "exports", encryptor=encryptor)


__all__ = ["ProductionDependencies", "create_production_app"]
