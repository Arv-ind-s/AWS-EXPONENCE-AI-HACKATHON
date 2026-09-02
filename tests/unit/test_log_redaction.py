"""Focused T-142 tests for the logging security boundary and rotation."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
import structlog

from covenant_radar.core.context import bind_job_run_id, bind_request_id
from covenant_radar.observability.logging import configure
from covenant_radar.observability.redaction import PromptLoggingError
from covenant_radar.observability.retention import (
    IntegrityRotatingFileHandler,
    integrity_sidecar,
    verify_integrity_hash,
)


def _config(path: Path, log_directory: Path) -> None:
    path.write_text(
        """
level = "INFO"

[redaction]
key_tokens = ["password", "secret", "token", "api_key", "authorization"]
personal_fields = ["email", "full_name", "pan_number", "account_number", "principal_id"]
reject_prompt_bodies = true

[rotation]
directory = "LOG_DIRECTORY"
max_bytes = 512
interval_seconds = 86400
retention_days = 180

[sampling]
default_rate = 1.0
""".replace("LOG_DIRECTORY", str(log_directory).replace("\\", "\\\\")),
        encoding="utf-8",
    )


def _last_json_line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    line = capsys.readouterr().out.strip().splitlines()[-1]
    return json.loads(line)


def test_personal_field_names_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "logging.toml"
    _config(config_path, tmp_path / "logs")
    configure(config_path)

    with bind_request_id("rq-t142-personal"), bind_job_run_id("jr-t142-personal"):
        structlog.get_logger("test.personal").info(
            "workload.record",
            email="alice@example.com",
            full_name="Alice Example",
            nested={"pan_number": "ABCDE1234F"},
            safe_reference="borrower-0001",
        )

    payload = _last_json_line(capsys)
    assert payload["email"] == "***REDACTED***"
    assert payload["full_name"] == "***REDACTED***"
    assert payload["nested"] == {"pan_number": "***REDACTED***"}
    assert payload["safe_reference"] == "borrower-0001"


def test_secret_patterns_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "logging.toml"
    _config(config_path, tmp_path / "logs")
    configure(config_path)

    structlog.get_logger("test.secret").info(
        "connector.failed",
        detail="Bearer live-credential-that-must-not-escape",
        connection="password=hunter2",
    )

    line = capsys.readouterr().out
    assert "live-credential-that-must-not-escape" not in line
    assert "hunter2" not in line
    assert "***REDACTED***" in line


def test_prompt_body_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "logging.toml"
    _config(config_path, tmp_path / "logs")
    configure(config_path)

    with pytest.raises(PromptLoggingError):
        structlog.get_logger("test.prompt").info(
            "model.request",
            prompt_body="the complete customer prompt",
        )

    assert capsys.readouterr().out == ""


def test_rotation_hashes_file(tmp_path: Path) -> None:
    path = tmp_path / "application.log"
    handler = IntegrityRotatingFileHandler(path, max_bytes=32, interval_seconds=3600)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.rotation")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for index in range(5):
            logger.info("record-%s-xxxxxxxxxxxxxxxx", index)
    finally:
        logger.removeHandler(handler)
        handler.close()

    archives = [
        candidate
        for candidate in tmp_path.iterdir()
        if candidate.name.startswith("application.log.") and not candidate.name.endswith(".sha256")
    ]
    assert archives
    for archive in archives:
        sidecar = integrity_sidecar(archive)
        assert sidecar.is_file()
        assert hashlib.sha256(archive.read_bytes()).hexdigest() in sidecar.read_text()
        assert verify_integrity_hash(archive)
