"""Liveness and readiness: two different questions with two different
answers (`spec §20`).

A process that is up but cannot reach its database is *healthy* — it did
not crash, its event loop is serving requests — but it is not *ready* to
take traffic that needs that database. Conflating the two causes outages
during deployment: a load balancer that treats "healthy" as "ready" keeps
routing traffic to an instance that cannot serve it. `/health` therefore
never touches a dependency; `/ready` is the sum of independently named
checks, each one of ``ready``, ``not_ready`` or ``not_configured``, so an
operator sees exactly which dependency is missing rather than one
undifferentiated boolean.

``not_configured`` is deliberately not a readiness failure: an optional
capability (SSO, OCR, SMTP, webhooks, the model provider) that a deployment
has chosen not to enable is a configuration choice, not a defect.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum


class ReadinessStatus(str, Enum):
    """The three states a named readiness check can resolve to."""

    READY = "ready"
    NOT_READY = "not_ready"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What one probe found, before it is attributed to a name."""

    status: ReadinessStatus
    detail: str


@dataclass(frozen=True, slots=True)
class NamedCheck:
    """One readiness dependency: a name an operator recognises, and the
    probe that establishes its current state.

    A probe should return ``CheckResult(NOT_READY, ...)`` for an expected
    failure rather than raising, but :func:`evaluate_readiness` still
    isolates an unexpected exception so one broken probe cannot take the
    whole readiness endpoint down with it.
    """

    name: str
    probe: Callable[[], CheckResult]


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One named check's resolved state, ready to serialise."""

    name: str
    status: ReadinessStatus
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """The aggregate `/ready` answer.

    Ready only when no named check is `NOT_READY`; a `NOT_CONFIGURED`
    check never blocks readiness.
    """

    checks: tuple[CheckOutcome, ...]

    @property
    def ready(self) -> bool:
        return all(check.status is not ReadinessStatus.NOT_READY for check in self.checks)

    @property
    def failing(self) -> tuple[str, ...]:
        return tuple(
            check.name for check in self.checks if check.status is ReadinessStatus.NOT_READY
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "checks": {check.name: check.to_dict() for check in self.checks},
        }


def evaluate_readiness(checks: Sequence[NamedCheck]) -> ReadinessReport:
    """Run every named check, isolating one probe's failure from the rest."""
    return ReadinessReport(checks=tuple(_run(check) for check in checks))


def _run(check: NamedCheck) -> CheckOutcome:
    try:
        result = check.probe()
    except Exception as error:  # noqa: BLE001 - a broken probe must not break every other check
        result = CheckResult(ReadinessStatus.NOT_READY, f"{type(error).__name__}: {error}")
    if not isinstance(result, CheckResult):
        raise TypeError(f"Readiness probe {check.name!r} must return a CheckResult.")
    return CheckOutcome(name=check.name, status=result.status, detail=result.detail)


def liveness_status() -> dict[str, object]:
    """Process liveness: if this can return, the process is up.

    Deliberately touches no dependency — that is what `/ready` is for.
    """
    return {"status": "healthy", "healthy": True}


__all__ = [
    "CheckOutcome",
    "CheckResult",
    "NamedCheck",
    "ReadinessReport",
    "ReadinessStatus",
    "evaluate_readiness",
    "liveness_status",
]
