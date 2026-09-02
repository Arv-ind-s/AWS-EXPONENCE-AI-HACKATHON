"""The why-panel's single data source across every stage (`T-070`).

``explain`` is deliberately unauthenticated and unscoped. ``db/repositories
/trace.py``'s own docstring is explicit that resolving and authorising the
subject is the job of whatever exposes a trace externally — here, the ``GET
/why/{subject_type}/{subject_id}`` route (`C-10`) that `T-071`/`T-072` add.
That route resolves the underlying covenant test, borrower or forecast in
the caller's scope, and only then reaches this function. Duplicating a
second, looser authorisation path here would let the two drift apart, which
is exactly what `db/repositories/trace.py` was written to prevent.

What this module does own is the one shape check that has nothing to do
with authorisation: whether ``subject_type`` names anything the why-panel
knows how to explain at all.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from covenant_radar.audit.trace_reader import ExplainStage, present, validate_subject_type
from covenant_radar.core.errors import ValidationError
from covenant_radar.db.repositories.trace import TraceRepository, TraceSubject
from covenant_radar.db.session import is_database_session


def explain(
    session: Session,
    subject: TraceSubject | tuple[str, UUID],
) -> tuple[ExplainStage, ...]:
    """Return every stage for ``subject``, in order, named and padded.

    ``subject`` must name one of ``ExplainSubjectType``'s known why-panel
    subjects; anything else is refused with a ``ValidationError`` naming the
    valid types before any query is built. The result always has one entry
    per stage — a subject with no history at all still returns every stage
    marked ``not_run``, never an empty result.
    """

    if not is_database_session(session):
        raise TypeError("explain() requires a SQLAlchemy Session.")
    validated_subject = _validated_subject(subject)
    repository = TraceRepository(session)
    records = repository.read(validated_subject)
    return present(records)


def _validated_subject(subject: TraceSubject | tuple[str, UUID]) -> TraceSubject:
    if isinstance(subject, TraceSubject):
        subject_type, subject_id = subject.subject_type, subject.subject_id
    elif isinstance(subject, tuple) and len(subject) == 2:
        subject_type, subject_id = subject
    else:
        raise TypeError(
            "explain() subject must be a TraceSubject or a (subject_type, subject_id) pair."
        )
    try:
        validated_type = validate_subject_type(subject_type)
    except ValueError as error:
        raise ValidationError(str(error), field="subject_type") from error
    return TraceSubject(validated_type, subject_id)


__all__ = ["explain"]
