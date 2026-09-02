"""Scoped read-only explainability API (T-072).

This router is the JSON counterpart to ``web.routes.why``.  It resolves the
subject through the caller's portfolio scope before reading trace rows and
then delegates all stage shaping to ``services.explain``.  It intentionally
performs no model/provider work and never recomputes a decision on a read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.api.v1.schemas.explain import ExplainRead
from covenant_radar.audit.trace_reader import ExplainSubjectType, validate_subject_type
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import CovenantTest
from covenant_radar.db.models.forecast import Forecast
from covenant_radar.db.repositories.base import RepositoryBase
from covenant_radar.db.repositories.trace import TraceSubject
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.explain import explain

_READ = requires(Permission.VIEW_BORROWER)
_READ_DEP = Depends(_READ)
_DEFAULT_PREFIX = "/api/v1"

# Subject resolution must happen before the unscoped trace service is called.
# Keeping this registry tied to ExplainSubjectType makes an unsupported
# subject a programming/configuration error rather than a user-controlled
# model lookup.
_SUBJECT_MODELS: Final[Mapping[str, type]] = {
    ExplainSubjectType.COVENANT_TEST.value: CovenantTest,
    ExplainSubjectType.BORROWER.value: Borrower,
    ExplainSubjectType.FORECAST.value: Forecast,
}


def create_explain_router(
    session: Session,
    *,
    prefix: str = _DEFAULT_PREFIX,
) -> APIRouter:
    """Build the protected, scoped explanation resource router."""

    if not is_database_session(session):
        raise TypeError("create_explain_router requires a SQLAlchemy Session.")
    router = APIRouter(prefix=prefix, tags=["explainability"])

    @router.get(
        "/explain/{subject_type}/{subject_id}",
        response_model=ExplainRead,
        name="api_explanation",
    )
    def get_explanation(
        subject_type: str,
        subject_id: str,
        principal: Principal = _READ_DEP,
    ) -> ExplainRead:
        validated_type = _validate_subject_type(subject_type)
        validated_id = _validate_subject_id(subject_id)

        scope = resolve_scope(principal, session)
        model = _SUBJECT_MODELS[validated_type]
        record = RepositoryBase(session, model).get(validated_id, scope=scope)
        if record is None:
            raise NotFound(
                f"{validated_type} {validated_id} was not found within the current scope."
            )

        stages = explain(session, TraceSubject(validated_type, validated_id))
        return ExplainRead.from_stages(
            subject_type=validated_type,
            subject_id=validated_id,
            stages=stages,
        )

    return router


def _validate_subject_type(value: str) -> str:
    try:
        return validate_subject_type(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _validate_subject_id(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError) as error:
        raise HTTPException(
            status_code=400,
            detail="The explanation subject id must be a UUID.",
        ) from error


__all__ = ["create_explain_router"]
