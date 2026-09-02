"""Unit tests for T-016's immutable scopes and privileged read boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.core.errors import AuthorizationError
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.identity import (
    AppUser,
    Role,
    UserPortfolioScope,
    UserRole,
)
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.base import RepositoryBase
from covenant_radar.db.scoping import (
    AUDITOR_CALLER,
    RETENTION_JOB_CALLER,
    Scope,
    ScopeAuditError,
    ScopeResolver,
)
from covenant_radar.security.rbac import Principal

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object, str]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, payload, actor, request_id))
        return object()


def _request_id(value: str) -> str:
    return f"rq-{value:0>16}"


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            AppUser.__table__,
            Role.__table__,
            UserRole.__table__,
            Portfolio.__table__,
            UserPortfolioScope.__table__,
            Borrower.__table__,
        ],
    )
    return engine


def _portfolio(code: str, *, parent: Portfolio | None = None) -> Portfolio:
    return Portfolio.create(
        code=code,
        name=code.title(),
        parent=parent,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_request_id(code),
    )


def _borrower(reference: str, portfolio: Portfolio) -> Borrower:
    return Borrower(
        id=uuid4(),
        reference=reference,
        legal_name=f"Borrower {reference}",
        portfolio_id=portfolio.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_request_id(reference),
    )


def _user(user_id: UUID | None = None) -> AppUser:
    return AppUser(
        id=user_id or uuid4(),
        username=f"user-{uuid4().hex[:10]}",
        email=f"{uuid4().hex[:10]}@example.com",
        full_name="Scoped User",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_request_id("user"),
    )


def test_empty_scope_returns_nothing() -> None:
    engine = _engine()
    with Session(engine) as session:
        portfolio = _portfolio("A")
        borrower = _borrower("B-000001", portfolio)
        session.add_all([portfolio, borrower])
        session.flush()

        repository = RepositoryBase(session, Borrower)
        scope = Scope.empty(uuid4())

        assert repository.list(scope=scope) == ()
        assert repository.get(borrower.id, scope=scope) is None
        assert repository.find(scope=scope, reference=borrower.reference) is None


def test_descendants_matched_by_path_prefix() -> None:
    engine = _engine()
    with Session(engine) as session:
        root = _portfolio("ROOT")
        child = _portfolio("CHILD", parent=root)
        other = _portfolio("OTHER")
        rows = [_borrower("B-ROOT", root), _borrower("B-CHILD", child), _borrower("B-OTHER", other)]
        session.add_all([root, child, other, *rows])
        session.flush()

        user_id = uuid4()
        scope = ScopeResolver(session).resolve(Principal.user(user_id, ()))
        assert scope.is_empty

        descendant_scope = Scope.from_paths(user_id, [root.path])
        exact_scope = Scope.from_paths(user_id, [root.path], include_descendants=False)
        repository = RepositoryBase(session, Borrower)

        assert {row.reference for row in repository.list(scope=descendant_scope)} == {
            "B-ROOT",
            "B-CHILD",
        }
        assert {row.reference for row in repository.list(scope=exact_scope)} == {"B-ROOT"}


def test_scope_resolver_caches_once_per_request() -> None:
    engine = _engine()
    with Session(engine) as session:
        user = _user()
        portfolio = _portfolio("A")
        session.add_all(
            [
                user,
                portfolio,
                UserPortfolioScope(
                    user_id=user.id,
                    portfolio_id=portfolio.id,
                    include_descendants=True,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=_request_id("scope"),
                ),
            ]
        )
        session.flush()

        resolver = ScopeResolver(session)
        principal = Principal.user(user.id, ())
        first = resolver.resolve(principal)
        second = resolver.resolve(principal)

        assert first is second
        assert first.descendant_paths == (portfolio.path,)


def test_unscoped_read_restricted_and_audited() -> None:
    engine = _engine()
    with Session(engine) as session:
        auditor = _user()
        ordinary = _user()
        role = Role(
            id=uuid4(),
            code="auditor",
            name="Auditor",
            is_system=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_request_id("role"),
        )
        a = _portfolio("A")
        b = _portfolio("B")
        rows = [_borrower("B-000001", a), _borrower("B-000002", b)]
        session.add_all(
            [
                auditor,
                ordinary,
                role,
                a,
                b,
                *rows,
                UserRole(
                    user_id=auditor.id,
                    role_id=role.id,
                    granted_by_id=auditor.id,
                    granted_at=_NOW,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=_request_id("grant"),
                ),
            ]
        )
        session.flush()
        audit = _Audit()
        repository = RepositoryBase(session, Borrower, audit=audit)

        result = repository.list_unscoped(
            caller=AUDITOR_CALLER,
            principal=Principal.user(auditor.id, ()),
            reason="retention evidence review",
            request_id=_request_id("audit"),
        )

        assert {row.reference for row in result} == {"B-000001", "B-000002"}
        assert len(audit.events) == 1
        event, subject, payload, actor, request_id = audit.events[0]
        assert event == "repository_unscoped_read"
        assert subject == ("repository", "Borrower")
        assert payload["scope_bypassed"] is True
        assert actor == auditor.id
        assert request_id == _request_id("audit")

        with pytest.raises(AuthorizationError, match="auditor role"):
            repository.list_unscoped(
                caller=AUDITOR_CALLER,
                principal=Principal.user(ordinary.id, ()),
                reason="not permitted",
            )


def test_retention_job_is_the_only_non_user_unscoped_caller() -> None:
    engine = _engine()
    with Session(engine) as session:
        portfolio = _portfolio("A")
        session.add_all([portfolio, _borrower("B-000001", portfolio)])
        session.flush()
        audit = _Audit()
        repository = RepositoryBase(session, Borrower, audit=audit)

        rows = repository.list_unscoped(
            caller=RETENTION_JOB_CALLER,
            reason="retention purge selection",
        )

        assert len(rows) == 1
        assert audit.events[0][3] == "retention_job"
        with pytest.raises(AuthorizationError, match="valid named caller"):
            repository.list_unscoped(caller="external", reason="invalid")  # type: ignore[arg-type]


def test_unscoped_read_requires_audit_writer() -> None:
    engine = _engine()
    with Session(engine) as session:
        portfolio = _portfolio("A")
        session.add_all([portfolio, _borrower("B-000001", portfolio)])
        session.flush()
        repository = RepositoryBase(session, Borrower)

        with pytest.raises(ScopeAuditError, match="audit writer"):
            repository.list_unscoped(caller=RETENTION_JOB_CALLER, reason="required audit")
