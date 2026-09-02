# API deprecation policy

> This text is the single source of truth for the API's deprecation
> commitment. It is embedded verbatim into the generated OpenAPI document as
> `info["x-deprecation-policy"]`
> (`covenant_radar.api.openapi.DEPRECATION_POLICY`), and
> `tests/contract/test_api_contract.py` asserts the two never drift apart.

A resource, field or parameter scheduled for removal is announced in the
release notes at least two minor releases before it is removed, and is
marked `"deprecated": true` in this document for that entire window. A
deprecated element keeps returning identical data until the announced
removal release — a deprecation is never used to silently change behaviour.
Once a removal date is fixed, responses that would use the deprecated
element carry a `Sunset` header naming it. Removal happens only in the
announced release, never earlier.
