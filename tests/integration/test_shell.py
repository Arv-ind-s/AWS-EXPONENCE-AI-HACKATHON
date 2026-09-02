"""Integration coverage for the T-022 application shell."""

from __future__ import annotations

import io
import json
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from covenant_radar.api.deps import public
from covenant_radar.asgi import create_app
from covenant_radar.cli import run_serve
from covenant_radar.config.settings import get_settings
from covenant_radar.i18n import CatalogueError, assert_no_literal_user_facing_strings
from covenant_radar.i18n.formatting import (
    format_fy_label,
    format_fy_quarter,
    format_indian_currency,
    format_indian_number,
)

pytestmark = pytest.mark.integration


def test_health_and_version() -> None:
    app = create_app()
    with TestClient(app) as client:
        health = client.get("/health")
        version = client.get("/version")

    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    assert health.json()["request_id"].startswith("rq-")
    assert version.status_code == 200
    assert version.json()["version"] == health.json()["version"]


def test_request_id_on_every_log_line(capsys: pytest.CaptureFixture[str]) -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"event": "request_completed"' in line
    ]
    assert records
    assert all(re.fullmatch(r"rq-[0-9a-f]{16}", record["request_id"]) for record in records)


def test_unknown_route_designed_404() -> None:
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/route-that-does-not-exist")

    assert response.status_code == 404
    assert "This page is not available" in response.text
    assert "Traceback" not in response.text
    assert response.headers["x-request-id"].startswith("rq-")


def test_exception_renders_500_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app()

    @app.get("/explode")
    @public
    async def explode() -> None:
        raise RuntimeError("private implementation detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/explode")

    assert response.status_code == 500
    assert "Support reference:" in response.text
    assert "Traceback" not in response.text
    assert "private implementation detail" not in response.text
    assert response.headers["x-request-id"].startswith("rq-")
    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"event": "request_failed"' in line
    ]
    assert len(records) == 1
    assert records[0]["level"] == "error"
    assert records[0]["error_class"] == "RuntimeError"
    assert records[0]["request_id"] == response.headers["x-request-id"]


def test_literal_string_fails_build_check(tmp_path: Path) -> None:
    template = tmp_path / "offending.html"
    template.write_text("<p>Untranslated warning</p>\n", encoding="utf-8")

    with pytest.raises(CatalogueError, match=r"offending\.html:1"):
        assert_no_literal_user_facing_strings(tmp_path)


def test_theme_resolved_server_side_no_flash() -> None:
    app = create_app()
    with TestClient(app) as client:
        client.cookies.set("covenant_radar_theme", "dark")
        response = client.get("/route-that-does-not-exist")

    assert response.status_code == 404
    assert '<html lang="en" data-theme="dark"' in response.text
    assert "localStorage" not in response.text
    assert "matchMedia" not in response.text


def test_lakh_crore_formatting() -> None:
    assert format_indian_number(1234567) == "12,34,567"
    assert format_indian_currency(1234567) == "₹12.35 lakh"
    assert format_indian_currency(624000000) == "₹62.4 crore"


def test_fy_quarter_formatting() -> None:
    assert format_fy_quarter(date(2026, 4, 1)) == "Q1 FY27"
    assert format_fy_quarter(date(2027, 3, 31)) == "Q4 FY27"
    assert format_fy_label(date(2026, 10, 1)) == "FY27Q3"


def test_port_busy_exits_3() -> None:
    output = io.StringIO()
    exit_code = run_serve(
        settings=get_settings(),
        port=8123,
        port_checker=lambda _host, _port: False,
        server_runner=lambda *_args, **_kwargs: pytest.fail("server must not start"),
        stream=output,
    )

    assert exit_code == 3
    assert "8123" in output.getvalue()


def _refuse_self_check(_settings: object, stream: io.StringIO) -> int:
    stream.write("Startup self-check 'database' failed: unreachable\n")
    return 4


def test_failed_startup_self_check_refuses_to_start() -> None:
    """`T-149` (`spec §N-06.b`): the process starts only when it can work —
    a failing pre-flight self-check refuses to start, naming the check,
    without ever handing control to the ASGI server."""
    output = io.StringIO()
    exit_code = run_serve(
        settings=get_settings(),
        port=8124,
        port_checker=lambda _host, _port: True,
        self_check_runner=_refuse_self_check,
        server_runner=lambda *_args, **_kwargs: pytest.fail("server must not start"),
        stream=output,
    )

    assert exit_code == 4
    assert "database" in output.getvalue()
