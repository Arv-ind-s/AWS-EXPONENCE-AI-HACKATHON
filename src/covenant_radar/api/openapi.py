"""The generated OpenAPI document: enrichment, validation and export.

FastAPI already derives an OpenAPI document from the registered routes and
Pydantic models. This module is the one place that document is enriched with
a bearer security scheme, a documented deprecation policy, and worked
JSON examples (`R-32.a`: "a generated OpenAPI document with examples"), and
checked for internal consistency.

``validate_openapi_document`` checks the parts of the OpenAPI 3.1 shape this
product's own generation touches — required top-level keys, that every
operation declares at least one described response, and that every ``$ref``
in the document resolves — rather than the complete OpenAPI meta-schema.
The full meta-schema is deliberately not vendored or fetched: no dependency
outside `requirements.lock` may be added, and nothing is fetched from a
third-party origin at runtime or at test time. A document that passes this
check is internally consistent with itself and with the routes that
produced it; `tests/contract/test_api_contract.py` is what proves the
document *matches* the live application from both directions, which is the
half a meta-schema check could never verify anyway.

No document is written into `docs/api/` and checked in: a generated
artefact must never be committed. `docs/api/README.md` documents the one
command that regenerates it from a running deployment's app instance.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

_OPENAPI_VERSION: Final[str] = "3.1.0"
_BEARER_SECURITY_SCHEME: Final[str] = "ApiKeyBearer"
_HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)
_OPENAPI_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"3\.1\.\d+")
_MAX_EXAMPLE_PROPERTIES: Final[int] = 8
_MAX_REF_DEPTH: Final[int] = 12

#: The single source of truth for the API's deprecation commitment. Embedded
#: into the OpenAPI document as ``info.x-deprecation-policy`` and echoed
#: verbatim in ``docs/api/deprecation-policy.md`` so the two never drift.
DEPRECATION_POLICY: Final[str] = (
    "A resource, field or parameter scheduled for removal is announced in "
    "the release notes at least two minor releases before it is removed, "
    'and is marked `"deprecated": true` in this document for that entire '
    "window. A deprecated element keeps returning identical data until the "
    "announced removal release — a deprecation is never used to silently "
    "change behaviour. Once a removal date is fixed, responses that would "
    "use the deprecated element carry a `Sunset` header naming it. Removal "
    "happens only in the announced release, never earlier."
)

_NO_EXAMPLE: Final[object] = object()

_FORMAT_EXAMPLES: Final[dict[str, Any]] = {
    "date": "2026-01-31",
    "date-time": "2026-01-31T09:00:00Z",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "user@example.test",
    "uri": "https://example.test/resource",
    "hostname": "example.test",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
}


def build_openapi_document(
    app: FastAPI,
    *,
    title: str = "Covenant Radar API",
    version: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Return the enriched OpenAPI 3.1 document for ``app``'s registered routes.

    ``version`` must be supplied by the caller (the running application's
    own version) rather than defaulted here, so the document can never claim
    a version the caller did not explicitly choose.
    """
    document = get_openapi(
        title=title,
        version=version,
        description=description or _DEFAULT_DESCRIPTION,
        routes=app.routes,
        openapi_version=_OPENAPI_VERSION,
    )
    _add_security_scheme(document)
    _add_deprecation_policy(document)
    _add_examples(document)
    return document


def validate_openapi_document(document: Mapping[str, Any]) -> list[str]:
    """Return every structural violation found in ``document``; empty is valid."""
    violations: list[str] = []
    openapi_version = document.get("openapi")
    if not isinstance(openapi_version, str) or not _OPENAPI_VERSION_PATTERN.fullmatch(
        openapi_version
    ):
        violations.append(f"openapi must be a 3.1.x version string, got {openapi_version!r}.")

    info = document.get("info")
    if (
        not isinstance(info, dict)
        or not _non_empty_str(info.get("title"))
        or not _non_empty_str(info.get("version"))
    ):
        violations.append("info.title and info.version are both required, non-empty strings.")

    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        violations.append("paths must be a non-empty object.")
        paths = {}

    operation_ids: dict[str, str] = {}
    for path, path_item in paths.items():
        label_prefix = f"path {path!r}"
        if not isinstance(path, str) or not path.startswith("/"):
            violations.append(f"{label_prefix} must be an absolute path starting with '/'.")
            continue
        if not isinstance(path_item, Mapping):
            violations.append(f"{label_prefix} must map to an object.")
            continue
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS:
                continue
            operation_label = f"{method.upper()} {path}"
            if not isinstance(operation, Mapping):
                violations.append(f"{operation_label} must map to an object.")
                continue
            violations.extend(_validate_operation(operation_label, operation, operation_ids))

    violations.extend(_unresolved_refs(document))
    return violations


def _validate_operation(
    label: str, operation: Mapping[str, Any], operation_ids: dict[str, str]
) -> list[str]:
    violations: list[str] = []
    responses = operation.get("responses")
    if not isinstance(responses, Mapping) or not responses:
        violations.append(f"{label} must declare at least one response.")
    else:
        for status_code, response in responses.items():
            if not isinstance(response, Mapping) or not _non_empty_str(response.get("description")):
                violations.append(
                    f"{label} response {status_code!r} needs a non-empty description."
                )

    operation_id = operation.get("operationId")
    if operation_id is not None:
        if not isinstance(operation_id, str) or not operation_id:
            violations.append(f"{label} operationId must be a non-empty string.")
        elif operation_id in operation_ids:
            existing = operation_ids[operation_id]
            violations.append(f"operationId {operation_id!r} is reused by {existing} and {label}.")
        else:
            operation_ids[operation_id] = label
    return violations


