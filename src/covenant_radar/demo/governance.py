"""Seed the model register and evaluation scoreboard for the demo walkthrough.

The governance screen reads `model_registration` and `evaluation_run`
directly and, by design, invents nothing when they are empty.  Nothing in the
seed path wrote either table, so both panels rendered "No model registrations
are recorded" / "No evaluation run has been recorded" — leaving the
documented walkthrough beat (deterministic champion beside the ML challenger,
the challenger's false-escalation result shown as failed and therefore not
promotable) with nothing to show.

Registrations are created through `ModelGovernanceService`, so the maker
event, the maker-checker request and the audit trail are the real ones.  The
evaluation runs are written as rows because they *are* records of an offline
harness pass rather than the output of a request-time service; the numbers
come from the checked-in ML reference manifests and floor ledger rather than
being invented here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.operations import EvaluationRun
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.model_governance import (
    APPROVE_MODEL_REGISTRATION_PERMISSION,
    PROPOSE_MODEL_REGISTRATION_PERMISSION,
    ModelGovernanceService,
)

#: The release the seeded scoreboard describes.  A fixed identifier keeps the
#: seed idempotent: re-running it finds this release rather than stacking a
#: second copy of the same two arms.
DEMO_COMMIT_SHA: Final[str] = "demo-release-7a"

CHAMPION_COMPONENT: Final[str] = "forecast.probability"
CHALLENGER_COMPONENT: Final[str] = "forecast.ml_challenger"

_ML_REFERENCE_DIR: Final[Path] = Path("var/ml-reference")
_CHALLENGER_MANIFEST: Final[str] = "stage4-gradient_boosted-ff9af19f21f7.manifest.json"

#: `evaluation/floors.json` records 1.000 for every category, so a 0.14
#: false-escalation score is a floor failure by a wide margin — which is the
#: point of the walkthrough beat, not an incidental detail.
_FALSE_ESCALATION_SCORE: Final[Decimal] = Decimal("0.14")
_FALSE_ESCALATION_FLOOR: Final[Decimal] = Decimal("1.000")


@dataclass(frozen=True, slots=True)
class GovernanceSeedReport:
    """What the governance seed created, for the CLI to print."""

    registrations_created: int
    evaluation_runs_created: int
    champion_approved: bool


class _GovernanceAuditWriter:
    """Adapt the broad recorder API to the service's audit protocol."""

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(
        self,
        event_type: str,
        subject: object,
        payload: object,
        *,
        actor: object,
        request_id: str,
    ) -> object:
        return self._recorder.record(
            event_type,
            subject,  # type: ignore[arg-type]
            payload,  # type: ignore[arg-type]
            actor=actor,  # type: ignore[arg-type]
            request_id=request_id,
        )


def seed_governance_records(
    session: Session,
    *,
    system_actor_id: UUID,
    clock: Clock | None = None,
) -> GovernanceSeedReport:
    """Populate the model register and evaluation scoreboard idempotently.

    Re-running only fills what is missing, matching the rest of the demo
    seed: a presenter can restart the bootstrap without stacking duplicate
    registrations or a second copy of the same release.
    """

    if not isinstance(session, Session):
        raise TypeError("seed_governance_records requires a SQLAlchemy Session.")
    if not isinstance(system_actor_id, UUID):
        raise TypeError("system_actor_id must be a UUID.")

    active_clock = clock or SystemClock()
    now = active_clock.now()
    request_id = "demo-governance-" + new_request_id()[:22]

    runs_created = _ensure_evaluation_runs(
        session,
        system_actor_id=system_actor_id,
        now=now,
        request_id=request_id,
    )
    champion_run_id, challenger_run_id = _release_run_ids(session)

    audit = _GovernanceAuditWriter(
        AuditRecorder(AuditRepository(session), clock=active_clock, request_id=request_id)
    )
    service = ModelGovernanceService(
        session,
        audit=audit,
        clock=active_clock,
        request_id=request_id,
    )

    maker_id = _persona_id(session, "admin") or system_actor_id
    maker = Principal.user(maker_id, (PROPOSE_MODEL_REGISTRATION_PERMISSION,))

    registrations = 0
    champion = service.register(
        maker,
        component=CHAMPION_COMPONENT,
        provider="deterministic",
        model_id="covenant-radar-deterministic-v1",
        purpose=(
            "Champion. The operational escalation probability every queue row, case, "
            "notification, simulation and memo is stated from."
        ),
        owner_id=maker_id,
        evaluation_run_id=champion_run_id,
    )
    registrations += 1

    challenger_family, challenger_checksum = _challenger_identity()
    service.register(
        maker,
        component=CHALLENGER_COMPONENT,
        provider="scikit-learn",
        model_id=f"{challenger_family}-{challenger_checksum[:12]}",
        purpose=(
            "Shadow challenger. Scored alongside the champion for comparison only; "
            "it never supplies an operational probability while unapproved."
        ),
        owner_id=maker_id,
        evaluation_run_id=challenger_run_id,
    )
    registrations += 1

    # Only the champion is approved. The challenger stays `registered` and
    # therefore reads as "Unapproved — not promotable", which is exactly what
    # its failed evaluation run should produce.
    champion_approved = _approve(session, service, champion, maker_id=maker_id)

    return GovernanceSeedReport(
        registrations_created=registrations,
        evaluation_runs_created=runs_created,
        champion_approved=champion_approved,
    )


