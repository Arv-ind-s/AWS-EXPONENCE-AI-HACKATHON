"""Unit coverage for `T-038`'s certificate requirement derivation and
grouping (`domain.certificates.requirements`)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.certificates.requirements import (
    CERTIFICATE_TEST_BASIS,
    ScheduleCertificateCandidate,
    derive_requirements,
    validate_lead_time_days,
)

_BORROWER = uuid4()
_DUE_DATE = date(2026, 6, 30)
_AS_OF = date(2026, 6, 20)


def _candidate(
    *,
    schedule_id=None,
    covenant_version_id=None,
    borrower_id=_BORROWER,
    due_date=_DUE_DATE,
    frequency="quarterly",
    test_basis=CERTIFICATE_TEST_BASIS,
    existing_certificate_id=None,
) -> ScheduleCertificateCandidate:
    return ScheduleCertificateCandidate(
        schedule_id=schedule_id or uuid4(),
        covenant_version_id=covenant_version_id or uuid4(),
        borrower_id=borrower_id,
        due_date=due_date,
        frequency=frequency,
        test_basis=test_basis,
        existing_certificate_id=existing_certificate_id,
    )


def test_covenants_grouped_into_one_request() -> None:
    first = _candidate()
    second = _candidate()
    third_borrower_two = _candidate(borrower_id=uuid4())
    fourth_other_date = _candidate(due_date=date(2026, 9, 30))

    requirements = derive_requirements(
        [first, second, third_borrower_two, fourth_other_date],
        lead_time_days=10,
        as_of=_AS_OF,
    )

    matching = [
        requirement
        for requirement in requirements
        if requirement.borrower_id == _BORROWER and requirement.due_date == _DUE_DATE
    ]
    assert len(matching) == 1
    requirement = matching[0]
    assert set(requirement.covenant_schedule_ids) == {first.schedule_id, second.schedule_id}
    # The anchor is deterministic (earliest by UUID bytes), not incidental.
    assert requirement.anchor_schedule_id == min(
        (first.schedule_id, second.schedule_id), key=lambda value: value.bytes
    )


def test_non_certificate_basis_produces_no_requirement() -> None:
    candidate = _candidate(test_basis="standalone")

    requirements = derive_requirements([candidate], lead_time_days=10, as_of=_AS_OF)

    assert requirements == ()


def test_lead_time_not_yet_elapsed_excluded() -> None:
    candidate = _candidate()

    requirements = derive_requirements(
        [candidate], lead_time_days=5, as_of=date(2026, 6, 1)
    )

    assert requirements == ()


def test_lead_time_elapsed_included_on_exact_boundary() -> None:
    candidate = _candidate()

    requirements = derive_requirements(
        [candidate], lead_time_days=10, as_of=date(2026, 6, 20)
    )

    assert len(requirements) == 1
    assert requirements[0].raise_on == date(2026, 6, 20)


def test_lead_time_longer_than_frequency_refused() -> None:
    with pytest.raises(ValidationError, match="lead time"):
        validate_lead_time_days("quarterly", 90)

    with pytest.raises(ValidationError, match="lead time"):
        validate_lead_time_days("monthly", 30)


def test_lead_time_shorter_than_frequency_accepted() -> None:
    validate_lead_time_days("quarterly", 14)
    validate_lead_time_days("monthly", 7)
    validate_lead_time_days("annual", 30)


def test_lead_time_on_event_never_refused() -> None:
    validate_lead_time_days("on_event", 10_000)


def test_derive_requirements_refuses_bad_lead_time_for_any_candidate() -> None:
    quarterly = _candidate(frequency="quarterly")

    with pytest.raises(ValidationError, match="lead time"):
        derive_requirements([quarterly], lead_time_days=90, as_of=_AS_OF)


def test_lead_time_must_be_a_positive_integer() -> None:
    with pytest.raises(ValidationError):
        validate_lead_time_days("quarterly", 0)
    with pytest.raises(ValidationError):
        validate_lead_time_days("quarterly", -1)
    with pytest.raises(ValidationError):
        validate_lead_time_days("quarterly", True)  # bool is not an accepted int


def test_requirement_ordering_is_deterministic() -> None:
    later = _candidate(due_date=date(2026, 9, 30))
    earlier = _candidate(due_date=date(2026, 3, 31))

    requirements = derive_requirements(
        [later, earlier], lead_time_days=10, as_of=date(2026, 9, 30)
    )

    assert [requirement.due_date for requirement in requirements] == [
        date(2026, 3, 31),
        date(2026, 9, 30),
    ]