def _unresolved_refs(document: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    for ref in _iter_refs(document):
        if not ref.startswith("#/"):
            violations.append(f"$ref {ref!r} is not a local reference; external refs are refused.")
            continue
        if _resolve_pointer(document, ref) is _NO_EXAMPLE:
            violations.append(f"$ref {ref!r} does not resolve within the document.")
    return violations


def _iter_refs(node: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.append(ref)
        for value in node.values():
            refs.extend(_iter_refs(value))
    elif isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for item in node:
            refs.extend(_iter_refs(item))
    return refs


def _resolve_pointer(document: Mapping[str, Any], ref: str) -> Any:
    node: Any = document
    for segment in ref.removeprefix("#/").split("/"):
        if not segment:
            continue
        decoded = segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or decoded not in node:
            return _NO_EXAMPLE
        node = node[decoded]
    return node


def _add_security_scheme(document: dict[str, Any]) -> None:
    components = document.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes[_BEARER_SECURITY_SCHEME] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "A scoped API key issued by an administrator. Send it as "
            "`Authorization: Bearer <key>`. A browser session cookie is "
            "accepted in its place for the same routes."
        ),
    }
    document.setdefault("security", [{_BEARER_SECURITY_SCHEME: []}])


def _add_deprecation_policy(document: dict[str, Any]) -> None:
    info = document.setdefault("info", {})
    info["x-deprecation-policy"] = DEPRECATION_POLICY


def _add_examples(document: dict[str, Any]) -> None:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            _add_examples_to_body(operation.get("requestBody"), document)
            responses = operation.get("responses")
            if isinstance(responses, dict):
                for response in responses.values():
                    if isinstance(response, dict):
                        _add_examples_to_body(response, document)


def _add_examples_to_body(body: Any, document: Mapping[str, Any]) -> None:
    if not isinstance(body, dict):
        return
    content = body.get("content")
    if not isinstance(content, dict):
        return
    media = content.get("application/json")
    if not isinstance(media, dict) or "example" in media or "examples" in media:
        return
    schema = media.get("schema")
    if not isinstance(schema, dict):
        return
    example = _example_for_schema(schema, document, seen=frozenset())
    if example is not _NO_EXAMPLE:
        media["example"] = example


def _example_for_schema(
    schema: Mapping[str, Any], document: Mapping[str, Any], *, seen: frozenset[str]
) -> Any:
    if len(seen) > _MAX_REF_DEPTH:
        return _NO_EXAMPLE
    if "example" in schema:
        return schema["example"]
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", maxsplit=1)[-1]
        if name in seen:
            return _NO_EXAMPLE
        resolved = _resolve_pointer(document, ref)
        if resolved is _NO_EXAMPLE or not isinstance(resolved, Mapping):
            return _NO_EXAMPLE
        return _example_for_schema(resolved, document, seen=seen | {name})

    for combinator in ("allOf", "anyOf", "oneOf"):
        parts = schema.get(combinator)
        if isinstance(parts, list) and parts:
            if combinator == "allOf":
                merged: dict[str, Any] = {}
                for part in parts:
                    if isinstance(part, Mapping):
                        value = _example_for_schema(part, document, seen=seen)
                        if isinstance(value, Mapping):
                            merged.update(value)
                if merged:
                    return merged
            for part in parts:
                if isinstance(part, Mapping):
                    value = _example_for_schema(part, document, seen=seen)
                    if value is not _NO_EXAMPLE:
                        return value
            return _NO_EXAMPLE

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    schema_type = schema.get("type")
    if schema_type == "object" or (schema_type is None and "properties" in schema):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            required = schema.get("required")
            names = (
                list(required)
                if isinstance(required, list) and required
                else list(properties)[:_MAX_EXAMPLE_PROPERTIES]
            )
            result: dict[str, Any] = {}
            for name in names[:_MAX_EXAMPLE_PROPERTIES]:
                property_schema = properties.get(name)
                if not isinstance(property_schema, Mapping):
                    continue
                value = _example_for_schema(property_schema, document, seen=seen)
                if value is not _NO_EXAMPLE:
                    result[name] = value
            return result
        return {}
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, Mapping):
            value = _example_for_schema(items, document, seen=seen)
            return [value] if value is not _NO_EXAMPLE else []
        return []
    if schema_type == "string":
        schema_format = schema.get("format")
        if schema_format in {"binary", "byte", "password"}:
            return _NO_EXAMPLE
        if isinstance(schema_format, str) and schema_format in _FORMAT_EXAMPLES:
            return _FORMAT_EXAMPLES[schema_format]
        return "string"
    if schema_type == "integer":
        minimum = schema.get("minimum")
        return int(minimum) if isinstance(minimum, int | float) else 0
    if schema_type == "number":
        minimum = schema.get("minimum")
        return float(minimum) if isinstance(minimum, int | float) else 0.0
    if schema_type == "boolean":
        return True
    if schema_type == "null":
        return None
    return _NO_EXAMPLE


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


_DEFAULT_DESCRIPTION: Final[str] = (
    "Covenant Radar's versioned, read-heavy REST API (`spec §R-32`). "
    "Every resource enforces the caller's portfolio scope and permission "
    "set identically whether the caller is a signed-in user or a scoped "
    "API key; a request outside that scope returns `404`, never `403`, so "
    "scope cannot be probed as an enumeration oracle. Every error uses one "
    'envelope: `{"error", "message", "field", "request_id"}`.'
)


__all__ = [
    "DEPRECATION_POLICY",
    "build_openapi_document",
    "validate_openapi_document",
]
