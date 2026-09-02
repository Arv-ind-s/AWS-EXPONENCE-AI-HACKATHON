"""Static contract checks for the one-command local demo bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


SCRIPT = Path(__file__).parents[2] / "scripts" / "demo_up.ps1"


def test_demo_bootstrap_runs_the_required_steps_in_order() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    # Anchored on the invocation, not the bare word: `serve` also appears in
    # the script's prose, so matching the substring alone found a comment near
    # the top of the file and made the ordering assertion vacuous.  The order
    # itself matches the script and the README: personas exist before the
    # pipeline runs, because the pipeline assigns cases to them.
    required_steps = (
        "pip install -e .",
        "radarctl migrate upgrade",
        "radarctl seed --reference-portfolio",
        "radarctl seed --demo-covenants",
        "python create_user.py",
        "radarctl job run nightly.pipeline",
        "radarctl serve",
    )
    positions = []
    for step in required_steps:
        index = content.find(step)
        assert index != -1, f"demo_up.ps1 no longer runs: {step}"
        positions.append(index)

    assert positions == sorted(positions), (
        "demo_up.ps1 steps are out of order: "
        f"{[step for _, step in sorted(zip(positions, required_steps, strict=True))]}"
    )
    assert "Set-StrictMode -Version Latest" in content
    assert "COVENANT_RADAR_DATABASE__URL" in content
    assert "sqlite:///var/covenant-radar.db" in content
    assert "COVENANT_RADAR_AI__PROVIDER" in content
    assert '"recorded"' in content
    assert "COVENANT_RADAR_DOCUMENTS__STORE" in content
    assert '"local"' in content


def test_demo_bootstrap_does_not_persist_generated_secrets() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "New-DemoSecret" in content
    assert "SetEnvironmentVariable($Name, (New-DemoSecret), \"Process\")" in content
    assert "Set-Content" not in content
    assert "Out-File" not in content