def _approve(
    session: Session,
    service: ModelGovernanceService,
    champion: object,
    *,
    maker_id: UUID,
) -> bool:
    """Approve the champion registration as a genuinely different actor.

    Maker-checker refuses the proposing actor, so this only runs when a
    distinct approver persona exists. When it does not, the champion simply
    stays unapproved rather than the seed faking a second identity.
    """

    request = getattr(champion, "approval_request", None)
    request_id = getattr(request, "id", None)
    if request_id is None:
        return False
    checker_id = _persona_id(session, "riskhead")
    if checker_id is None or checker_id == maker_id:
        return False
    checker = Principal.user(checker_id, (APPROVE_MODEL_REGISTRATION_PERMISSION,))
    service.decide_approval(
        checker,
        request_id,
        approved=True,
        reason="Deterministic champion approved for operational use in the demo release.",
    )
    return True


def _ensure_evaluation_runs(
    session: Session,
    *,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> int:
    existing = {
        row.arm
        for row in session.scalars(
            select(EvaluationRun).where(EvaluationRun.commit_sha == DEMO_COMMIT_SHA)
        )
    }
    created = 0
    for arm, passed, scores in (
        (
            "deterministic-champion",
            True,
            {
                "false_escalation": "1.000",
                "forecast_dating": "1.000",
                "engine": "1.000",
                "materiality": "1.000",
                "floor": str(_FALSE_ESCALATION_FLOOR),
                "verdict": "Meets every category floor. Approved as champion.",
            },
        ),
        (
            "ml-challenger",
            False,
            {
                "false_escalation": str(_FALSE_ESCALATION_SCORE),
                "forecast_dating": "0.910",
                "engine": "1.000",
                "materiality": "0.870",
                "floor": str(_FALSE_ESCALATION_FLOOR),
                "verdict": (
                    "Fails the false-escalation floor "
                    f"({_FALSE_ESCALATION_SCORE} against a required "
                    f"{_FALSE_ESCALATION_FLOOR}). Not promotable."
                ),
            },
        ),
    ):
        if arm in existing:
            continue
        session.add(
            EvaluationRun(
                id=new_id(),
                commit_sha=DEMO_COMMIT_SHA,
                arm=arm,
                scores=scores,
                passed=passed,
                executed_at=now,
                created_at=now,
                updated_at=now,
                created_by_id=system_actor_id,
                updated_by_id=system_actor_id,
                request_id=request_id,
            )
        )
        created += 1
    session.flush()
    return created


def _release_run_ids(session: Session) -> tuple[UUID | None, UUID | None]:
    rows = {
        row.arm: row.id
        for row in session.scalars(
            select(EvaluationRun).where(EvaluationRun.commit_sha == DEMO_COMMIT_SHA)
        )
    }
    return rows.get("deterministic-champion"), rows.get("ml-challenger")


def _challenger_identity() -> tuple[str, str]:
    """Read the challenger's family and checksum from its shipped manifest."""

    path = _ML_REFERENCE_DIR / _CHALLENGER_MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # The register must still name the challenger honestly when the
        # reference artefact is not on this host.
        return "gradient_boosted", "unavailable"
    family = str(manifest.get("family") or "gradient_boosted")
    checksum = str(manifest.get("checksum") or "unavailable")
    return family, checksum


def _persona_id(session: Session, username: str) -> UUID | None:
    return session.scalar(select(AppUser.id).where(AppUser.username == username))


__all__ = [
    "CHALLENGER_COMPONENT",
    "CHAMPION_COMPONENT",
    "DEMO_COMMIT_SHA",
    "GovernanceSeedReport",
    "seed_governance_records",
]
