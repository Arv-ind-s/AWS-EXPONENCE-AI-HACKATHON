"""A small, strict OpenID Connect authorization-code client.

Only the protocol mechanics live here.  Claims are returned to
``provisioning.py`` after signature and claim validation; this module never
decides application roles and never stores provider tokens.  Network access
is injectable through ``httpx`` so the complete flow is testable against an
offline transport.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import asymmetric, hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.security.provisioning import (
    AuditWriter,
    IdentitySource,
    ProviderUnavailable,
    SSOError,
)

_DEFAULT_SCOPES: Final[tuple[str, ...]] = ("openid", "profile", "email")
_SUPPORTED_SIGNATURE_ALGORITHMS: Final[frozenset[str]] = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)
_MAX_JWT_BYTES: Final[int] = 64 * 1024
_MAX_JSON_BYTES: Final[int] = 2 * 1024 * 1024


class OIDCError(SSOError):
    """A deliberately generic OIDC flow failure."""


class OIDCProviderUnavailable(ProviderUnavailable):
    """An OIDC discovery, JWKS or token endpoint is unavailable."""


@dataclass(frozen=True, slots=True)
class OIDCSettings:
    """Validated, deployment-supplied OIDC client configuration."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = _DEFAULT_SCOPES
    discovery_url: str | None = None
    state_ttl: timedelta = timedelta(minutes=5)
    jwks_cache_ttl: timedelta = timedelta(minutes=15)
    clock_skew: timedelta = timedelta(minutes=2)
    request_timeout_seconds: float = 5.0
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        _validate_url(self.issuer, "issuer", allow_insecure=self.allow_insecure_http)
        _validate_url(self.redirect_uri, "redirect_uri", allow_insecure=True)
        if self.discovery_url is not None:
            _validate_url(
                self.discovery_url, "discovery_url", allow_insecure=self.allow_insecure_http
            )
        for name, value in (
            ("client_id", self.client_id),
            ("client_secret", self.client_secret),
        ):
            if not isinstance(value, str) or not value or len(value) > 1024:
                raise ValueError(f"OIDC {name} must be a non-empty bounded string.")
        if not self.scopes or any(not _safe_scope(scope) for scope in self.scopes):
            raise ValueError("OIDC scopes must contain safe, non-empty values.")
        if self.state_ttl <= timedelta(0) or self.state_ttl > timedelta(hours=1):
            raise ValueError("OIDC state TTL must be between one second and one hour.")
        if self.jwks_cache_ttl <= timedelta(0) or self.jwks_cache_ttl > timedelta(days=7):
            raise ValueError("OIDC JWKS cache TTL is outside the supported range.")
        if self.clock_skew < timedelta(0) or self.clock_skew > timedelta(minutes=10):
            raise ValueError("OIDC clock skew must be between zero and ten minutes.")
        if not 0.1 <= self.request_timeout_seconds <= 60:
            raise ValueError("OIDC request timeout must be between 0.1 and 60 seconds.")


@dataclass(frozen=True, slots=True)
class OIDCDiscovery:
    """The endpoints needed by the authorization-code flow."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True, slots=True)
class OIDCAuthRequest:
    """State returned when the browser is redirected to the provider."""

    url: str
    state: str
    nonce: str
    code_verifier: str
    redirect_destination: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OIDCIdentity:
    """Verified OIDC claims, ready for claim mapping and provisioning."""

    claims: Mapping[str, object]
    redirect_destination: str = "/"
    provider: IdentitySource = IdentitySource.OIDC

    @property
    def subject(self) -> str:
        """Return the provider's stable subject identifier."""
        value = self.claims.get("sub")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class _PendingState:
    state_hash: str
    code_verifier: str
    nonce: str
    redirect_destination: str
    expires_at: datetime


