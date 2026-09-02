"""Unit tests for T-013's Argon2id password service and policy."""

from __future__ import annotations

import pytest

from covenant_radar.security.passwords import (
    Argon2Parameters,
    PasswordPolicy,
    PasswordPolicyError,
    PasswordService,
    constant_time_compare,
)

pytestmark = pytest.mark.unit


def test_argon2id_parameters() -> None:
    service = PasswordService(
        parameters=Argon2Parameters(time_cost=1, memory_cost=1024, parallelism=1),
        policy=PasswordPolicy(min_length=8),
    )

    encoded = service.hash("Strong-pass-123")

    assert encoded.startswith("$argon2id$")
    assert "$m=1024,t=1,p=1$" in encoded


def test_rehash_on_parameter_change() -> None:
    old = PasswordService(
        parameters=Argon2Parameters(time_cost=1, memory_cost=1024, parallelism=1),
        policy=PasswordPolicy(min_length=8),
    )
    current = PasswordService(
        parameters=Argon2Parameters(time_cost=2, memory_cost=2048, parallelism=1),
        policy=PasswordPolicy(min_length=8),
    )
    encoded = old.hash("Strong-pass-123")

    result = current.verify("Strong-pass-123", encoded)

    assert result.valid
    assert result.needs_rehash


def test_policy_rejects_weak() -> None:
    service = PasswordService(
        parameters=Argon2Parameters(time_cost=1, memory_cost=1024, parallelism=1),
        policy=PasswordPolicy(),
    )

    with pytest.raises(PasswordPolicyError, match="at least 12"):
        service.hash("weak")


def test_constant_time_comparison() -> None:
    assert constant_time_compare("same-value", "same-value")
    assert not constant_time_compare("same-value", "different-value")
    assert constant_time_compare(b"same-value", "same-value")
    assert not constant_time_compare(b"same-value", "different-value")
