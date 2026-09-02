"""Idempotent local personas used by the full-product demo."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.db.models.identity import AppUser, Role, UserPortfolioScope, UserRole
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.security.passwords import PasswordService

DEMO_PASSWORD: Final[str] = "CovenantRadar#2026"
DEMO_PORTFOLIO_CODE: Final[str] = "REF-PORTFOLIO"


@dataclass(frozen=True, slots=True)
class Persona:
    username: str
    role_code: str
    full_name: str

    @property
    def email(self) -> str:
        return f"{self.username}@localhost"


PERSONAS: Final[tuple[Persona, ...]] = (
    Persona("rm", "relationship_manager", "Relationship Manager"),
    Persona("credit", "credit", "Credit Officer"),
    Persona("approver", "credit_approver", "Credit Approver"),
    Persona("risk", "risk", "Risk Officer"),
    Persona("riskhead", "risk_head", "Risk Head"),
    Persona("auditor", "auditor", "Auditor / Inspector"),
    Persona("admin", "administrator", "Administrator"),
    Persona("steward", "data_steward", "Data Steward"),
)


@dataclass(frozen=True, slots=True)
class PersonaReport:
    created: tuple[str, ...]
    repaired: tuple[str, ...]
    users: dict[str, UUID]


def ensure_demo_personas(
    session: Session,
    *,
    clock: Clock | None = None,
) -> PersonaReport:
    """Create or repair all role-accurate personas in the caller's transaction."""

    if not isinstance(session, Session):
        raise TypeError("ensure_demo_personas requires a SQLAlchemy Session.")
    portfolio = session.scalar(
        select(Portfolio).where(Portfolio.code == DEMO_PORTFOLIO_CODE)
    )
    if portfolio is None:
        raise RuntimeError(
            f"Portfolio {DEMO_PORTFOLIO_CODE!r} not found. Build the reference portfolio first."
        )
    now = (clock or SystemClock()).now()
    password_hash = PasswordService().hash(DEMO_PASSWORD)
    created: list[str] = []
    repaired: list[str] = []
    users: dict[str, UUID] = {}

    for persona in PERSONAS:
        role = session.scalar(select(Role).where(Role.code == persona.role_code))
        if role is None:
            raise RuntimeError(f"Demo role {persona.role_code!r} is missing from reference data.")
        request_id = "demo-user-" + new_request_id()[:30]
        user = session.scalar(select(AppUser).where(AppUser.username == persona.username))
        is_new = user is None
        if user is None:
            user = AppUser(
                username=persona.username,
                email=persona.email,
                full_name=persona.full_name,
                password_hash=password_hash,
                is_active=True,
                auth_source="local",
                external_subject=None,
                mfa_secret_enc=None,
                failed_attempts=0,
                locked_until=None,
                password_changed_at=now,
                must_change_password=False,
                locale="en",
                theme="light",
                created_at=now,
                updated_at=now,
                created_by_id=None,
                updated_by_id=None,
                request_id=request_id,
                version=1,
            )
            session.add(user)
            session.flush()
            created.append(persona.username)
        else:
            changed = False
            if user.password_hash != password_hash:
                user.password_hash = password_hash
                changed = True
            for field, value in (
                ("must_change_password", False),
                ("is_active", True),
                ("failed_attempts", 0),
                ("locked_until", None),
            ):
                if getattr(user, field) != value:
                    setattr(user, field, value)
                    changed = True
            if changed:
                user.updated_at = now
                user.updated_by_id = user.id
                user.request_id = request_id
                user.version += 1
                repaired.append(f"{persona.username} (account)")

        users[persona.username] = user.id
        granted = session.scalar(
            select(UserRole.id).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
        )
        if granted is None:
            session.add(
                UserRole(
                    user_id=user.id,
                    role_id=role.id,
                    granted_at=now,
                    created_at=now,
                    updated_at=now,
                    created_by_id=user.id,
                    updated_by_id=user.id,
                    request_id=request_id,
                )
            )
            if not is_new:
                repaired.append(f"{persona.username} (role)")

        scoped = session.scalar(
            select(UserPortfolioScope.id).where(
                UserPortfolioScope.user_id == user.id,
                UserPortfolioScope.portfolio_id == portfolio.id,
            )
        )
        if scoped is None:
            session.add(
                UserPortfolioScope(
                    user_id=user.id,
                    portfolio_id=portfolio.id,
                    include_descendants=True,
                    created_at=now,
                    updated_at=now,
                    created_by_id=user.id,
                    updated_by_id=user.id,
                    request_id=request_id,
                )
            )
            if not is_new:
                repaired.append(f"{persona.username} (portfolio scope)")
    session.flush()
    return PersonaReport(tuple(created), tuple(repaired), users)


__all__ = [
    "DEMO_PASSWORD",
    "DEMO_PORTFOLIO_CODE",
    "PERSONAS",
    "Persona",
    "PersonaReport",
    "ensure_demo_personas",
]
