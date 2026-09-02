"""Phase 7A composition checks for the real nightly runtime."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from covenant_radar.config.settings import load_settings
from covenant_radar.db.base import Base
from covenant_radar.db.models.identity import AppUser
from covenant_radar.scheduler.pipeline import PIPELINE_JOB_NAME, PIPELINE_STEPS
from covenant_radar.services.nightly_runtime import build_nightly_runtime

pytestmark = pytest.mark.integration


def test_build_nightly_runtime_registers_real_pipeline(tmp_path: Path) -> None:
    database_path = tmp_path / "phase7a-runtime.db"
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = load_settings(
        environ={"COVENANT_RADAR_DATABASE__URL": f"sqlite:///{database_path}"}
    )

    runtime = build_nightly_runtime(factory, settings)

    assert runtime.threshold_store.snapshot_id()
    assert PIPELINE_JOB_NAME in runtime.registry
    assert all(step in runtime.registry for step in PIPELINE_STEPS)
    with factory() as session:
        assert session.scalar(select(AppUser.id)) == runtime.system_actor_id

    Base.metadata.drop_all(engine)
    engine.dispose()
