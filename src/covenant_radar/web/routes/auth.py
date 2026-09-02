"""Browser routes for the local authentication flow.

The router is intentionally constructed with an already configured
``AuthService``.  Application startup owns dependency wiring and transaction
boundaries; this module only parses bounded form input, renders escaped
templates and applies the session cookie attributes supplied by the session
service.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from covenant_radar.api.deps import public
from covenant_radar.core.errors import DomainError, ValidationError
from covenant_radar.security.oidc import OIDCClient, OIDCIdentity
from covenant_radar.security.provisioning import (
    ProviderUnavailable,
    ProvisioningService,
    SSOError,
)
from covenant_radar.security.saml import SAMLIdentity, SAMLServiceProvider
from covenant_radar.services.auth import (
    GENERIC_AUTHENTICATION_MESSAGE,
    GENERIC_MFA_MESSAGE,
    AuthResult,
    AuthService,
    AuthStatus,
)
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_DIRECTORY = Path(__file__).resolve().parents[1] / "templates"
CHALLENGE_COOKIE_NAME = "covenant_radar_auth_challenge"
OIDC_STATE_COOKIE_NAME = "covenant_radar_oidc_state"
SAML_STATE_COOKIE_NAME = "covenant_radar_saml_state"
_MAX_FORM_BYTES = 16 * 1024
_MAX_FIELD_BYTES = 4096
_MAX_SSO_FORM_BYTES = 4 * 1024 * 1024
_MAX_SSO_FIELD_BYTES = 4 * 1024 * 1024


def create_auth_router(
    service: AuthService,
    *,
    template_directory: Path | str = _TEMPLATE_DIRECTORY,
    oidc: OIDCClient | None = None,
    saml: SAMLServiceProvider | None = None,
    provisioning: ProvisioningService | None = None,
) -> APIRouter:
    """Build local authentication routes and optional configured SSO routes.

    SSO is opt-in at router construction time.  The local route remains
    independent, which is important for break-glass access when an external
    identity provider is down.
    """
    # Authentication transitions are intentionally public routes.  They are
    # still protected by CSRF and signed challenge/session cookies where
    # applicable; a permission dependency here would make sign-in circular.
    router = APIRouter(dependencies=[Depends(public)])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    def render(
        request: Request,
        template_name: str,
        *,
        status_code: int = 200,
        **context: object,
    ) -> HTMLResponse:
        environment = getattr(request.app.state, "template_env", fallback_environment)
        template = environment.get_template(f"screens/auth/{template_name}")
        locale = request.cookies.get("covenant_radar_locale", "en").lower()
        if locale not in {"en", "hi"}:
            locale = "en"
        values: dict[str, object] = {
            "request": request,
            "principal": None,
            "theme": theme_for_request(request),
            "locale": locale,
            "text_direction": "ltr",
            "error": "",
            **context,
        }
        return HTMLResponse(
            template.render(**values),
            status_code=status_code,
        )

    @router.get("/sign-in", response_class=HTMLResponse, name="sign_in")
    async def sign_in_page(request: Request, next: str = "/", username: str = "") -> HTMLResponse:
        return render(
            request,
            "sign_in.html",
            next=_safe_destination(next),
            username=username[:64],
        )

    @router.post("/sign-in", response_class=HTMLResponse, name="sign_in_submit")
    async def sign_in_submit(request: Request) -> Response:
        try:
            values = await _read_form(request)
        except ValidationError as error:
            return render(
                request,
                "sign_in.html",
                status_code=400,
                next="/",
                username="",
                error=error.message,
            )
        destination = _safe_destination(values.get("next", "/"))
        username = values.get("username", "")
        password = values.get("password", "")
        try:
            result = service.sign_in(
                username,
                password,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
        except DomainError:
            # Authentication failures are intentionally not differentiated in
            # the browser, even if a configured audit/persistence boundary
            # reports a deliberate domain refusal.
            result = AuthResult(status=AuthStatus.FAILED, message=GENERIC_AUTHENTICATION_MESSAGE)

        if result.status is AuthStatus.FAILED:
            return render(
                request,
                "sign_in.html",
                status_code=401,
                next=destination,
                username=username,
                error=result.message or GENERIC_AUTHENTICATION_MESSAGE,
            )
        if result.status is AuthStatus.MFA_ENROLLMENT_REQUIRED:
            response = RedirectResponse(_with_next("/mfa/enrol", destination), status_code=303)
            _set_challenge_cookie(response, service, result.challenge_cookie)
            return response
        if result.status is AuthStatus.MFA_REQUIRED:
            response = RedirectResponse(_with_next("/mfa/verify", destination), status_code=303)
            _set_challenge_cookie(response, service, result.challenge_cookie)
            return response
        if result.status is AuthStatus.PASSWORD_CHANGE_REQUIRED:
            response = RedirectResponse(
                _with_next("/password/change", destination), status_code=303
            )
            _set_challenge_cookie(response, service, result.challenge_cookie)
            return response
        if result.session is None:  # pragma: no cover - AuthService invariant
            return render(
                request,
                "sign_in.html",
                status_code=500,
                next=destination,
                username=username,
                error="Authentication could not be completed.",
            )
        response = RedirectResponse(destination, status_code=303)
        _set_session_cookie(response, service, result)
        _delete_challenge_cookie(response, service)
        return response

    @router.post("/sign-out", name="sign_out")
    async def sign_out(request: Request) -> Response:
        service.sign_out(request.cookies.get(service.sessions.settings.cookie_name))
        response = RedirectResponse("/sign-in", status_code=303)
        _delete_session_cookie(response, service)
        _delete_challenge_cookie(response, service)
        return response

    @router.get("/sso/oidc/start", name="oidc_start")
    async def oidc_start(request: Request, next: str = "/") -> Response:
        if oidc is None:
            return _sso_unavailable_response(
                request,
                render,
                provider="OIDC",
                destination=next,
            )
        try:
            authorization = await oidc.begin_authorization(_safe_destination(next))
        except ProviderUnavailable:
            return _sso_unavailable_response(
                request,
                render,
                provider="OIDC",
                destination=next,
            )
        except SSOError:
            return _sso_failure_response(request, render, destination=next)
        response = RedirectResponse(authorization.url, status_code=303)
        _set_provider_state_cookie(response, service, OIDC_STATE_COOKIE_NAME, authorization.state)
        return response

    @router.get("/sso/oidc/callback", name="oidc_callback")
    async def oidc_callback(request: Request) -> Response:
        if oidc is None:
            return _sso_unavailable_response(
                request,
                render,
                provider="OIDC",
                destination="/",
            )
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        try:
            identity = await oidc.complete_callback(
                code,
                state,
                expected_state=request.cookies.get(OIDC_STATE_COOKIE_NAME) or "",
            )
            return _issue_sso_session(
                request,
                service,
                identity,
                provisioning,
            )
        except ProviderUnavailable:
            return _sso_unavailable_response(
                request,
                render,
                provider="OIDC",
                destination="/",
            )
        except SSOError:
            return _sso_failure_response(request, render, destination="/")
        except DomainError as error:
            _audit_sso_route_failure(service, "oidc", error.code)
            return _sso_failure_response(request, render, destination="/")

    @router.get("/sso/saml/start", name="saml_start")
    async def saml_start(request: Request, next: str = "/") -> Response:
        if saml is None:
            return _sso_unavailable_response(
                request,
                render,
                provider="SAML",
                destination=next,
            )
        try:
            authorization = saml.begin_authorization(_safe_destination(next))
        except ProviderUnavailable:
            return _sso_unavailable_response(
                request,
                render,
                provider="SAML",
                destination=next,
            )
        except SSOError:
            return _sso_failure_response(request, render, destination=next)
        response = RedirectResponse(authorization.url, status_code=303)
        _set_provider_state_cookie(
            response, service, SAML_STATE_COOKIE_NAME, authorization.relay_state
        )
        return response

    @router.post("/sso/saml/acs", name="saml_acs")
    async def saml_acs(request: Request) -> Response:
        if saml is None:
            return _sso_unavailable_response(
                request,
                render,
                provider="SAML",
                destination="/",
            )
        try:
            values = await _read_form(
                request,
                max_bytes=_MAX_SSO_FORM_BYTES,
                max_field_bytes=_MAX_SSO_FIELD_BYTES,
            )
            relay_state = values.get("RelayState", "")
            identity = saml.complete_callback(
                values.get("SAMLResponse", ""),
                relay_state,
                expected_relay_state=request.cookies.get(SAML_STATE_COOKIE_NAME) or "",
            )
            return _issue_sso_session(request, service, identity, provisioning)
        except ValidationError as error:
            return render(
                request,
                "sign_in.html",
                status_code=400,
                next="/",
                username="",
                error=error.message,
            )
        except ProviderUnavailable:
            return _sso_unavailable_response(
                request,
                render,
                provider="SAML",
                destination="/",
            )
        except SSOError:
            return _sso_failure_response(request, render, destination="/")
        except DomainError as error:
            _audit_sso_route_failure(service, "saml", error.code)
            return _sso_failure_response(request, render, destination="/")

    @router.get("/password/change", response_class=HTMLResponse, name="change_password")
    async def change_password_page(request: Request, next: str = "/") -> Response:
        credential = _credential_from_request(request, service)
        if not _password_credential_is_live(credential, service):
            return _sign_in_redirect_response(request, destination=_request_destination(request))
        return render(request, "change_password.html", next=_safe_destination(next))

    @router.post("/password/change", response_class=HTMLResponse, name="change_password_submit")
    async def change_password_submit(request: Request) -> Response:
        try:
            values = await _read_form(request)
        except ValidationError as error:
            return render(
                request,
                "change_password.html",
                status_code=400,
                next="/",
                error=error.message,
            )
        destination = _safe_destination(values.get("next", "/"))
        credential = _credential_from_request(request, service)
        if not _password_credential_is_live(credential, service):
            return RedirectResponse(sign_in_redirect_url(destination), status_code=303)
        try:
            result = service.change_password(
                credential,
                values.get("new_password", ""),
                values.get("confirmation", ""),
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
        except ValidationError as error:
            return render(
                request,
                "change_password.html",
                status_code=400,
                next=destination,
                error=error.message,
            )
        if not result.authenticated:
            return _sign_in_redirect_response(request, destination=destination)
        response = RedirectResponse(destination, status_code=303)
        _set_session_cookie(response, service, result)
        _delete_challenge_cookie(response, service)
        return response

    @router.get("/mfa/enrol", response_class=HTMLResponse, name="mfa_enrol")
    async def mfa_enrol_page(request: Request, next: str = "/") -> Response:
        challenge_cookie = request.cookies.get(CHALLENGE_COOKIE_NAME)
        try:
            enrollment = service.begin_mfa_enrollment(challenge_cookie or "")
        except DomainError:
            return _sign_in_redirect_response(request, destination=_request_destination(request))
        response = render(
            request,
            "mfa_enrol.html",
            next=_safe_destination(next),
            secret=enrollment.enrollment.secret,
            provisioning_uri=enrollment.enrollment.provisioning_uri,
        )
        _set_challenge_cookie(response, service, enrollment.challenge_cookie)
        return response

    @router.post("/mfa/enrol", response_class=HTMLResponse, name="mfa_enrol_submit")
    async def mfa_enrol_submit(request: Request) -> Response:
        try:
            values = await _read_form(request)
        except ValidationError as error:
            return render(
                request,
                "mfa_enrol.html",
                status_code=400,
                next="/",
                error=error.message,
            )
        destination = _safe_destination(values.get("next", "/"))
        challenge_cookie = request.cookies.get(CHALLENGE_COOKIE_NAME, "")
        result = service.complete_mfa_enrollment(
            challenge_cookie,
            values.get("code", ""),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        if result.status is AuthStatus.FAILED:
            try:
                replacement = service.begin_mfa_enrollment(challenge_cookie)
            except DomainError:
                return _sign_in_redirect_response(request, destination=destination)
            render_response = render(
                request,
                "mfa_enrol.html",
                status_code=401,
                next=destination,
                secret=replacement.enrollment.secret,
                provisioning_uri=replacement.enrollment.provisioning_uri,
                error=result.message or GENERIC_MFA_MESSAGE,
            )
            _set_challenge_cookie(render_response, service, replacement.challenge_cookie)
            return render_response
        if result.status is AuthStatus.PASSWORD_CHANGE_REQUIRED:
            redirect_response = RedirectResponse(
                _with_next("/password/change", destination), status_code=303
            )
            _set_challenge_cookie(redirect_response, service, result.challenge_cookie)
            return redirect_response
        if result.session is None:  # pragma: no cover - AuthService invariant
            return _sign_in_redirect_response(request, destination=destination)
        redirect_response = RedirectResponse(destination, status_code=303)
        _set_session_cookie(redirect_response, service, result)
        _delete_challenge_cookie(redirect_response, service)
        return redirect_response

    @router.get("/mfa/verify", response_class=HTMLResponse, name="mfa_verify")
    async def mfa_verify_page(request: Request, next: str = "/") -> Response:
        challenge_cookie = request.cookies.get(CHALLENGE_COOKIE_NAME, "")
        if service.sessions.read_challenge(challenge_cookie, purpose="mfa") is None:
            return _sign_in_redirect_response(request, destination=_request_destination(request))
        return render(request, "mfa_verify.html", next=_safe_destination(next))

    @router.post("/mfa/verify", response_class=HTMLResponse, name="mfa_verify_submit")
    async def mfa_verify_submit(request: Request) -> Response:
        try:
            values = await _read_form(request)
        except ValidationError as error:
            return render(
                request,
                "mfa_verify.html",
                status_code=400,
                next="/",
                error=error.message,
            )
        destination = _safe_destination(values.get("next", "/"))
        result = service.verify_mfa(
            request.cookies.get(CHALLENGE_COOKIE_NAME, ""),
            values.get("code", ""),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        if result.status is AuthStatus.FAILED:
            return render(
                request,
                "mfa_verify.html",
                status_code=401,
                next=destination,
                error=result.message or GENERIC_MFA_MESSAGE,
            )
        if result.status is AuthStatus.PASSWORD_CHANGE_REQUIRED:
            response = RedirectResponse(
                _with_next("/password/change", destination), status_code=303
            )
            _set_challenge_cookie(response, service, result.challenge_cookie)
            return response
        if result.session is None:  # pragma: no cover - AuthService invariant
            return _sign_in_redirect_response(request, destination=destination)
        response = RedirectResponse(destination, status_code=303)
        _set_session_cookie(response, service, result)
        _delete_challenge_cookie(response, service)
        return response

    return router


def sign_in_redirect_url(
    destination: str,
    *,
    form_data: Mapping[str, object] | None = None,
) -> str:
    """Build a safe sign-in URL after a session timeout.

    Only non-secret, explicitly allow-listed form fields survive the redirect;
    passwords and arbitrary submitted fields never enter a URL.
    """
    params: dict[str, str] = {"next": _safe_destination(destination)}
    if form_data is not None:
        username = form_data.get("username")
        if isinstance(username, str) and len(username) <= 64:
            params["username"] = username
    return "/sign-in?" + urlencode(params)


def _sso_unavailable_response(
    request: Request,
    render: Callable[..., HTMLResponse],
    *,
    provider: str,
    destination: str,
) -> HTMLResponse:
    """Render an actionable provider outage without hiding local sign-in."""
    return render(
        request,
        "sign_in.html",
        status_code=503,
        next=_safe_destination(destination),
        username="",
        error=f"{provider} sign-in is unavailable. Local sign-in remains available.",
    )


def _sso_failure_response(
    request: Request, render: Callable[..., HTMLResponse], *, destination: str
) -> HTMLResponse:
    """Render the same generic message for all untrusted SSO failures."""
    return render(
        request,
        "sign_in.html",
        status_code=401,
        next=_safe_destination(destination),
        username="",
        error="Single sign-on could not be completed.",
    )


def _issue_sso_session(
    request: Request,
    service: AuthService,
    identity: OIDCIdentity | SAMLIdentity,
    provisioning: ProvisioningService | None,
) -> RedirectResponse:
    if provisioning is None:
        raise SSOError("provisioning_not_configured")
    provisioned = provisioning.provision(identity.claims, source=identity.provider)
    issued = service.sessions.issue(
        provisioned.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    service.audit.record(
        "authentication_sso_session_issued",
        ("app_user", provisioned.id),
        {"source": identity.provider.value},
        actor=provisioned.id,
        request_id=service.request_id,
    )
    response = RedirectResponse(identity.redirect_destination, status_code=303)
    response.set_cookie(value=issued.cookie, **service.sessions.cookie_attributes())
    _delete_provider_state_cookies(response, service)
    return response


def _audit_sso_route_failure(service: AuthService, provider: str, reason: str) -> None:
    service.audit.record(
        "authentication_sso_failed",
        ("authentication_provider", provider),
        {"outcome": "failed", "reason": reason},
        actor=None,
        request_id=service.request_id,
    )


def _set_provider_state_cookie(
    response: Response, service: AuthService, name: str, value: str
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        httponly=True,
        secure=service.sessions.settings.secure_cookie,
        samesite=service.sessions.settings.same_site,
        path=service.sessions.settings.cookie_path,
        max_age=max(1, int(service.sessions.settings.challenge_timeout.total_seconds())),
    )


def _delete_provider_state_cookies(response: Response, service: AuthService) -> None:
    for name in (OIDC_STATE_COOKIE_NAME, SAML_STATE_COOKIE_NAME):
        response.delete_cookie(key=name, path=service.sessions.settings.cookie_path)


def _sign_in_redirect_response(request: Request, *, destination: str) -> RedirectResponse:
    return RedirectResponse(
        sign_in_redirect_url(
            destination,
            form_data={"username": request.query_params.get("username", "")},
        ),
        status_code=303,
    )


def _request_destination(request: Request) -> str:
    """Return the current relative URL for a timeout redirect."""
    path = request.url.path
    query = request.url.query
    return f"{path}?{query}" if query else path


def _set_session_cookie(response: Response, service: AuthService, result: AuthResult) -> None:
    if result.session is None:
        return
    response.set_cookie(value=result.session.cookie, **service.sessions.cookie_attributes())


def _set_challenge_cookie(
    response: Response, service: AuthService, challenge_cookie: str | None
) -> None:
    if challenge_cookie is None:
        return
    response.set_cookie(
        value=challenge_cookie,
        **service.sessions.challenge_cookie_attributes(CHALLENGE_COOKIE_NAME),
    )


def _delete_session_cookie(response: Response, service: AuthService) -> None:
    response.delete_cookie(
        key=service.sessions.settings.cookie_name,
        path=service.sessions.settings.cookie_path,
    )


def _delete_challenge_cookie(response: Response, service: AuthService) -> None:
    response.delete_cookie(key=CHALLENGE_COOKIE_NAME, path=service.sessions.settings.cookie_path)


def _credential_from_request(request: Request, service: AuthService) -> str:
    return request.cookies.get(CHALLENGE_COOKIE_NAME) or request.cookies.get(
        service.sessions.settings.cookie_name, ""
    )


def _password_credential_is_live(credential: str, service: AuthService) -> bool:
    if service.sessions.validate(credential) is not None:
        return True
    return service.sessions.read_challenge(credential, purpose="password_change") is not None


async def _read_form(
    request: Request,
    *,
    max_bytes: int = _MAX_FORM_BYTES,
    max_field_bytes: int = _MAX_FIELD_BYTES,
) -> dict[str, str]:
    body = await request.body()
    if len(body) > max_bytes:
        raise ValidationError("The submitted form is too large.")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValidationError("The submitted form is invalid.") from error
    values: dict[str, str] = {}
    for key, items in parsed.items():
        if len(items) != 1 or len(key.encode("utf-8")) > max_field_bytes:
            raise ValidationError("The submitted form is invalid.")
        value = items[0]
        if len(value.encode("utf-8")) > max_field_bytes:
            raise ValidationError("The submitted form is invalid.")
        values[key] = value
    return values


def _safe_destination(destination: str) -> str:
    if not isinstance(destination, str) or len(destination) > 2048:
        return "/"
    decoded = unquote(destination)
    parsed = urlsplit(decoded)
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or "\\" in decoded
        or any(ord(character) < 32 for character in decoded)
    ):
        return "/"
    return destination


def _with_next(path: str, destination: str) -> str:
    return path + "?" + urlencode({"next": _safe_destination(destination)})
