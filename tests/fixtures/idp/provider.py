"""Deterministic, local-only OIDC and SAML identity-provider fixtures."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from lxml import etree

from covenant_radar.security.saml import (
    _ENVELOPED,
    _EXCLUSIVE_C14N,
    _RSA_SHA256,
    _SHA256,
    DS_NS,
    SAML_NS,
    SAMLP_NS,
)

FIXED_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
OIDC_ISSUER = "https://idp.example.test"
IDP_ENTITY_ID = "https://idp.example.test/saml"
SAML_ENTITY_ID = "https://radar.example.test/saml"
SAML_ACS_URL = "https://radar.example.test/sso/saml/acs"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _xml_b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _jwt_key(kid: str) -> rsa.RSAPrivateKey:
    del kid
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


OIDC_OLD_KEY = _jwt_key("old")
OIDC_NEW_KEY = _jwt_key("new")
SAML_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_SAML_CERTIFICATE = (
    x509.CertificateBuilder()
    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline-idp")]))
    .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline-idp")]))
    .public_key(SAML_KEY.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(FIXED_NOW - timedelta(days=1))
    .not_valid_after(FIXED_NOW + timedelta(days=365))
    .sign(SAML_KEY, hashes.SHA256())
)
SAML_CERTIFICATE_PEM = _SAML_CERTIFICATE.public_bytes(serialization.Encoding.PEM)


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = key.private_numbers().public_numbers
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def make_jwt(
    *,
    key: rsa.RSAPrivateKey = OIDC_OLD_KEY,
    kid: str = "old",
    nonce: str,
    subject: str = "subject-1",
    email: str = "alice@example.test",
    roles: list[str] | None = None,
    portfolio_scope: list[str] | None = None,
    now: datetime = FIXED_NOW,
    expires_in: int = 300,
) -> str:
    header = {"alg": "RS256", "kid": kid, "typ": "JWT"}
    claims: dict[str, object] = {
        "iss": OIDC_ISSUER,
        "sub": subject,
        "aud": "radar-client",
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + expires_in,
        "email": email,
        "preferred_username": "alice",
        "name": "Alice Example",
        "roles": roles or ["relationship_manager"],
        "portfolio_scope": portfolio_scope or ["portfolio/root"],
    }
    encoded_header = _b64(json.dumps(header, separators=(",", ":")).encode("ascii"))
    encoded_claims = _b64(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{encoded_header}.{encoded_claims}.{_b64(signature)}"


class LocalOIDCTransport(httpx.AsyncBaseTransport):
    """An HTTPX transport with discovery, JWKS rotation and token exchange."""

    def __init__(self, *, key: rsa.RSAPrivateKey = OIDC_OLD_KEY, kid: str = "old") -> None:
        self.key = key
        self.kid = kid
        self.nonce = ""
        self.claims: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []

    def rotate(self) -> None:
        """Publish the new signing key used by subsequent token responses."""
        self.key = OIDC_NEW_KEY
        self.kid = "new"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                request=request,
                json={
                    "issuer": OIDC_ISSUER,
                    "authorization_endpoint": OIDC_ISSUER + "/authorize",
                    "token_endpoint": OIDC_ISSUER + "/token",
                    "jwks_uri": OIDC_ISSUER + "/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(
                200,
                request=request,
                json={"keys": [_jwk(self.key, self.kid)]},
            )
        if request.url.path == "/token":
            form = parse_qs(request.content.decode("utf-8"))
            self.nonce = self.nonce or ""
            token = make_jwt(
                key=self.key,
                kid=self.kid,
                nonce=self.nonce,
                **self.claims,
            )
            if form.get("code_verifier") == [""]:
                return httpx.Response(400, request=request, json={"error": "invalid_grant"})
            return httpx.Response(200, request=request, json={"id_token": token})
        return httpx.Response(404, request=request)


def _element(
    parent: etree._Element, namespace: str, local_name: str, **attributes: str
) -> etree._Element:
    return etree.SubElement(parent, f"{{{namespace}}}{local_name}", **attributes)


def make_saml_response(
    request_id: str,
    *,
    signed: bool = True,
    audience: str = SAML_ENTITY_ID,
    recipient: str = SAML_ACS_URL,
    now: datetime = FIXED_NOW,
    assertion_id: str = "_assertion-1",
) -> str:
    """Create a signed or unsigned SAML HTTP-POST response."""
    root = etree.Element(
        f"{{{SAMLP_NS}}}Response",
        nsmap={"samlp": SAMLP_NS, "saml": SAML_NS, "ds": DS_NS},
        ID="_response-1",
        Version="2.0",
        IssueInstant=now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        InResponseTo=request_id,
        Destination=recipient,
    )
    _element(root, SAML_NS, "Issuer").text = IDP_ENTITY_ID
    status = _element(root, SAMLP_NS, "Status")
    _element(
        status,
        SAMLP_NS,
        "StatusCode",
        Value="urn:oasis:names:tc:SAML:2.0:status:Success",
    )
    assertion = _element(
        root,
        SAML_NS,
        "Assertion",
        ID=assertion_id,
        Version="2.0",
        IssueInstant=now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )
    _element(assertion, SAML_NS, "Issuer").text = IDP_ENTITY_ID
    subject = _element(assertion, SAML_NS, "Subject")
    _element(subject, SAML_NS, "NameID").text = "subject-1"
    confirmation = _element(
        subject, SAML_NS, "SubjectConfirmation", Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"
    )
    _element(
        confirmation,
        SAML_NS,
        "SubjectConfirmationData",
        InResponseTo=request_id,
        Recipient=recipient,
        NotOnOrAfter=(now + timedelta(minutes=5))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    )
    conditions = _element(
        assertion,
        SAML_NS,
        "Conditions",
        NotBefore=(now - timedelta(seconds=10))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        NotOnOrAfter=(now + timedelta(minutes=5))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    )
    restriction = _element(conditions, SAML_NS, "AudienceRestriction")
    _element(restriction, SAML_NS, "Audience").text = audience
    statement = _element(assertion, SAML_NS, "AttributeStatement")
    for name, values in {
        "email": ["alice@example.test"],
        "preferred_username": ["alice"],
        "name": ["Alice Example"],
        "roles": ["relationship_manager"],
        "portfolio_scope": ["portfolio/root"],
    }.items():
        attribute = _element(statement, SAML_NS, "Attribute", Name=name)
        for value in values:
            _element(attribute, SAML_NS, "AttributeValue").text = value
    if signed:
        _sign_assertion(assertion)
    return base64.b64encode(etree.tostring(root, encoding="utf-8")).decode("ascii")


def _sign_assertion(assertion: etree._Element) -> None:
    assertion_id = assertion.get("ID") or ""
    unsigned = etree.fromstring(etree.tostring(assertion))
    canonical_assertion = etree.tostring(
        unsigned, method="c14n", exclusive=True, with_comments=False
    )
    digest_value = _xml_b64(hashlib.sha256(canonical_assertion).digest())
    signature = etree.Element(f"{{{DS_NS}}}Signature")
    signed_info = etree.SubElement(signature, f"{{{DS_NS}}}SignedInfo")
    etree.SubElement(signed_info, f"{{{DS_NS}}}CanonicalizationMethod", Algorithm=_EXCLUSIVE_C14N)
    etree.SubElement(signed_info, f"{{{DS_NS}}}SignatureMethod", Algorithm=_RSA_SHA256)
    reference = etree.SubElement(signed_info, f"{{{DS_NS}}}Reference", URI=f"#{assertion_id}")
    transforms = etree.SubElement(reference, f"{{{DS_NS}}}Transforms")
    etree.SubElement(transforms, f"{{{DS_NS}}}Transform", Algorithm=_ENVELOPED)
    etree.SubElement(transforms, f"{{{DS_NS}}}Transform", Algorithm=_EXCLUSIVE_C14N)
    etree.SubElement(reference, f"{{{DS_NS}}}DigestMethod", Algorithm=_SHA256)
    etree.SubElement(reference, f"{{{DS_NS}}}DigestValue").text = digest_value
    assertion.insert(1, signature)
    signed_info_bytes = etree.tostring(
        signed_info, method="c14n", exclusive=True, with_comments=False
    )
    signature_value = SAML_KEY.sign(signed_info_bytes, padding.PKCS1v15(), hashes.SHA256())
    etree.SubElement(signature, f"{{{DS_NS}}}SignatureValue").text = base64.b64encode(
        signature_value
    ).decode("ascii")
