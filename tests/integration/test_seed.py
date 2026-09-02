"""Integration coverage for the versioned reference-data loader and CLI."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar import cli
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AppUser,
    IndustryReference,
    Permission,
    Role,
    RolePermission,
)
from covenant_radar.db.seed import ReferenceDataError, SeedLoader

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _ROOT / "src" / "covenant_radar" / "db" / "seed" / "data"
_CLOCK = FixedClock(datetime(2026, 8, 30, 12, 0, tzinfo=UTC))


@pytest.fixture
def db_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_seed_is_idempotent(db_session: Session) -> None:
    first = SeedLoader(db_session, clock=_CLOCK, request_id="test-seed").load()
    second = SeedLoader(db_session, clock=_CLOCK, request_id="test-seed").load()

    assert first.changed
    assert not second.changed
    assert first.catalog_hash == second.catalog_hash
    assert second.inserted == {}
    assert second.updated == {}


def test_roles_match_spec_matrix(db_session: Session) -> None:
    SeedLoader(db_session, clock=_CLOCK, request_id="test-seed").load()
    permission_by_code = {
        permission.code: permission.id for permission in db_session.scalars(select(Permission))
    }
    role_permissions: dict[str, set[str]] = {}
    for role in db_session.scalars(select(Role)).all():
        ids = db_session.scalars(
            select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
        ).all()
        role_permissions[role.code] = {
            code for code, permission_id in permission_by_code.items() if permission_id in ids
        }

    expected_common = {
        "VIEW_QUEUE",
        "VIEW_BORROWER",
        "VIEW_CASE",
        "VIEW_MEMO",
        "VIEW_COVENANT",
        "VIEW_FORECAST",
        "VIEW_EVIDENCE",
        "VIEW_DOCUMENT",
    }
    assert role_permissions == {
        "relationship_manager": expected_common
        | {"GENERATE_MEMO", "RUN_SIMULATION", "LOG_ACTION", "UPDATE_CASE", "RECORD_DISPOSITION"},
        "credit": expected_common
        | {
            "UPLOAD_DOCUMENT",
            "RUN_INTAKE",
            "REGISTER_COVENANT",
            "RECORD_WAIVER",
            "GENERATE_MEMO",
            "RUN_SIMULATION",
            "LOG_ACTION",
            "UPDATE_CASE",
                "RECORD_DISPOSITION",
                "EXPORT_EVIDENCE",
                "INGEST_FINANCIAL_STATEMENTS",
        },
        "credit_approver": expected_common
        | {
            "UPLOAD_DOCUMENT",
            "RUN_INTAKE",
            "REGISTER_COVENANT",
            "APPROVE_COVENANT",
            "RECORD_WAIVER",
            "GENERATE_MEMO",
            "RUN_SIMULATION",
            "LOG_ACTION",
            "UPDATE_CASE",
            "RECORD_DISPOSITION",
            "EXPORT_EVIDENCE",
        },
        "risk": expected_common
        | {
            "UPLOAD_DOCUMENT",
            "RUN_INTAKE",
            "REGISTER_COVENANT",
            "RECORD_WAIVER",
            "GENERATE_MEMO",
            "RUN_SIMULATION",
            "LOG_ACTION",
            "UPDATE_CASE",
            "RECORD_DISPOSITION",
            "OVERRIDE_RISK_VIEW",
                "PROPOSE_THRESHOLDS",
                "EXPORT_EVIDENCE",
                "INGEST_FINANCIAL_STATEMENTS",
        },
        "risk_head": expected_common
        | {
            "UPLOAD_DOCUMENT",
            "RUN_INTAKE",
            "REGISTER_COVENANT",
            "APPROVE_COVENANT",
            "RECORD_WAIVER",
            "GENERATE_MEMO",
            "RUN_SIMULATION",
            "LOG_ACTION",
            "UPDATE_CASE",
            "RECORD_DISPOSITION",
            "OVERRIDE_RISK_VIEW",
            "PROPOSE_THRESHOLDS",
            "APPROVE_THRESHOLDS",
            "APPROVE_MODEL_PROMOTION",
            "EXPORT_EVIDENCE",
        },
        "auditor": expected_common | {"VIEW_AUDIT", "EXPORT_EVIDENCE", "READ_PERSONAL_DATA"},
        "administrator": set(permission_by_code),
        "data_steward": expected_common
        | {
            "UPLOAD_DOCUMENT",
            "RUN_INTAKE",
            "RESOLVE_QUARANTINE",
                "CORRECT_SOURCE_DATA",
                "INGEST_DATA",
                "INGEST_FINANCIAL_STATEMENTS",
        },
    }
    assert "CONFIRM_FAILED_COVENANT" not in permission_by_code
    assert "CREDIT_DECISION" not in permission_by_code


def test_newer_taxonomy_supersedes_not_overwrites(db_session: Session, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(_DATA_DIR, data_dir)
    SeedLoader(db_session, data_dir=data_dir, clock=_CLOCK, request_id="test-seed").load()

    industry_path = data_dir / "industries.json"
    industries = json.loads(industry_path.read_text(encoding="utf-8"))
    industries["taxonomy_version"] = "2.0"
    industries["industries"] = [
        row for row in industries["industries"] if row["code"] not in {"A", "A01"}
    ]
    industries["industries"].append(
        {"code": "C30", "name": "Other manufacturing", "parent_code": "C"}
    )
    industry_path.write_text(json.dumps(industries), encoding="utf-8")

    report = SeedLoader(
        db_session, data_dir=data_dir, clock=_CLOCK, request_id="test-seed-2"
    ).load()
    retained = db_session.scalar(select(IndustryReference).where(IndustryReference.code == "A"))
    added = db_session.scalar(select(IndustryReference).where(IndustryReference.code == "C30"))

    assert report.changed
    assert report.retained["industries"] >= 1
    assert retained is not None
    assert added is not None
    assert added.taxonomy_version == "2.0"


def test_retired_reference_still_resolves(db_session: Session, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(_DATA_DIR, data_dir)
    SeedLoader(db_session, data_dir=data_dir, clock=_CLOCK, request_id="test-seed").load()

    industry_path = data_dir / "industries.json"
    industries = json.loads(industry_path.read_text(encoding="utf-8"))
    industries["taxonomy_version"] = "2.0"
    industries["industries"] = [row for row in industries["industries"] if row["code"] != "A01"]
    industry_path.write_text(json.dumps(industries), encoding="utf-8")

    SeedLoader(db_session, data_dir=data_dir, clock=_CLOCK, request_id="test-seed-2").load()
    retired = db_session.scalar(select(IndustryReference).where(IndustryReference.code == "A01"))
    assert retired is not None


def test_duplicate_code_refuses_and_loads_nothing(db_session: Session, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(_DATA_DIR, data_dir)
    permission_path = data_dir / "permissions.json"
    permissions = json.loads(permission_path.read_text(encoding="utf-8"))
    permissions["permissions"].append(permissions["permissions"][0])
    permission_path.write_text(json.dumps(permissions), encoding="utf-8")

    with pytest.raises(ReferenceDataError, match="VIEW_QUEUE"):
        SeedLoader(db_session, data_dir=data_dir, clock=_CLOCK).load()

    assert db_session.scalar(select(Role.id)) is None
    assert db_session.scalar(select(Permission.id)) is None


def test_reset_refused_on_non_development_url() -> None:
    output = StringIO()
    result = cli.run_seed(
        database_url="postgresql://db.example.invalid/covenant_radar",
        reset=True,
        stream=output,
    )

    assert result == 2
    assert "non-development database" in output.getvalue()
    assert "--i-understand" in output.getvalue()


def test_user_create_refuses_password_argument(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "user",
                "create",
                "--username",
                "admin",
                "--role",
                "administrator",
                "--password",
                "unsafe",
            ]
        )

    assert raised.value.code == 2
    assert "--password" in capsys.readouterr().err


def test_user_create_sets_forced_password_change(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'user.db'}"
    assert cli.run_migrate_upgrade(database_url=database_url, stream=StringIO()) == 0
    assert cli.run_seed(database_url=database_url, stream=StringIO()) == 0
    assert (
        cli.run_user_create(
            username="admin",
            role="administrator",
            database_url=database_url,
            password_reader=lambda _: "A-strong-initial-password",
            stream=StringIO(),
        )
        == 0
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        user = session.scalar(select(AppUser).where(AppUser.username == "admin"))
        assert user is not None
        assert user.must_change_password
        assert user.password_hash is not None
        assert "A-strong-initial-password" not in user.password_hash
    engine.dispose()