class OIDCStateStore:
    """Atomic, bounded, replay-resistant state store.

    Only a SHA-256 digest of the browser-visible state is held.  The store is
    process-local by default; a multi-worker deployment should provide the
    same interface backed by its shared cache.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingState] = {}
        self._consumed: dict[str, datetime] = {}
        self._lock = threading.RLock()

    def put(
        self,
        state: str,
        *,
        code_verifier: str,
        nonce: str,
        redirect_destination: str,
        expires_at: datetime,
    ) -> None:
        digest = _state_digest(state)
        with self._lock:
            self._prune(expires_at)
            self._pending[digest] = _PendingState(
                state_hash=digest,
                code_verifier=code_verifier,
                nonce=nonce,
                redirect_destination=redirect_destination,
                expires_at=expires_at,
            )

    def get(self, state: str, *, now: datetime) -> _PendingState | None:
        """Read pending state without consuming it."""
        digest = _state_digest(state)
        with self._lock:
            self._prune(now)
            value = self._pending.get(digest)
            if value is None or not hmac.compare_digest(value.state_hash, digest):
                return None
            return value

    def consume(self, state: str, *, now: datetime) -> _PendingState | None:
        """Atomically consume valid state exactly once."""
        digest = _state_digest(state)
        with self._lock:
            self._prune(now)
            value = self._pending.pop(digest, None)
            if value is None or value.expires_at <= now:
                return None
            self._consumed[digest] = value.expires_at
            return value

    def was_consumed(self, state: str, *, now: datetime) -> bool:
        digest = _state_digest(state)
        with self._lock:
            self._prune(now)
            return digest in self._consumed

    def _prune(self, now: datetime) -> None:
        self._pending = {
            key: value for key, value in self._pending.items() if value.expires_at > now
        }
        self._consumed = {key: expiry for key, expiry in self._consumed.items() if expiry > now}


class OIDCClient:
    """Perform discovery, PKCE authorization, token exchange and validation."""

    def __init__(
        self,
        settings: OIDCSettings,
        *,
        clock: Clock | None = None,
        audit: AuditWriter | None = None,
        state_store: OIDCStateStore | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        request_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or SystemClock()
        self.audit = audit
        self.state_store = state_store or OIDCStateStore()
        self._http_client = http_client
        self._transport = transport
        self.request_id = request_id or get_request_id() or new_request_id()
        self._cache_lock = threading.RLock()
        self._discovery: tuple[OIDCDiscovery, datetime] | None = None
        self._jwks: tuple[tuple[Mapping[str, object], ...], datetime] | None = None

    async def close(self) -> None:
        """Close an internally-created HTTP client if one has been opened."""
        return None

    async def discover(self, *, force: bool = False) -> OIDCDiscovery:
        """Load and validate provider metadata, using a short-lived cache."""
        now = _utc(self.clock.now())
        with self._cache_lock:
            if not force and self._discovery is not None and self._discovery[1] > now:
                return self._discovery[0]
        url = self.settings.discovery_url or _discovery_url(self.settings.issuer)
        try:
            document = await self._get_json(url)
        except OIDCProviderUnavailable:
            self._audit("authentication_sso_provider_unavailable", {"provider": "oidc"})
            raise
        try:
            discovery = _parse_discovery(document, self.settings)
        except (TypeError, ValueError) as error:
            self._audit(
                "authentication_sso_security_event",
                {"provider": "oidc", "reason": "discovery_invalid"},
            )
            raise OIDCError("discovery_invalid") from error
        with self._cache_lock:
            self._discovery = (discovery, now + self.settings.jwks_cache_ttl)
        return discovery

    async def begin_authorization(self, destination: str = "/") -> OIDCAuthRequest:
        """Create a PKCE-bound request and return its provider redirect URL."""
        discovery = await self.discover()
        now = _utc(self.clock.now())
        state = _token(32)
        nonce = _token(32)
        code_verifier = _token(64)
        expires_at = now + self.settings.state_ttl
        safe_destination = _safe_destination(destination)
        self.state_store.put(
            state,
            code_verifier=code_verifier,
            nonce=nonce,
            redirect_destination=safe_destination,
            expires_at=expires_at,
        )
        params = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": " ".join(self.settings.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        self._audit("authentication_sso_started", {"provider": "oidc"})
        return OIDCAuthRequest(
            url=discovery.authorization_endpoint + "?" + urlencode(params),
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            redirect_destination=safe_destination,
            expires_at=expires_at,
        )

    async def complete_callback(
        self,
        code: str,
        state: str,
        *,
        expected_state: str | None = None,
    ) -> OIDCIdentity:
        """Exchange one callback code and return only verified identity claims."""
        try:
            if not _bounded_text(code, 4096) or not _bounded_ascii(state, 512):
                raise OIDCError("callback_shape_invalid")
            now = _utc(self.clock.now())
            if expected_state is not None and (
                not _bounded_ascii(expected_state, 512)
                or not hmac.compare_digest(state, expected_state)
            ):
                raise OIDCError("state_mismatch")
            pending = self.state_store.get(state, now=now)
            if pending is None:
                reason = (
                    "state_replayed"
                    if self.state_store.was_consumed(state, now=now)
                    else "state_invalid"
                )
                raise OIDCError(reason)
            discovery = await self.discover()
            token_document = await self._exchange_code(discovery, code, pending.code_verifier)
            token = token_document.get("id_token")
            if not isinstance(token, str) or len(token) > _MAX_JWT_BYTES:
                raise OIDCError("id_token_missing")
            claims = await self._verify_id_token(token, expected_nonce=pending.nonce)
            consumed = self.state_store.consume(state, now=now)
            if consumed is None:
                raise OIDCError("state_replayed")
        except OIDCError as error:
            self._audit(
                "authentication_sso_security_event",
                {"provider": "oidc", "reason": error.reason},
            )
            raise
        except OIDCProviderUnavailable:
            raise
        except Exception as error:
            self._audit(
                "authentication_sso_security_event",
                {"provider": "oidc", "reason": "callback_invalid"},
            )
            raise OIDCError("callback_invalid") from error
        self._audit("authentication_sso_succeeded", {"provider": "oidc"})
        return OIDCIdentity(claims=claims, redirect_destination=pending.redirect_destination)

    async def verify_id_token(self, token: str, *, expected_nonce: str) -> Mapping[str, object]:
        """Verify a token and audit every deliberate validation refusal."""
        try:
            return await self._verify_id_token(token, expected_nonce=expected_nonce)
        except OIDCError as error:
            self._audit(
                "authentication_sso_security_event",
                {"provider": "oidc", "reason": error.reason},
            )
            raise

    async def _verify_id_token(self, token: str, *, expected_nonce: str) -> Mapping[str, object]:
        """Verify a signed ID token, refreshing JWKS once for rotation."""
        header, claims, signing_input, signature = _decode_jwt(token)
        algorithm = header.get("alg")
        kid = header.get("kid")
        if not isinstance(algorithm, str) or algorithm not in _SUPPORTED_SIGNATURE_ALGORITHMS:
            raise self._refuse("unsupported_signature_algorithm")
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise self._refuse("missing_key_id")
        keys = await self._get_jwks()
        key = _find_key(keys, kid, algorithm)
        verified = key is not None and _verify_signature(signing_input, signature, key, algorithm)
        if not verified:
            # A new signing key may have been published between discovery and
            # validation.  One forced refresh handles rotation without
            # turning a bad token into an unbounded network retry.
            keys = await self._get_jwks(force=True)
            key = _find_key(keys, kid, algorithm)
            verified = key is not None and _verify_signature(
                signing_input, signature, key, algorithm
            )
        if not verified:
            raise self._refuse("signature_invalid")
        _validate_claims(claims, self.settings, expected_nonce, _utc(self.clock.now()))
        return claims

    async def _get_jwks(self, *, force: bool = False) -> tuple[Mapping[str, object], ...]:
        now = _utc(self.clock.now())
        with self._cache_lock:
            if not force and self._jwks is not None and self._jwks[1] > now:
                return self._jwks[0]
        discovery = await self.discover(force=force)
        try:
            document = await self._get_json(discovery.jwks_uri)
            raw_keys = document.get("keys")
            if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > 100:
                raise ValueError("JWKS keys are missing")
            keys = tuple(item for item in raw_keys if isinstance(item, Mapping))
            if len(keys) != len(raw_keys) or len({item.get("kid") for item in keys}) != len(keys):
                raise ValueError("JWKS contains malformed or duplicate keys")
            if not all(isinstance(item.get("kid"), str) for item in keys):
                raise ValueError("JWKS key id is missing")
        except OIDCProviderUnavailable:
            self._audit("authentication_sso_provider_unavailable", {"provider": "oidc"})
            raise
        except (TypeError, ValueError) as error:
            self._audit(
                "authentication_sso_security_event",
                {"provider": "oidc", "reason": "jwks_invalid"},
            )
            raise OIDCError("jwks_invalid") from error
        with self._cache_lock:
            self._jwks = (keys, now + self.settings.jwks_cache_ttl)
        return keys

    async def _exchange_code(
        self, discovery: OIDCDiscovery, code: str, code_verifier: str
    ) -> Mapping[str, object]:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": code_verifier,
        }
        try:
            response = await self._request(
                "POST",
                discovery.token_endpoint,
                data=payload,
                auth=(self.settings.client_id, self.settings.client_secret),
                headers={"Accept": "application/json"},
            )
            document = _json_response(response)
        except OIDCProviderUnavailable:
            self._audit("authentication_sso_provider_unavailable", {"provider": "oidc"})
            raise
        if "error" in document or not isinstance(document.get("id_token"), str):
            raise self._refuse("token_exchange_rejected")
        return document

    async def _get_json(self, url: str) -> Mapping[str, object]:
        try:
            response = await self._request("GET", url, headers={"Accept": "application/json"})
            return _json_response(response)
        except OIDCProviderUnavailable:
            raise
        except Exception as error:
            raise OIDCProviderUnavailable("provider_request_failed") from error

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._http_client is not None:
            try:
                response = await self._http_client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.HTTPError, ValueError) as error:
                raise OIDCProviderUnavailable("provider_request_failed") from error
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
        except (httpx.HTTPError, ValueError) as error:
            raise OIDCProviderUnavailable("provider_request_failed") from error

    def _refuse(self, reason: str) -> OIDCError:
        return OIDCError(reason)

    def _audit(self, event_type: str, payload: Mapping[str, object]) -> None:
        if self.audit is None:
            return
        self.audit.record(
            event_type,
            ("authentication_provider", "oidc"),
            dict(payload),
            actor=None,
            request_id=self.request_id,
        )


def _parse_discovery(document: Mapping[str, object], settings: OIDCSettings) -> OIDCDiscovery:
    issuer = _endpoint(document.get("issuer"), "issuer", settings)
    if issuer != settings.issuer.rstrip("/"):
        raise ValueError("Discovery issuer does not match configured issuer")
    return OIDCDiscovery(
        issuer=issuer,
        authorization_endpoint=_endpoint(
            document.get("authorization_endpoint"), "authorization_endpoint", settings
        ),
        token_endpoint=_endpoint(document.get("token_endpoint"), "token_endpoint", settings),
        jwks_uri=_endpoint(document.get("jwks_uri"), "jwks_uri", settings),
    )


def _endpoint(value: object, name: str, settings: OIDCSettings) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError(f"OIDC {name} is missing")
    _validate_url(value, name, allow_insecure=settings.allow_insecure_http)
    return value.rstrip("/") if name == "issuer" else value


def _validate_url(value: str, name: str, *, allow_insecure: bool) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"OIDC {name} must be an absolute HTTP(S) URL without credentials.")
    if parsed.fragment:
        raise ValueError(f"OIDC {name} must not contain a fragment.")
    if parsed.scheme == "http" and not (allow_insecure and _loopback(parsed.hostname)):
        raise ValueError(f"OIDC {name} must use HTTPS outside an explicitly local fixture.")


def _loopback(host: str | None) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


def _json_response(response: httpx.Response) -> Mapping[str, object]:
    if len(response.content) > _MAX_JSON_BYTES:
        raise OIDCProviderUnavailable("provider_response_too_large")
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError) as error:
        raise OIDCProviderUnavailable("provider_response_invalid_json") from error
    if not isinstance(value, Mapping):
        raise OIDCProviderUnavailable("provider_response_invalid_shape")
    return value


def _decode_jwt(token: str) -> tuple[Mapping[str, object], Mapping[str, object], bytes, bytes]:
    if not isinstance(token, str) or not token or len(token) > _MAX_JWT_BYTES:
        raise OIDCError("jwt_shape_invalid")
    pieces = token.split(".")
    if len(pieces) != 3:
        raise OIDCError("jwt_shape_invalid")
    try:
        header = _json_object(_b64decode(pieces[0]))
        claims = _json_object(_b64decode(pieces[1]))
        signature = _b64decode(pieces[2])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OIDCError("jwt_shape_invalid") from error
    return header, claims, f"{pieces[0]}.{pieces[1]}".encode("ascii"), signature


def _json_object(data: bytes) -> Mapping[str, object]:
    value = json.loads(data)
    if not isinstance(value, Mapping):
        raise ValueError("JWT component is not an object")
    return value


def _b64decode(value: str) -> bytes:
    if (
        not value
        or len(value) > _MAX_JWT_BYTES
        or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for char in value
        )
    ):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _find_key(
    keys: tuple[Mapping[str, object], ...], kid: str, algorithm: str
) -> Mapping[str, object] | None:
    matches = tuple(key for key in keys if key.get("kid") == kid)
    if len(matches) != 1:
        return None
    key = matches[0]
    if key.get("use") not in (None, "sig") or key.get("alg") not in (None, algorithm):
        return None
    return key


def _verify_signature(
    signing_input: bytes, signature: bytes, key: Mapping[str, object], algorithm: str
) -> bool:
    try:
        public_key = _public_key(key)
        digest = _hash_for_algorithm(algorithm)
        if isinstance(public_key, rsa.RSAPublicKey):
            if algorithm.startswith("PS"):
                public_key.verify(
                    signature,
                    signing_input,
                    padding.PSS(mgf=padding.MGF1(digest), salt_length=digest.digest_size),
                    digest,
                )
            else:
                public_key.verify(signature, signing_input, padding.PKCS1v15(), digest)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            expected_curves = {
                "ES256": "secp256r1",
                "ES384": "secp384r1",
                "ES512": "secp521r1",
            }
            if public_key.curve.name != expected_curves.get(algorithm):
                return False
            width = (public_key.curve.key_size + 7) // 8
            if len(signature) != width * 2:
                return False
            r = int.from_bytes(signature[:width], "big")
            s = int.from_bytes(signature[width:], "big")
            from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

            public_key.verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(digest))
        else:
            return False
    except (InvalidSignature, ValueError, TypeError, KeyError):
        return False
    return True


def _public_key(key: Mapping[str, object]) -> asymmetric.AsymmetricPublicKey:
    key_type = key.get("kty")
    if key_type == "RSA":
        n = _b64decode(_required_jwk_string(key, "n"))
        e = _b64decode(_required_jwk_string(key, "e"))
        modulus = int.from_bytes(n, "big")
        exponent = int.from_bytes(e, "big")
        if len(n) < 256 or exponent < 3 or exponent % 2 == 0:
            raise ValueError("unsafe RSA JWK")
        return rsa.RSAPublicNumbers(exponent, modulus).public_key()
    if key_type == "EC":
        curve_name = key.get("crv")
        curves: dict[str, ec.EllipticCurve] = {
            "P-256": ec.SECP256R1(),
            "P-384": ec.SECP384R1(),
            "P-521": ec.SECP521R1(),
        }
        curve = curves.get(curve_name) if isinstance(curve_name, str) else None
        if curve is None:
            raise ValueError("unsupported EC curve")
        point = (
            b"\x04"
            + _b64decode(_required_jwk_string(key, "x"))
            + _b64decode(_required_jwk_string(key, "y"))
        )
        return ec.EllipticCurvePublicKey.from_encoded_point(curve, point)
    raise ValueError("unsupported JWK type")


def _required_jwk_string(key: Mapping[str, object], name: str) -> str:
    value = key.get(name)
    if not isinstance(value, str):
        raise ValueError(f"JWK member {name} is missing")
    return value


def _hash_for_algorithm(algorithm: str) -> hashes.HashAlgorithm:
    return {
        "RS256": hashes.SHA256(),
        "PS256": hashes.SHA256(),
        "ES256": hashes.SHA256(),
        "RS384": hashes.SHA384(),
        "PS384": hashes.SHA384(),
        "ES384": hashes.SHA384(),
        "RS512": hashes.SHA512(),
        "PS512": hashes.SHA512(),
        "ES512": hashes.SHA512(),
    }[algorithm]


def _validate_claims(
    claims: Mapping[str, object],
    settings: OIDCSettings,
    expected_nonce: str,
    now: datetime,
) -> None:
    issuer = claims.get("iss")
    subject = claims.get("sub")
    audience = claims.get("aud")
    nonce = claims.get("nonce")
    if (
        issuer != settings.issuer.rstrip("/")
        or not isinstance(subject, str)
        or not _bounded_text(subject, 255)
    ):
        raise OIDCError("issuer_or_subject_mismatch")
    audiences = (
        (audience,)
        if isinstance(audience, str)
        else tuple(audience)
        if isinstance(audience, list)
        else ()
    )
    if not audiences or any(not isinstance(item, str) or not item for item in audiences):
        raise OIDCError("audience_mismatch")
    if settings.client_id not in audiences:
        raise OIDCError("audience_mismatch")
    if len(audiences) > 1 and claims.get("azp") != settings.client_id:
        raise OIDCError("authorized_party_mismatch")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
        raise OIDCError("nonce_mismatch")
    exp = _numeric_claim(claims.get("exp"))
    issued_at = _numeric_claim(claims.get("iat"))
    now_seconds = now.timestamp()
    skew = settings.clock_skew.total_seconds()
    if exp is None or now_seconds >= exp + skew:
        raise OIDCError("token_expired")
    if issued_at is None or issued_at > now_seconds + skew:
        raise OIDCError("token_issued_in_future")
    not_before = claims.get("nbf")
    if not_before is not None:
        not_before_value = _numeric_claim(not_before)
        if not_before_value is None or not_before_value > now_seconds + skew:
            raise OIDCError("token_not_yet_valid")


def _numeric_claim(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _token(byte_count: int) -> str:
    return secrets.token_urlsafe(byte_count)


def _pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("ascii")).hexdigest()


def _discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def _safe_scope(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and all(char.isalnum() or char in {".", "_", "-"} for char in value)
    )


def _bounded_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def _bounded_ascii(value: object, maximum: int) -> bool:
    return _bounded_text(value, maximum) and all(ord(char) < 128 for char in value)


def _safe_destination(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        return "/"
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return "/"
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Authentication timestamps must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "OIDCAuthRequest",
    "OIDCClient",
    "OIDCDiscovery",
    "OIDCError",
    "OIDCIdentity",
    "OIDCProviderUnavailable",
    "OIDCSettings",
    "OIDCStateStore",
]
