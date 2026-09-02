"""Conditional-request support (`ETag` / `If-None-Match`) for detail reads.

A resource that a person can edit (it carries `VersionedColumns`' `version`)
uses that version as its `ETag`: it changes exactly when the representation
does, which is what an optimistic-concurrency version column already means.
A resource that is written once and never edited (`StandardColumns` only —
`CovenantTest`, `Simulation`, `Forecast`, `AuditEvent`) has no such counter,
but its `id` serves the same purpose: the representation behind a given id
never changes once created, so matching on `id` is exact rather than a
weakened stand-in.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request


def etag_for_version(version: int) -> str:
    """Return the strong `ETag` for a row identified by its version number."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer.")
    return f'"{version}"'


def etag_for_id(row_id: UUID) -> str:
    """Return the strong `ETag` for an immutable, write-once row."""
    if not isinstance(row_id, UUID):
        raise TypeError("row_id must be a UUID.")
    return f'"{row_id}"'


def is_not_modified(request: Request, etag: str) -> bool:
    """Return whether ``If-None-Match`` on ``request`` already matches ``etag``."""
    header = request.headers.get("if-none-match")
    if not header:
        return False
    candidates = {value.strip() for value in header.split(",")}
    return "*" in candidates or etag in candidates


__all__ = ["etag_for_id", "etag_for_version", "is_not_modified"]
