"""Request and job context: the identifiers that stitch one causal chain
together across every log line, audit event, trace row and model-call
record it produces.

A `request_id` (``rq-`` plus 16 hex) is minted once per inbound HTTP
request; a `job_run_id` (``jr-`` plus 16 hex) is minted once per scheduled
job run. Both are ordinary `ContextVar`s: reading one outside any bound
scope returns `None` rather than raising, so a log call made before a
request context exists — at startup, in a script, in a test — still works.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_REQUEST_ID_HEX_LENGTH = 16
_JOB_RUN_ID_HEX_LENGTH = 16

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_job_run_id: ContextVar[str | None] = ContextVar("job_run_id", default=None)


def new_request_id() -> str:
    """Mint a request identifier in the ``rq-`` plus 16 hex shape."""
    return f"rq-{secrets.token_hex(_REQUEST_ID_HEX_LENGTH // 2)}"


def new_job_run_id() -> str:
    """Mint a job-run identifier in the same shape, prefixed ``jr-``."""
    return f"jr-{secrets.token_hex(_JOB_RUN_ID_HEX_LENGTH // 2)}"


def get_request_id() -> str | None:
    """The current request id, or `None` outside any bound request."""
    return _request_id.get()


def get_job_run_id() -> str | None:
    """The current job-run id, or `None` outside any bound job run."""
    return _job_run_id.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    """Bind `request_id` for the lifetime of the enclosed block."""
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


@contextmanager
def bind_job_run_id(job_run_id: str) -> Iterator[None]:
    """Bind `job_run_id` for the lifetime of the enclosed block."""
    token = _job_run_id.set(job_run_id)
    try:
        yield
    finally:
        _job_run_id.reset(token)
