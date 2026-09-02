"""Workload-level proofs that no sensitive values reach log sinks."""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi.testclient import TestClient

from covenant_radar.asgi import create_app
from covenant_radar.observability.logging import configure, logging_health
from tests.unit.test_log_redaction import _config


def _log_files(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in directory.glob("*.log") if path.is_file()
    )


def test_full_workload_logs_contain_no_personal_value(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "logging.toml"
    logs = tmp_path / "logs"
    _config(config_path, logs)
    configure(config_path, stdout=False)

    workload = structlog.get_logger("workload.personal")
    for index in range(20):
        workload.info(
            "borrower.processed",
            borrower_reference=f"B-{index:06d}",
            full_name="Alice Example",
            email="alice@example.com",
            account_number="000123456789",
            status="review",
        )

    content = _log_files(logs)
    assert "Alice Example" not in content
    assert "alice@example.com" not in content
    assert "000123456789" not in content


def test_full_workload_logs_contain_no_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "logging.toml"
    logs = tmp_path / "logs"
    _config(config_path, logs)
    configure(config_path, stdout=False)

    workload = structlog.get_logger("workload.secret")
    for index in range(20):
        workload.info(
            "connector.processed",
            connector=f"feed-{index}",
            authorization="Bearer credential-that-must-not-escape",
            detail="password=hunter2",
        )

    content = _log_files(logs)
    assert "credential-that-must-not-escape" not in content
    assert "hunter2" not in content


def test_unwritable_directory_does_not_break_request(
    tmp_path: Path,
) -> None:
    blocking_path = tmp_path / "not-a-directory"
    blocking_path.write_text("directory creation must fail", encoding="utf-8")
    configure(log_directory=blocking_path, stdout=False)

    app = create_app()
    # create_app configures its normal sink at startup; restore the deliberately
    # broken sink after composition to exercise a live request against it.
    configure(log_directory=blocking_path, stdout=True)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["logging"]["healthy"] is False
    assert body["logging"]["status"] == "degraded"
    assert logging_health()["healthy"] is False
