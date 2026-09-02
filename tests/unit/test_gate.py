"""Tests for the T-002 quality gate."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from io import StringIO
from pathlib import Path

from covenant_radar import cli

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE_PACKAGES = (
    "ai",
    "api",
    "audit",
    "config",
    "db",
    "documents",
    "domain",
    "ingestion",
    "notifications",
    "observability",
    "ports",
    "scheduler",
    "security",
    "services",
    "web",
)


def test_gate_runs_steps_in_order() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 0

    result = cli.run_gate(
        fast=True,
        command_runner=runner,
        executable_finder=lambda _: "nox",
        stream=StringIO(),
    )

    assert result == 0
    assert [command[-1] for command in commands] == [
        "format",
        "lint",
        "types",
        "imports",
        "tests",
        "alembic_drift",
    ]


def test_gate_stops_at_first_failure() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 19 if command[-1] == "lint" else 0

    output = StringIO()
    result = cli.run_gate(
        fast=True,
        command_runner=runner,
        executable_finder=lambda _: "nox",
        stream=output,
    )

    assert result == 19
    assert [command[-1] for command in commands] == ["format", "lint"]
    assert "FAIL lint — exit 19" in output.getvalue()

    missing_tool_output = StringIO()
    assert (
        cli.run_gate(
            fast=True,
            command_runner=runner,
            executable_finder=lambda _: None,
            stream=missing_tool_output,
        )
        == 127
    )
    assert "Required tool unavailable: nox" in missing_tool_output.getvalue()

    invalid_step_output = StringIO()
    assert (
        cli.run_gate(
            fast=True,
            selected_steps=("unknown",),
            command_runner=runner,
            executable_finder=lambda _: "nox",
            stream=invalid_step_output,
        )
        == 2
    )
    assert "Valid steps: format, lint, type-check" in invalid_step_output.getvalue()


def test_unimplemented_step_skips_with_task_id() -> None:
    output = StringIO()
    result = cli.run_gate(
        fast=False,
        command_runner=lambda _: 0,
        executable_finder=lambda _: "nox",
        stream=output,
    )

    assert result == 0
    assert "SKIP integration-tests — not yet implemented (T-003)" in output.getvalue()


def test_import_contracts_parse() -> None:
    result = subprocess.run(
        ["lint-imports"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_domain_contract_would_fail_on_framework_import() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_root = Path(temporary_directory)
        package_root = temporary_root / "covenant_radar"
        framework_root = temporary_root / "fastapi"
        package_root.mkdir()
        framework_root.mkdir()
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        for package in ARCHITECTURE_PACKAGES:
            package_directory = package_root / package
            package_directory.mkdir()
            (package_directory / "__init__.py").write_text("", encoding="utf-8")
        domain_root = package_root / "domain"
        (domain_root / "offending.py").write_text("import fastapi\n", encoding="utf-8")
        (framework_root / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy(ROOT / ".importlinter", temporary_root / ".importlinter")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = (
            str(temporary_root) + os.pathsep + environment.get("PYTHONPATH", "")
        )

        result = subprocess.run(
            ["lint-imports"],
            cwd=temporary_root,
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )

    assert result.returncode != 0
    assert "Domain purity" in result.stdout
