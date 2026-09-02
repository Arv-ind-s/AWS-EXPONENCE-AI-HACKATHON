# Covenant Radar API reference

Covenant Radar exposes a versioned, read-heavy REST API under `/api/v1`
(`spec §R-32`). This directory documents how the API is authenticated, how
its OpenAPI document is produced, and its deprecation policy. The OpenAPI
document itself is generated on demand and is never committed here — a
generated artefact must never be committed to the repository, and a
checked-in copy would drift from the implementation the moment a route
changed without someone remembering to regenerate it.

## Authentication

Every request resolves to a principal or is refused with `401`
(`plan.md §1`, "Identity"). Two credential kinds are accepted, interchangeably,
on every route:

- **A browser session** — the signed, HttpOnly cookie issued at sign-in.
- **A scoped API key** — `Authorization: Bearer <key>`, issued by an
  administrator (`covenant_radar.services.api_keys.ApiKeyService`). A key
  carries its own permission scopes and portfolio scope, exactly like a
  user; a key scoped to one portfolio cannot read another's data through any
  endpoint, and a request outside its scope returns `404`, never `403`
  (`R-32.b`; scope is never an enumeration oracle).

A key is shown once, at issue or rotation, and is never retrievable or
logged again — only its display prefix and a SHA-256 digest persist. Each
key also carries its own `rate_limit_per_min`; exceeding it returns `429`
with a `Retry-After` header, independent of the coarser, IP-keyed API rate
limit applied to every caller.

## Pagination, filtering and conditional requests

List resources use opaque, HMAC-signed cursor pagination
(`covenant_radar.api.pagination`); a cursor from a different filter set is
refused with `422` rather than silently reinterpreted. Detail resources
support `If-None-Match` and return `304` when the caller's cached
representation is current.

## The error envelope

Every error — a deliberately raised domain error, a raw HTTP exception, or a
request validation failure — is reshaped into one JSON body:

```json
{"error": "not_found", "message": "...", "field": null, "request_id": "rq-..."}
```

## Regenerating the OpenAPI document

The document is derived from the live application's registered routes, so
it is generated from a running `FastAPI` instance rather than maintained by
hand:

```python
from covenant_radar import __version__
from covenant_radar.api.openapi import build_openapi_document, validate_openapi_document

document = build_openapi_document(app, version=__version__)
assert validate_openapi_document(document) == []
```

`tests/contract/test_api_contract.py` runs the same generation against the
application's actual route table on every commit, in both directions: every
registered route appears in the generated document, and every document
entry corresponds to a real route. A document that disagrees with the
implementation in either direction is a contract-test failure, never a
silent drift (`R-32.a`).

## Deprecation policy

See [`deprecation-policy.md`](deprecation-policy.md). The same text is
embedded in the generated document as `info["x-deprecation-policy"]`, so an
integrator reading the document programmatically sees the identical
commitment.
