"""Strict SAML 2.0 service-provider response validation.

The validator accepts HTTP-POST responses only.  It verifies the assertion's
XML signature against the deployment's pinned IdP certificate, selects the
assertion referenced by the signature (preventing signature wrapping),
validates the response/request binding, conditions, audience and recipient,
and claims the assertion id in an atomic replay cache before returning
attributes to the provisioning boundary.

The implementation uses ``lxml`` only for its hardened parser and XML
canonicalisation.  It does not trust a certificate embedded in ``KeyInfo``;
the configured certificate is the trust anchor.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import re
import secrets
import threading
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509 import load_pem_x509_certificate
from lxml import etree

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.security.provisioning import (
    AuditWriter,
    IdentitySource,
    ProviderUnavailable,
    SSOError,
)

SAML_NS: Final[str] = "urn:oasis:names:tc:SAML:2.0:assertion"
SAMLP_NS: Final[str] = "urn:oasis:names:tc:SAML:2.0:protocol"
DS_NS: Final[str] = "http://www.w3.org/2000/09/xmldsig#"
_EXCLUSIVE_C14N: Final[str] = "http://www.w3.org/2001/10/xml-exc-c14n#"
_EXCLUSIVE_C14N_WITH_COMMENTS: Final[str] = "http://www.w3.org/2001/10/xml-exc-c14n#WithComments"
_C14N: Final[str] = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
_C14N_WITH_COMMENTS: Final[str] = _C14N + "#WithComments"
_ENVELOPED: Final[str] = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
_SHA256: Final[str] = "http://www.w3.org/2001/04/xmlenc#sha256"
_SHA384: Final[str] = "http://www.w3.org/2001/04/xmldsig-more#sha384"
_SHA512: Final[str] = "http://www.w3.org/2001/04/xmlenc#sha512"
_RSA_SHA256: Final[str] = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
_RSA_SHA384: Final[str] = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha384"
_RSA_SHA512: Final[str] = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha512"
_ECDSA_SHA256: Final[str] = "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256"
_ECDSA_SHA384: Final[str] = "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha384"
_ECDSA_SHA512: Final[str] = "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha512"
_BEARER: Final[str] = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
_MAX_XML_BYTES: Final[int] = 2 * 1024 * 1024
_MAX_BASE64_INPUT_BYTES: Final[int] = 4 * _MAX_XML_BYTES
_MAX_ATTRIBUTE_VALUE: Final[int] = 4096


class SAMLError(SSOError):
    """A safe SAML response failure."""


class SAMLProviderUnavailable(ProviderUnavailable):
    """Reserved for metadata/IdP availability failures."""


@dataclass(frozen=True, slots=True)
class SAMLSettings:
    """Pinned SAML service-provider and IdP trust configuration."""

    entity_id: str
    idp_entity_id: str
    single_sign_on_url: str
    assertion_consumer_service_url: str
    idp_certificate: bytes | str | Path
    state_ttl: timedelta = timedelta(minutes=5)
    clock_skew: timedelta = timedelta(minutes=2)
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("entity_id", self.entity_id),
            ("idp_entity_id", self.idp_entity_id),
        ):
            if not isinstance(value, str) or not 1 <= len(value) <= 1024:
                raise ValueError(f"SAML {name} must be a bounded string.")
        _validate_url(
            self.single_sign_on_url,
            "single_sign_on_url",
            allow_insecure=self.allow_insecure_http,
        )
        _validate_url(
            self.assertion_consumer_service_url,
            "assertion_consumer_service_url",
            allow_insecure=True,
        )
        if self.state_ttl <= timedelta(0) or self.state_ttl > timedelta(hours=1):
            raise ValueError("SAML state TTL must be between one second and one hour.")
        if self.clock_skew < timedelta(0) or self.clock_skew > timedelta(minutes=10):
            raise ValueError("SAML clock skew must be between zero and ten minutes.")


@dataclass(frozen=True, slots=True)
class SAMLAuthRequest:
    """A generated AuthnRequest and the relay state bound to it."""

    url: str
    request_id: str
    relay_state: str
    redirect_destination: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SAMLIdentity:
    """Verified SAML attributes ready for mapping and provisioning."""

    claims: Mapping[str, object]
    redirect_destination: str = "/"
    provider: IdentitySource = IdentitySource.SAML

    @property
    def subject(self) -> str:
        """Return the NameID mapped to the stable subject claim."""
        value = self.claims.get("sub")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class _PendingRequest:
    request_id: str
    relay_hash: str
    redirect_destination: str
    expires_at: datetime


class SAMLRequestStore:
    """Atomic RelayState store shared by start and ACS requests."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingRequest] = {}
        self._consumed: dict[str, datetime] = {}
        self._lock = threading.RLock()

    def put(
        self, relay_state: str, request_id: str, destination: str, expires_at: datetime
    ) -> None:
        digest = _relay_digest(relay_state)
        with self._lock:
            self._prune(expires_at)
            self._pending[digest] = _PendingRequest(
                request_id=request_id,
                relay_hash=digest,
                redirect_destination=destination,
                expires_at=expires_at,
            )

    def get(self, relay_state: str, *, now: datetime) -> _PendingRequest | None:
        digest = _relay_digest(relay_state)
        with self._lock:
            self._prune(now)
            return self._pending.get(digest)

    def consume(self, relay_state: str, *, now: datetime) -> _PendingRequest | None:
        digest = _relay_digest(relay_state)
        with self._lock:
            self._prune(now)
            pending = self._pending.pop(digest, None)
            if pending is None or pending.expires_at <= now:
                return None
            self._consumed[digest] = pending.expires_at
            return pending

    def was_consumed(self, relay_state: str, *, now: datetime) -> bool:
        digest = _relay_digest(relay_state)
        with self._lock:
            self._prune(now)
            return digest in self._consumed

    def _prune(self, now: datetime) -> None:
        self._pending = {
            key: value for key, value in self._pending.items() if value.expires_at > now
        }
        self._consumed = {key: expiry for key, expiry in self._consumed.items() if expiry > now}


