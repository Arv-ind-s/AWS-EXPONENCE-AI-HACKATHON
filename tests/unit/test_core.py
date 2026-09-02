"""Tests for the core primitives: errors, ids, clock, money, context, logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import structlog

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.context import bind_request_id, get_request_id
from covenant_radar.core.errors import (
    ERROR_CLASSES,
    AuthorizationError,
    Conflict,
    DomainError,
    ExternalServiceError,
    NotFound,
    ValidationError,
)
from covenant_radar.core.ids import human_reference, new_id
from covenant_radar.core.money import Money
from covenant_radar.observability.logging import configure


def test_error_codes_unique() -> None:
    codes = [error_class.code for error_class in ERROR_CLASSES]

    assert len(codes) == len(set(codes))
    assert len(codes) == 6


@pytest.mark.parametrize(
    "error_class", [ValidationError, AuthorizationError, NotFound, Conflict, ExternalServiceError]
)
def test_error_subclasses_are_domain_errors(error_class: type[DomainError]) -> None:
    error = error_class("something went wrong", field="borrower.reference")

    assert isinstance(error, DomainError)
    assert error.message == "something went wrong"
    assert error.field == "borrower.reference"
    assert str(error) == "something went wrong"


def test_uuid7_monotonic() -> None:
    identifiers = [new_id() for _ in range(500)]

    assert len(identifiers) == len(set(identifiers))
    assert identifiers == sorted(identifiers, key=lambda identifier: identifier.int)
    assert all(identifier.version == 7 for identifier in identifiers)


def test_human_reference_format() -> None:
    assert human_reference("B", 123) == "B-000123"
    assert human_reference("CV", 456) == "CV-000456"
    assert human_reference("F", 1234567) == "F-1234567"


def test_human_reference_rejects_non_positive_sequence() -> None:
    with pytest.raises(ValueError, match="positive sequence"):
        human_reference("B", 0)


def test_fixed_clock_deterministic() -> None:
    instant = datetime(2026, 8, 30, 6, 30, tzinfo=UTC)
    clock = FixedClock(instant)

    assert clock.now() == instant
    assert clock.now() == instant

    clock.advance(timedelta(days=1))
    assert clock.now() == instant + timedelta(days=1)


def test_fixed_clock_refuses_naive_instant() -> None:
    naive_instant = datetime(2026, 8, 30, 6, 30)  # noqa: DTZ001 -- the defect under test

    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(naive_instant)


def test_money_refuses_float() -> None:
    with pytest.raises(TypeError, match="1.5"):
        Money(1.5, "INR")  # type: ignore[arg-type]


def test_money_refuses_cross_unit_arithmetic() -> None:
    rupees = Money(Decimal("100.00"), "INR")
    dollars = Money(Decimal("100.00"), "USD")

    with pytest.raises(ValueError, match="different units"):
        rupees + dollars


def test_money_arithmetic_within_one_unit() -> None:
    limit = Money(Decimal("1000.00"), "INR")
    drawn = Money(Decimal("400.00"), "INR")

    assert (limit - drawn) == Money(Decimal("600.00"), "INR")
    assert (drawn + drawn) == Money(Decimal("800.00"), "INR")
    assert limit > drawn
    assert (limit * 2) == Money(Decimal("2000.00"), "INR")


def test_money_formatted_uses_indian_grouping() -> None:
    assert Money(Decimal("1234567.8"), "INR").formatted() == "₹12,34,567.80"
    assert Money(Decimal("999"), "INR").formatted() == "₹999.00"


def test_log_carries_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure()

    with bind_request_id("rq-abcdef0123456789"):
        structlog.get_logger("test").info("covenant.evaluated")

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["request_id"] == "rq-abcdef0123456789"
    assert payload["event"] == "covenant.evaluated"


def test_log_redacts_secret_pattern(capsys: pytest.CaptureFixture[str]) -> None:
    configure()

    structlog.get_logger("test").info("login.attempted", password="hunter2")

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["password"] != "hunter2"
    assert "hunter2" not in line


def test_log_outside_context_does_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    configure()

    assert get_request_id() is None
    structlog.get_logger("test").info("startup.completed")

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["request_id"] is None
