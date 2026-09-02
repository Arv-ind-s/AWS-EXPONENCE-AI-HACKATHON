"""Unit tests for the identity and structure tables (`T-007`):
`plan.md §5.1` copied exactly, the materialised portfolio path, and the
distinct-actor constraint that makes maker-checker impossible to bypass
in the database.

Every test runs against a real in-memory SQLite database — the same
technique `tests/unit/test_db_base.py` (`T-006`) established — so this
file stays fast and network-free; the schema is proven again against a
real PostgreSQL instance once `tests/integration` exercises these models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.errors import ValidationError
from covenant_radar.db.base import Base
from covenant_radar.db.models.identity import (
    ApiKey,
    AppUser,
    Permission,
    Role,
    RolePermission,
    UserPortfolioScope,
    UserRole,
    UserSession,
)
from covenant_radar.db.models.maker_checker import MakerCheckerRequest
from covenant_radar.db.models.organisation import Organisation
from covenant_radar.db.models.portfolio import MAX_PORTFOLIO_DEPTH, Portfolio

_MODEL_TABLES = [
    Organisation.__table__,
    Portfolio.__table__,
    AppUser.__table__,
    Role.__table__,
    Permission.__table__,
    RolePermission.__table__,
    UserRole.__table__,
    UserPortfolioScope.__table__,
    UserSession.__table__,
    ApiKey.__table__,
    MakerCheckerRequest.__table__,
]

# StandardColumns (`db/base.py`) carried by every table, plus the
# foreign-keyed overrides `identity.UserAttributedColumns` adds on top —
# every T-007 table mixes both in, so every table's column set includes
# these six.
_STANDARD_COLUMNS = {
    "id",
    "created_at",
    "updated_at",
    "created_by_id",
    "updated_by_id",
    "request_id",
}

# `plan.md §5.1`'s "Key fields" per table, copied exactly.
_PLAN_FIELDS: dict[str, set[str]] = {
    "organisation": {"name", "short_code", "regulatory_id", "fiscal_year_start_month"},
    "portfolio": {"code", "name", "parent_id", "branch_code", "path"},
    "app_user": {
        "username",
        "email",
        "full_name",
        "password_hash",
        "auth_source",
        "external_subject",
        "is_active",
        "mfa_secret_enc",
        "failed_attempts",
        "locked_until",
        "password_changed_at",
        "must_change_password",
        "locale",
        "theme",
    },
    "role": {"code", "name", "is_system"},
    "permission": {"code", "description"},
    "role_permission": {"role_id", "permission_id"},
    "user_role": {"user_id", "role_id", "granted_by_id", "granted_at"},
    "user_portfolio_scope": {"user_id", "portfolio_id", "include_descendants"},
    "user_session": {
        "user_id",
        "token_hash",
        "issued_at",
        "last_seen_at",
        "expires_at",
        "absolute_expires_at",
        "ip_hash",
        "user_agent_hash",
        "revoked_at",
    },
    "api_key": {
        "name",
        "key_hash",
        "prefix",
        "scopes",
        "portfolio_scope",
        "rate_limit_per_min",
        "expires_at",
        "last_used_at",
        "revoked_at",
    },
    "maker_checker_request": {
        "subject_type",
        "subject_id",
        "operation",
        "payload",
        "maker_id",
        "checker_id",
        "state",
        "decided_at",
        "reason",
    },
}

# Tables carrying `VersionedColumns` — the user-editable entities
# (`plan.md §5`'s convention), as opposed to pure joins, seeded reference
# data, or system-managed rows refreshed on effectively every request.
_VERSIONED_TABLES = {
    "organisation",
    "portfolio",
    "app_user",
    "role",
    "user_portfolio_scope",
    "api_key",
    "maker_checker_request",
}

_MODELS_BY_TABLE = {table.name: table for table in _MODEL_TABLES}


def _sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_MODEL_TABLES)
    return engine


def _now() -> datetime:
    return datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)


def _request_id(suffix: str) -> str:
    return f"rq-{suffix:0>16}"


def test_all_columns_match_plan() -> None:
    for table_name, plan_fields in _PLAN_FIELDS.items():
        table = _MODELS_BY_TABLE[table_name]
        expected = set(_STANDARD_COLUMNS) | plan_fields
        if table_name in _VERSIONED_TABLES:
            expected.add("version")

        actual = {column.name for column in table.columns}
        assert actual == expected, (
            f"{table_name}: expected {sorted(expected)}, got {sorted(actual)}"
        )


def test_username_unique() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        common_kwargs = {
            "full_name": "A User",
            "created_at": _now(),
            "updated_at": _now(),
            "request_id": _request_id("1"),
        }
        session.add(AppUser(username="jdoe", email="jdoe@example.com", **common_kwargs))
        session.add(AppUser(username="jdoe", email="jdoe2@example.com", **common_kwargs))
        with pytest.raises(IntegrityError):
            session.commit()


def test_portfolio_path_maintained_on_move() -> None:
    root = Portfolio.create(
        code="ROOT",
        name="Root",
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("1"),
    )
    child = Portfolio.create(
        code="CHILD",
        name="Child",
        parent=root,
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("2"),
    )
    grandchild = Portfolio.create(
        code="GRANDCHILD",
        name="Grandchild",
        parent=child,
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("3"),
    )

    assert root.path == f"{root.id.hex}/"
    assert child.parent_id == root.id
    assert child.path == f"{root.path}{child.id.hex}/"
    assert grandchild.path == f"{child.path}{grandchild.id.hex}/"

    other_root = Portfolio.create(
        code="OTHER",
        name="Other root",
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("4"),
    )
    child.move_to(other_root, descendants=[grandchild])

    assert child.parent_id == other_root.id
    assert child.path == f"{other_root.path}{child.id.hex}/"
    assert grandchild.path == f"{child.path}{grandchild.id.hex}/"

    # Moving the child back to being a root works too, and a descendant
    # not passed in `descendants` is left alone rather than guessed at.
    child.move_to(None, descendants=[])
    assert child.parent_id is None
    assert child.path == f"{child.id.hex}/"

    # A move that would push a descendant past the configured maximum
    # depth is refused before anything is mutated. `root` is already at
    # depth 1, so `MAX_PORTFOLIO_DEPTH - 1` more levels lands exactly at
    # the limit — one level further is what the move must refuse.
    deep_chain = [root]
    for index in range(MAX_PORTFOLIO_DEPTH - 1):
        deep_chain.append(
            Portfolio.create(
                code=f"DEEP-{index}",
                name=f"Deep {index}",
                parent=deep_chain[-1],
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id(str(5 + index)),
            )
        )
    assert deep_chain[-1].path.count("/") == MAX_PORTFOLIO_DEPTH

    with pytest.raises(ValidationError, match="maximum"):
        grandchild.move_to(deep_chain[-1])


def test_maker_equals_checker_rejected_by_constraint() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        maker = AppUser(
            username="maker",
            email="maker@example.com",
            full_name="Maker",
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("1"),
        )
        checker = AppUser(
            username="checker",
            email="checker@example.com",
            full_name="Checker",
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("2"),
        )
        session.add_all([maker, checker])
        session.flush()

        valid_request = MakerCheckerRequest(
            subject_type="covenant_registration",
            subject_id=uuid4(),
            operation="register",
            payload={"foo": "bar"},
            maker_id=maker.id,
            checker_id=checker.id,
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("3"),
        )
        session.add(valid_request)
        session.commit()

        invalid_request = MakerCheckerRequest(
            subject_type="covenant_registration",
            subject_id=uuid4(),
            operation="register",
            payload={"foo": "bar"},
            maker_id=maker.id,
            checker_id=maker.id,
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("4"),
        )
        session.add(invalid_request)
        with pytest.raises(IntegrityError):
            session.commit()


def test_session_stores_only_token_hash() -> None:
    column_names = {column.name for column in UserSession.__table__.columns}
    assert "token_hash" in column_names
    assert "token" not in column_names
    assert not any("token" in name and name != "token_hash" for name in column_names)


def test_scope_absence_means_no_access() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        user = AppUser(
            username="scoped",
            email="scoped@example.com",
            full_name="Scoped User",
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("1"),
        )
        session.add(user)
        session.commit()

        # No `user_portfolio_scope` row was ever created for this user,
        # and the table has no "grants everything" column anywhere for a
        # query to fall back on.
        scopes = session.scalars(
            select(UserPortfolioScope).where(UserPortfolioScope.user_id == user.id)
        ).all()
        assert scopes == []
        assert "grants_all" not in {c.name for c in UserPortfolioScope.__table__.columns}