class SAMLReplayCache:
    """Thread-safe assertion replay cache suitable for one worker."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._seen: dict[str, datetime] = {}
        self._lock = threading.RLock()
        self._clock = clock or SystemClock()

    def claim(self, assertion_id: str, expires_at: datetime) -> bool:
        now = _utc(self._clock.now())
        with self._lock:
            self._seen = {key: value for key, value in self._seen.items() if value > now}
            if assertion_id in self._seen:
                return False
            self._seen[assertion_id] = expires_at
            return True


class SAMLServiceProvider:
    """Create requests and validate SAML HTTP-POST responses."""

    def __init__(
        self,
        settings: SAMLSettings,
        *,
        clock: Clock | None = None,
        audit: AuditWriter | None = None,
        request_store: SAMLRequestStore | None = None,
        replay_cache: SAMLReplayCache | None = None,
        request_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or SystemClock()
        self.audit = audit
        self.request_store = request_store or SAMLRequestStore()
        self.replay_cache = replay_cache or SAMLReplayCache(clock=self.clock)
        self.request_id = request_id or get_request_id() or new_request_id()
        self._certificate = _load_certificate(settings.idp_certificate)

    def begin_authorization(self, destination: str = "/") -> SAMLAuthRequest:
        """Create an unsigned AuthnRequest using the HTTP-Redirect binding."""
        now = _utc(self.clock.now())
        request_id = "_" + secrets.token_hex(24)
        relay_state = secrets.token_urlsafe(32)
        expires_at = now + self.settings.state_ttl
        safe_destination = _safe_destination(destination)
        self.request_store.put(relay_state, request_id, safe_destination, expires_at)
        request_xml = _authn_request_xml(
            request_id=request_id,
            issue_instant=now,
            settings=self.settings,
        )
        compressor = zlib.compressobj(level=9, wbits=-15)
        compressed = compressor.compress(request_xml) + compressor.flush()
        query = urlencode(
            {
                "SAMLRequest": base64.b64encode(compressed).decode("ascii"),
                "RelayState": relay_state,
            }
        )
        self._audit("authentication_sso_started", {"provider": "saml"})
        return SAMLAuthRequest(
            url=self.settings.single_sign_on_url + "?" + query,
            request_id=request_id,
            relay_state=relay_state,
            redirect_destination=safe_destination,
            expires_at=expires_at,
        )

    def complete_callback(
        self,
        response: str | bytes,
        relay_state: str,
        *,
        expected_relay_state: str | None = None,
    ) -> SAMLIdentity:
        """Validate one base64 XML response and return verified claims."""
        try:
            return self._complete_callback(
                response,
                relay_state,
                expected_relay_state=expected_relay_state,
            )
        except SAMLError as error:
            self._audit(
                "authentication_sso_security_event",
                {"provider": "saml", "reason": error.reason},
            )
            raise

    def _complete_callback(
        self,
        response: str | bytes,
        relay_state: str,
        *,
        expected_relay_state: str | None,
    ) -> SAMLIdentity:
        if not _bounded_ascii(relay_state, 512):
            raise self._refuse("relay_state_invalid")
        if expected_relay_state is not None and not hmac.compare_digest(
            relay_state, expected_relay_state
        ):
            raise self._refuse("relay_state_mismatch")
        now = _utc(self.clock.now())
        pending = self.request_store.get(relay_state, now=now)
        if pending is None:
            reason = (
                "relay_state_replayed"
                if self.request_store.was_consumed(relay_state, now=now)
                else "relay_state_invalid"
            )
            raise self._refuse(reason)
        root = self._parse_response(response)
        _required_id(root, "response")
        in_response_to = _required_attribute(root, "InResponseTo")
        if in_response_to != pending.request_id:
            raise self._refuse("in_response_to_mismatch")
        if _required_attribute(root, "Destination") != self.settings.assertion_consumer_service_url:
            raise self._refuse("destination_mismatch")
        _validate_issue_instant(root, now=now, skew=self.settings.clock_skew)
        issuer = _text(_child(root, SAML_NS, "Issuer"))
        if issuer != self.settings.idp_entity_id:
            raise self._refuse("issuer_mismatch")
        status = _child(root, SAMLP_NS, "Status")
        status_code = _child(status, SAMLP_NS, "StatusCode") if status is not None else None
        if (
            status_code is None
            or _required_attribute(status_code, "Value")
            != "urn:oasis:names:tc:SAML:2.0:status:Success"
        ):
            raise self._refuse("response_status_not_success")
        assertions = root.xpath(".//saml:Assertion", namespaces={"saml": SAML_NS})
        if len(assertions) != 1:
            raise self._refuse("assertion_count_invalid")
        assertion = assertions[0]
        assertion_id = _required_id(assertion, "assertion")
        self._verify_assertion_signature(assertion, assertion_id)
        _validate_assertion_context(
            root,
            assertion,
            request_id=pending.request_id,
            acs_url=self.settings.assertion_consumer_service_url,
            audience=self.settings.entity_id,
            expected_issuer=self.settings.idp_entity_id,
            now=now,
            skew=self.settings.clock_skew,
        )
        conditions = _child(assertion, SAML_NS, "Conditions")
        expires_at = _condition_expiry(conditions, now=now, skew=self.settings.clock_skew)
        if not self.replay_cache.claim(assertion_id, expires_at):
            raise self._refuse("assertion_replayed")
        consumed = self.request_store.consume(relay_state, now=now)
        if consumed is None:
            raise self._refuse("relay_state_replayed")
        claims = _extract_claims(assertion)
        self._audit("authentication_sso_succeeded", {"provider": "saml"})
        return SAMLIdentity(claims=claims, redirect_destination=consumed.redirect_destination)

    def _parse_response(self, response: str | bytes) -> etree._Element:
        if isinstance(response, str):
            try:
                encoded = response.encode("ascii", errors="strict")
            except UnicodeEncodeError as error:
                raise self._refuse("response_shape_invalid") from error
        elif isinstance(response, bytes):
            encoded = response
        else:
            raise self._refuse("response_shape_invalid")
        if len(encoded) > _MAX_BASE64_INPUT_BYTES:
            raise self._refuse("response_too_large")
        try:
            raw = _decode_base64(encoded)
        except (ValueError, TypeError):
            raw = encoded
        if len(raw) > _MAX_XML_BYTES:
            raise self._refuse("response_too_large")
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
            remove_comments=False,
        )
        try:
            root = etree.fromstring(raw, parser=parser)
        except (etree.XMLSyntaxError, ValueError, TypeError) as error:
            raise self._refuse("response_xml_invalid") from error
        if root.tag != f"{{{SAMLP_NS}}}Response":
            raise self._refuse("response_root_invalid")
        _reject_duplicate_ids(root)
        return root

    def _verify_assertion_signature(self, assertion: etree._Element, assertion_id: str) -> None:
        signatures = assertion.xpath("./ds:Signature", namespaces={"ds": DS_NS})
        if len(signatures) != 1:
            raise self._refuse("assertion_signature_missing_or_ambiguous")
        signature = signatures[0]
        signed_info = _child(signature, DS_NS, "SignedInfo")
        signature_value = _child(signature, DS_NS, "SignatureValue")
        if signed_info is None or signature_value is None:
            raise self._refuse("signature_shape_invalid")
        references = signed_info.xpath("./ds:Reference", namespaces={"ds": DS_NS})
        if len(references) != 1:
            raise self._refuse("signature_reference_invalid")
        reference = references[0]
        if _required_attribute(reference, "URI") != "#" + assertion_id:
            raise self._refuse("signature_reference_mismatch")
        transforms = _child(reference, DS_NS, "Transforms")
        if transforms is None:
            raise self._refuse("signature_transforms_missing")
        transform_algorithms = [
            _required_attribute(element, "Algorithm")
            for element in transforms.xpath("./ds:Transform", namespaces={"ds": DS_NS})
        ]
        if transform_algorithms not in (
            [_ENVELOPED, _EXCLUSIVE_C14N],
            [_ENVELOPED, _EXCLUSIVE_C14N_WITH_COMMENTS],
            [_ENVELOPED, _C14N],
            [_ENVELOPED, _C14N_WITH_COMMENTS],
        ):
            raise self._refuse("signature_transforms_unsupported")
        digest_method = _child(reference, DS_NS, "DigestMethod")
        digest_value = _text(_child(reference, DS_NS, "DigestValue"))
        if digest_method is None or not digest_value:
            raise self._refuse("signature_digest_missing")
        digest_algorithm = _required_attribute(digest_method, "Algorithm")
        digest = _hash_for_digest(digest_algorithm)
        unsigned_assertion = copy.deepcopy(assertion)
        nested_signature = unsigned_assertion.xpath("./ds:Signature", namespaces={"ds": DS_NS})
        if len(nested_signature) != 1:
            raise self._refuse("signature_shape_invalid")
        unsigned_assertion.remove(nested_signature[0])
        canonical_assertion = _canonicalize(unsigned_assertion, transform_algorithms[-1])
        expected_digest = hashlib.new(digest.name, canonical_assertion).digest()
        try:
            actual_digest = _decode_base64(digest_value)
        except (UnicodeEncodeError, ValueError, TypeError) as error:
            raise self._refuse("signature_digest_invalid") from error
        if not hmac.compare_digest(expected_digest, actual_digest):
            raise self._refuse("signature_digest_invalid")
        c14n_method = _child(signed_info, DS_NS, "CanonicalizationMethod")
        signature_method = _child(signed_info, DS_NS, "SignatureMethod")
        if c14n_method is None or signature_method is None:
            raise self._refuse("signature_method_missing")
        c14n_algorithm = _required_attribute(c14n_method, "Algorithm")
        signed_info_bytes = _canonicalize(signed_info, c14n_algorithm)
        signature_algorithm = _required_attribute(signature_method, "Algorithm")
        try:
            signature_bytes = _decode_base64(_text(signature_value))
        except (UnicodeEncodeError, ValueError, TypeError) as error:
            raise self._refuse("signature_value_invalid") from error
        if not _verify_xml_signature(
            self._certificate.public_key(),
            signature_algorithm,
            signed_info_bytes,
            signature_bytes,
        ):
            raise self._refuse("assertion_signature_invalid")

    def _refuse(self, reason: str) -> SAMLError:
        return SAMLError(reason)

    def _audit(self, event_type: str, payload: Mapping[str, object]) -> None:
        if self.audit is None:
            return
        self.audit.record(
            event_type,
            ("authentication_provider", "saml"),
            dict(payload),
            actor=None,
            request_id=self.request_id,
        )


def _authn_request_xml(
    *, request_id: str, issue_instant: datetime, settings: SAMLSettings
) -> bytes:
    root = etree.Element(
        f"{{{SAMLP_NS}}}AuthnRequest",
        nsmap={"samlp": SAMLP_NS, "saml": SAML_NS},
        ID=request_id,
        Version="2.0",
        IssueInstant=_format_time(issue_instant),
        Destination=settings.single_sign_on_url,
        AssertionConsumerServiceURL=settings.assertion_consumer_service_url,
        ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
    )
    etree.SubElement(root, f"{{{SAML_NS}}}Issuer").text = settings.entity_id
    etree.SubElement(
        root,
        f"{{{SAMLP_NS}}}NameIDPolicy",
        Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
        AllowCreate="true",
    )
    return etree.tostring(root, encoding="utf-8", xml_declaration=True)


def _validate_assertion_context(
    response: etree._Element,
    assertion: etree._Element,
    *,
    request_id: str,
    acs_url: str,
    audience: str,
    expected_issuer: str,
    now: datetime,
    skew: timedelta,
) -> None:
    issuer = _text(_child(assertion, SAML_NS, "Issuer"))
    if issuer != expected_issuer:
        raise SAMLError("assertion_issuer_mismatch")
    conditions = _child(assertion, SAML_NS, "Conditions")
    if conditions is None:
        raise SAMLError("conditions_missing")
    if not conditions.get("NotOnOrAfter"):
        raise SAMLError("assertion_expiry_missing")
    _validate_condition_times(conditions, now=now, skew=skew)
    restrictions = conditions.xpath("./saml:AudienceRestriction", namespaces={"saml": SAML_NS})
    if not restrictions or any(
        not any(
            _text(audience_element) == audience
            for audience_element in restriction.findall(f"{{{SAML_NS}}}Audience")
        )
        for restriction in restrictions
    ):
        raise SAMLError("audience_mismatch")
    confirmations = assertion.xpath(".//saml:SubjectConfirmation", namespaces={"saml": SAML_NS})
    valid_confirmation = False
    for confirmation in confirmations:
        if confirmation.get("Method") != _BEARER:
            continue
        data = _child(confirmation, SAML_NS, "SubjectConfirmationData")
        if data is None:
            continue
        if data.get("InResponseTo") != request_id or data.get("Recipient") != acs_url:
            continue
        if not data.get("NotOnOrAfter"):
            continue
        if not _time_window_is_valid(data, now=now, skew=skew):
            continue
        valid_confirmation = True
        break
    if not valid_confirmation:
        raise SAMLError("subject_confirmation_invalid")
    subject = _child(assertion, SAML_NS, "Subject")
    if subject is None or not _text(_child(subject, SAML_NS, "NameID")):
        raise SAMLError("subject_missing")
    if response.get("Destination") != acs_url:
        raise SAMLError("destination_mismatch")


def _validate_condition_times(
    conditions: etree._Element, *, now: datetime, skew: timedelta
) -> None:
    if not _time_window_is_valid(conditions, now=now, skew=skew):
        raise SAMLError("assertion_expired_or_not_yet_valid")


def _time_window_is_valid(element: etree._Element, *, now: datetime, skew: timedelta) -> bool:
    not_before = _parse_time(element.get("NotBefore")) if element.get("NotBefore") else None
    not_on_or_after = (
        _parse_time(element.get("NotOnOrAfter")) if element.get("NotOnOrAfter") else None
    )
    if not_before is not None and now + skew < not_before:
        return False
    if not_on_or_after is not None and now >= not_on_or_after + skew:
        return False
    return not_before is None or not_on_or_after is None or not_before < not_on_or_after


def _condition_expiry(
    conditions: etree._Element | None, *, now: datetime, skew: timedelta
) -> datetime:
    if conditions is None or not conditions.get("NotOnOrAfter"):
        raise SAMLError("assertion_expiry_missing")
    return _parse_time(conditions.get("NotOnOrAfter")) + skew


def _validate_issue_instant(element: etree._Element, *, now: datetime, skew: timedelta) -> None:
    issue_instant = element.get("IssueInstant")
    if issue_instant is None:
        raise SAMLError("issue_instant_missing")
    parsed = _parse_time(issue_instant)
    if parsed > now + skew or now >= parsed + timedelta(days=1):
        raise SAMLError("issue_instant_invalid")


def _extract_claims(assertion: etree._Element) -> Mapping[str, object]:
    subject = _child(_child(assertion, SAML_NS, "Subject"), SAML_NS, "NameID")
    name_id = _text(subject)
    values: dict[str, list[str]] = {"sub": [name_id], "nameid": [name_id]}
    attributes = assertion.xpath(".//saml:Attribute", namespaces={"saml": SAML_NS})
    for attribute in attributes:
        name = attribute.get("Name") or attribute.get("FriendlyName")
        if not name or not _bounded_text(name, 256):
            continue
        clean_values = []
        for value in attribute.findall(f"{{{SAML_NS}}}AttributeValue"):
            text = _text(value)
            if text and len(text) <= _MAX_ATTRIBUTE_VALUE and all(ord(char) >= 32 for char in text):
                clean_values.append(text)
        if clean_values:
            values.setdefault(name, []).extend(clean_values)
            friendly = attribute.get("FriendlyName")
            if friendly and friendly != name:
                values.setdefault(friendly, []).extend(clean_values)
    return {key: _claim_value(item) for key, item in values.items()}


def _claim_value(values: list[str]) -> str | tuple[str, ...]:
    unique = tuple(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else unique


def _load_certificate(value: bytes | str | Path):
    if isinstance(value, Path):
        raw = value.read_bytes()
    elif isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8") if "BEGIN CERTIFICATE" in value else Path(value).read_bytes()
    else:
        raise ValueError("SAML IdP certificate must be PEM bytes or a configured path.")
    try:
        certificate = load_pem_x509_certificate(raw)
    except ValueError as error:
        raise ValueError("SAML IdP certificate is not valid PEM X.509.") from error
    public_key = certificate.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey | ec.EllipticCurvePublicKey):
        raise ValueError("SAML IdP certificate uses an unsupported public-key type.")
    return certificate


def _verify_xml_signature(public_key, algorithm: str, data: bytes, signature: bytes) -> bool:
    algorithms = {
        _RSA_SHA256: (hashes.SHA256(), False),
        _RSA_SHA384: (hashes.SHA384(), False),
        _RSA_SHA512: (hashes.SHA512(), False),
        _ECDSA_SHA256: (hashes.SHA256(), True),
        _ECDSA_SHA384: (hashes.SHA384(), True),
        _ECDSA_SHA512: (hashes.SHA512(), True),
    }
    selected = algorithms.get(algorithm)
    if selected is None:
        return False
    digest, is_ecdsa = selected
    try:
        if is_ecdsa and isinstance(public_key, ec.EllipticCurvePublicKey):
            width = (public_key.curve.key_size + 7) // 8
            if len(signature) != width * 2:
                return False
            public_key.verify(
                encode_dss_signature(
                    int.from_bytes(signature[:width], "big"),
                    int.from_bytes(signature[width:], "big"),
                ),
                data,
                ec.ECDSA(digest),
            )
        elif not is_ecdsa and isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, data, padding.PKCS1v15(), digest)
        else:
            return False
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def _canonicalize(element: etree._Element, algorithm: str) -> bytes:
    if algorithm not in {
        _EXCLUSIVE_C14N,
        _EXCLUSIVE_C14N_WITH_COMMENTS,
        _C14N,
        _C14N_WITH_COMMENTS,
    }:
        raise SAMLError("canonicalization_unsupported")
    return etree.tostring(
        element,
        method="c14n",
        exclusive=algorithm.startswith(_EXCLUSIVE_C14N),
        with_comments=algorithm.endswith("WithComments"),
    )


def _hash_for_digest(algorithm: str) -> hashes.HashAlgorithm:
    selected = {_SHA256: hashes.SHA256(), _SHA384: hashes.SHA384(), _SHA512: hashes.SHA512()}.get(
        algorithm
    )
    if selected is None:
        raise SAMLError("digest_algorithm_unsupported")
    return selected


def _reject_duplicate_ids(root: etree._Element) -> None:
    values = [element.get("ID") for element in root.iter() if element.get("ID")]
    if len(values) != len(set(values)) or any(
        not re.fullmatch(r"[_A-Za-z][_A-Za-z0-9:.-]{0,255}", value) for value in values
    ):
        raise SAMLError("duplicate_or_invalid_xml_id")


def _required_id(element: etree._Element, name: str) -> str:
    value = element.get("ID")
    if value is None or not re.fullmatch(r"[_A-Za-z][_A-Za-z0-9:.-]{0,255}", value):
        raise SAMLError(f"{name}_id_invalid")
    return value


def _required_attribute(element: etree._Element | None, name: str) -> str:
    if element is None or not isinstance(element.get(name), str) or not element.get(name):
        raise SAMLError(f"xml_attribute_{name.lower()}_missing")
    return element.get(name) or ""


def _child(
    element: etree._Element | None, namespace: str, local_name: str
) -> etree._Element | None:
    if element is None:
        return None
    return element.find(f"{{{namespace}}}{local_name}")


def _text(element: etree._Element | None) -> str:
    return "" if element is None or element.text is None else element.text.strip()


def _decode_base64(value: str | bytes) -> bytes:
    """Decode XML-schema base64Binary while allowing legal whitespace."""
    encoded = value.encode("ascii") if isinstance(value, str) else value
    return base64.b64decode(b"".join(encoded.split()), validate=True)


def _parse_time(value: str | None) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise SAMLError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SAMLError("timestamp_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SAMLError("timestamp_timezone_missing")
    return parsed.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_url(value: str, name: str, *, allow_insecure: bool) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"SAML {name} must be an absolute HTTP(S) URL without credentials.")
    if parsed.fragment:
        raise ValueError(f"SAML {name} must not contain a fragment.")
    if parsed.scheme == "http" and not (
        allow_insecure and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError(f"SAML {name} must use HTTPS outside a local fixture.")


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


def _bounded_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def _bounded_ascii(value: object, maximum: int) -> bool:
    return _bounded_text(value, maximum) and all(ord(char) < 128 for char in value)


def _relay_digest(relay_state: str) -> str:
    return hashlib.sha256(relay_state.encode("ascii")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Authentication timestamps must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "SAMLAuthRequest",
    "SAMLIdentity",
    "SAMLProviderUnavailable",
    "SAMLReplayCache",
    "SAMLRequestStore",
    "SAMLServiceProvider",
    "SAMLSettings",
    "SAMLError",
]
