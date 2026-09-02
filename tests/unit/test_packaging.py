"""Packaging baseline tests for T-001."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from covenant_radar import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_version_importable() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_cli_entry_point_exists() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "radarctl", "--help"],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    for command in ("serve", "migrate", "seed", "gate", "job", "perf", "diag"):
        assert command in result.stdout


def test_lock_has_no_ranges() -> None:
    lock_lines = (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
    requirements = [line for line in lock_lines if line and not line.startswith("#")]

    assert requirements
    assert not [line for line in requirements if re.search(r"(?:>=|<=|!=|~=|>|<|\*)", line)]


def test_gitignore_covers_generated_paths() -> None:
    ignored = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    assert {
        "var/",
        ".venv/",
        "__pycache__/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "*.db",
        ".env",
        "playwright-report/",
        ".coverage*",
    }.issubset(ignored)
