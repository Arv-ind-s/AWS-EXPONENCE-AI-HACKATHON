"""Certificate requirement derivation and grouping (`T-038`, `spec §R-09.a`).

A certificate *requirement* exists wherever the testing calendar (`T-035`)
has opened a `covenant_schedule` occurrence for a covenant version whose
`test_basis` names a borrower/CA certificate as the evidence a test
consumes. `covenant_version.test_basis` is deliberately free text
(`domain/covenants/model.py`'s `_validate_test_basis` only bounds its
length), so this module is what gives one value in that open vocabulary,
:data:`CERTIFICATE_TEST_BASIS`, a fixed meaning the rest of the certificate
workflow can rely on.

**Grouping.** `plan.md §5.6`'s `certificate_request` carries exactly one
`covenant_schedule_id` — the occurrence whose due date anchors the request —
while `covenant_schedule.certificate_id` is the column that actually lets
several occurrences share one request: every grouped occurrence's row points
at the same request, but only one of them is the request's own anchor.
"Several covenants sharing one certificate" (`T-038`'s own wording) is
therefore every occurrence due for the same borrower on the same date, and
the anchor is deterministically the earliest-created of them — sorting by
the UUIDv7 primary key's own bytes needs no extra timestamp field, since a
UUIDv7's high bits already encode creation order.

**Lead time.** A requirement is not actionable ("does the lead time have
to have elapsed" — `R-09.a`) until `due_date - lead_time_days <= as_of`; a
configured lead time that is not shorter than the covenant's own testing
frequency can never be reached at all, and is refused outright rather than
silently never firing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final
from uuid import UUID

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.covenants.cure import FREQUENCY_WINDOW_DAYS

#: The `covenant_version.test_basis` value that marks a covenant whose test
#: consumes a borrower/CA certificate rather than an imported statement or
#: another already-modelled evidence source. Chosen to read naturally beside
#: the free-text values already seen in this codebase's fixtures and seed
#: data ("standalone", "trailing_12m", "reported", "period_end", ...).
CERTIFICATE_TEST_BASIS: Final[str] = "certificate"


def validate_lead_time_days(frequency: str, lead_time_days: int) -> None:
    """Refuse a certificate lead time that cannot fit inside `frequency`.

    A lead time at or beyond the testing interval itself would ask for the
    certificate before the covenant's own previous testing cycle could even
    have closed — there is no date on which raising it would make sense.
    `on_event` has no fixed interval and is therefore never refused here.
    """
    if not isinstance(frequency, str) or not frequency:
        raise ValidationError("covenant_version.frequency is required.", field="frequency")
    if (
        isinstance(lead_time_days, bool)
        or not isinstance(lead_time_days, int)
        or lead_time_days <= 0
    ):
        raise ValidationError(
            "certificate lead_time_days must be a positive integer.", field="lead_time_days"
        )
    if frequency == "on_event":
        return
    window = FREQUENCY_WINDOW_DAYS.get(frequency)
    if window is None:
        raise ValidationError(
            f"Cannot validate certificate lead time for unknown frequency {frequency!r}.",
            field="frequency",
        )
    if lead_time_days >= window:
        raise ValidationError(
            f"The certificate lead time of {lead_time_days} days is not shorter than the "
            f"{frequency} testing frequency ({window} days); it would request a certificate "
            "before the covenant's own previous testing cycle could have closed.",
            field="lead_time_days",
        )


def _validate_date(value: object, name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValidationError(f"{name} must be a calendar date.", field=name)
    return value


def _validate_uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValidationError(f"{name} must be a UUID.", field=name)
    return value


@dataclass(frozen=True, slots=True)
class ScheduleCertificateCandidate:
    """One open `covenant_schedule` occurrence, joined with just enough of
    its owning covenant version and borrower to decide whether it needs a
    certificate request — the pure-domain input the persistence-aware
    service (`services/certificates.py`) resolves from the database.
    """

    schedule_id: UUID
    covenant_version_id: UUID
    borrower_id: UUID
    due_date: date
    frequency: str
    test_basis: str
    existing_certificate_id: UUID | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.schedule_id, "schedule_id")
        _validate_uuid(self.covenant_version_id, "covenant_version_id")
        _validate_uuid(self.borrower_id, "borrower_id")
        _validate_date(self.due_date, "due_date")
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValidationError("frequency must be non-empty text.", field="frequency")
        if not isinstance(self.test_basis, str) or not self.test_basis:
            raise ValidationError("test_basis must be non-empty text.", field="test_basis")
        if self.existing_certificate_id is not None:
            _validate_uuid(self.existing_certificate_id, "existing_certificate_id")


@dataclass(frozen=True, slots=True)
class CertificateRequirement:
    """One actionable certificate requirement: a borrower and due date
    shared by one or more schedule occurrences, ready to be raised (or
    already raised — `covenant_schedule_ids` is the *complete* current
    group regardless of which members are already linked) because its lead
    time has elapsed as of the date the caller supplied.
    """

    borrower_id: UUID
    due_date: date
    raise_on: date
    covenant_schedule_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _validate_uuid(self.borrower_id, "borrower_id")
        _validate_date(self.due_date, "due_date")
        _validate_date(self.raise_on, "raise_on")
        if not self.covenant_schedule_ids:
            raise ValidationError(
                "A certificate requirement must cover at least one schedule occurrence.",
                field="covenant_schedule_ids",
            )
        for schedule_id in self.covenant_schedule_ids:
            _validate_uuid(schedule_id, "covenant_schedule_ids")

    @property
    def anchor_schedule_id(self) -> UUID:
        """The occurrence a new request's own `covenant_schedule_id` names.

        Deterministically the earliest-created member of the group — see
        this module's docstring for why a UUIDv7's own bytes are a safe sort
        key for creation order.
        """
        return self.covenant_schedule_ids[0]


def derive_requirements(
    candidates: Sequence[ScheduleCertificateCandidate],
    *,
    lead_time_days: int,
    as_of: date,
) -> tuple[CertificateRequirement, ...]:
    """Group open certificate-basis occurrences into actionable requirements.

    Every candidate whose `test_basis` is not :data:`CERTIFICATE_TEST_BASIS`
    is ignored outright — it needs no certificate at all. Every remaining
    candidate's frequency is checked against `lead_time_days` before any
    grouping happens, so a configuration that cannot possibly work for a
    covenant actually in the register is refused rather than silently
    producing a requirement for every *other* covenant and skipping just
    that one.
    """
    validated_as_of = _validate_date(as_of, "as_of")
    if not isinstance(candidates, Sequence) or isinstance(candidates, str | bytes):
        raise ValidationError("candidates must be a sequence.", field="candidates")

    certificate_candidates = [
        candidate for candidate in candidates if candidate.test_basis == CERTIFICATE_TEST_BASIS
    ]
    for candidate in certificate_candidates:
        validate_lead_time_days(candidate.frequency, lead_time_days)

    groups: dict[tuple[UUID, date], list[UUID]] = {}
    for candidate in certificate_candidates:
        key = (candidate.borrower_id, candidate.due_date)
        groups.setdefault(key, []).append(candidate.schedule_id)

    requirements: list[CertificateRequirement] = []
    for (borrower_id, due_date), schedule_ids in groups.items():
        raise_on = due_date - timedelta(days=lead_time_days)
        if raise_on > validated_as_of:
            continue
        ordered_ids = tuple(sorted(schedule_ids, key=lambda value: value.bytes))
        requirements.append(
            CertificateRequirement(
                borrower_id=borrower_id,
                due_date=due_date,
                raise_on=raise_on,
                covenant_schedule_ids=ordered_ids,
            )
        )
    return tuple(
        sorted(
            requirements,
            key=lambda requirement: (
                requirement.due_date,
                requirement.borrower_id.bytes,
                requirement.anchor_schedule_id.bytes,
            ),
        )
    )


__all__ = [
    "CERTIFICATE_TEST_BASIS",
    "CertificateRequirement",
    "ScheduleCertificateCandidate",
    "derive_requirements",
    "validate_lead_time_days",
]
