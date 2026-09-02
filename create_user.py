#!/usr/bin/env python3
"""Create the spec §7 demo personas used by the local demo.

One user per role in `spec.md §7`, not a single super-user.  Two things make
this the shape the demo needs:

*   **Permissions are real.**  The `administrator` role deliberately holds
    view and administration rights only — no `RUN_INTAKE`, `RUN_SIMULATION`,
    `GENERATE_MEMO` or `UPDATE_CASE`.  A demo driven from one administrator
    account therefore cannot reach intake or the simulator at all, and those
    nav links never render.  That is correct RBAC, so the demo needs the
    roles that *can* act.
*   **Maker-checker needs two people.**  `credit` registers a covenant and
    `credit_approver` approves it.  The distinct-actor rule is enforced in
    the database, so it cannot be demonstrated from a single login.

Every persona is scoped to the reference portfolio.  This is not optional
decoration: `user_portfolio_scope`'s absence for a user means *no access,
never all* (`db/models/identity.py`), so a persona without a scope row sees
an empty queue, an empty borrower list and empty covenants no matter which
permissions the role carries.

The script is idempotent and repairs as well as creates: an existing user
missing its role grant or portfolio scope is completed in place rather than
duplicated.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from covenant_radar.config.settings import get_settings
from covenant_radar.core.clock import SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.db.models.identity import AppUser, Role, UserPortfolioScope, UserRole
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.security.passwords import PasswordService

#: Shared across every demo persona so a presenter types it once.  This is a
#: disposable local showcase credential and is documented in the README for
#: that reason; change it before exposing the app beyond localhost.
DEMO_PASSWORD = "CovenantRadar#2026"

PORTFOLIO_CODE = "REF-PORTFOLIO"


@dataclass(frozen=True, slots=True)
class Persona:
    """One demo login, bound to exactly one `spec §7` role."""

    username: str
    role_code: str
    full_name: str

    @property
    def email(self) -> str:
        return f"{self.username}@localhost"


#: Ordered as `spec §7`'s table reads, so the demo runbook and this list stay
#: comparable line by line.
PERSONAS: tuple[Persona, ...] = (
    Persona("rm", "relationship_manager", "Relationship Manager"),
    Persona("credit", "credit", "Credit Officer"),
    Persona("approver", "credit_approver", "Credit Approver"),
    Persona("risk", "risk", "Risk Officer"),
    Persona("riskhead", "risk_head", "Risk Head"),
    Persona("auditor", "auditor", "Auditor / Inspector"),
    Persona("admin", "administrator", "Administrator"),
    Persona("steward", "data_steward", "Data Steward"),
)


def ensure_demo_personas() -> None:
    """Create or repair every demo persona and its reference-portfolio scope."""

    settings = get_settings()
    engine = create_engine(settings.database.url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    passwords = PasswordService()
    password_hash = passwords.hash(DEMO_PASSWORD)
    created: list[str] = []
    repaired: list[str] = []

    try:
        with session_factory() as session:
            with session.begin():
                portfolio = session.scalar(
                    select(Portfolio).where(Portfolio.code == PORTFOLIO_CODE)
                )
                if portfolio is None:
                    raise RuntimeError(
                        f"Portfolio {PORTFOLIO_CODE!r} not found. Run "
                        "`radarctl seed --reference-portfolio` first."
                    )

                for persona in PERSONAS:
                    role = session.scalar(select(Role).where(Role.code == persona.role_code))
                    if role is None:
                        raise RuntimeError(
                            f"Role {persona.role_code!r} not found. Run `radarctl seed` first."
                        )

                    now = SystemClock().now()
                    request_id = new_request_id()
                    user = session.scalar(
                        select(AppUser).where(AppUser.username == persona.username)
                    )
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
                            # A forced password change on first sign-in would
                            # stop the demo before it reached a single screen.
                            must_change_password=False,
                            locale="en",
                            theme="light",
                            created_at=now,
                            updated_at=now,
                            request_id=request_id,
                        )
                        session.add(user)
                        session.flush()
                        created.append(persona.username)
                    else:
                        # Repair an account left half-built by an earlier run:
                        # reset the known demo password and clear any lockout
                        # so the documented credentials always work.
                        user.password_hash = password_hash
                        user.must_change_password = False
                        user.is_active = True
                        user.failed_attempts = 0
                        user.locked_until = None
                        user.updated_at = now

                    granted = session.scalar(
                        select(UserRole.id).where(
                            UserRole.user_id == user.id,
                            UserRole.role_id == role.id,
                        )
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
    finally:
        engine.dispose()

    write = sys.stdout.write
    write(f"Demo personas ready (password: {DEMO_PASSWORD})\n")
    for persona in PERSONAS:
        write(f"  {persona.username:<10} {persona.role_code:<22} {persona.full_name}\n")
    if created:
        write(f"\nCreated: {', '.join(created)}\n")
    if repaired:
        write(f"Repaired: {', '.join(repaired)}\n")
    if not created and not repaired:
        write("\nAll personas already present and correctly scoped.\n")


if __name__ == "__main__":
    ensure_demo_personas()
