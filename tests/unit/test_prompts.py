"""Unit coverage for T-092 prompt versioning and integrity checks."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from covenant_radar.ai.prompts.loader import (
    PromptIntegrityError,
    PromptLoader,
    PromptPlaceholderError,
    PromptVersionError,
    verify_prompt_manifest,
)

pytestmark = pytest.mark.unit

PROMPT_ROOT = Path(__file__).resolve().parents[2] / "src" / "covenant_radar" / "ai" / "prompts"


def _copy_prompts(tmp_path: Path) -> PromptLoader:
    destination = tmp_path / "prompts"
    shutil.copytree(PROMPT_ROOT, destination)
    return PromptLoader(destination)


def test_edit_without_bump_fails(tmp_path: Path) -> None:
    loader = _copy_prompts(tmp_path)
    prompt_path = loader.prompt_directory / "stage1_extract.v1.md"
    prompt_path.write_text(
        prompt_path.read_text(encoding="utf-8") + "\nAdditional instruction.\n",
        encoding="utf-8",
    )

    with pytest.raises(PromptIntegrityError, match=r"stage1_extract\.v1\.md.*v1"):
        loader.load("stage1_extract", "v1")


def test_bump_without_change_refused(tmp_path: Path) -> None:
    loader = _copy_prompts(tmp_path)
    original = loader.prompt_directory / "stage1_extract.v1.md"
    bumped = loader.prompt_directory / "stage1_extract.v2.md"
    bumped.write_text(
        original.read_text(encoding="utf-8").replace("prompt-version: v1", "prompt-version: v2", 1),
        encoding="utf-8",
    )
    original.unlink()

    with pytest.raises(PromptIntegrityError, match="version bump without a content change"):
        loader.update_manifest()


def test_missing_version_header_refused(tmp_path: Path) -> None:
    loader = _copy_prompts(tmp_path)
    prompt_path = loader.prompt_directory / "stage1_extract.v1.md"
    lines = prompt_path.read_text(encoding="utf-8").splitlines()
    prompt_path.write_text("# Missing header\n" + "\n".join(lines[1:]) + "\n", encoding="utf-8")

    with pytest.raises(PromptVersionError, match="no valid first-line version header"):
        loader.load("stage1_extract", "v1")


def test_unknown_version_lists_available(tmp_path: Path) -> None:
    loader = _copy_prompts(tmp_path)

    with pytest.raises(ValueError, match=r"available versions: v1"):
        loader.load("stage1_extract", "v9")


def test_unfilled_placeholder_refused(tmp_path: Path) -> None:
    loader = _copy_prompts(tmp_path)
    prompt_path = loader.prompt_directory / "stage1_extract.v1.md"
    prompt_path.write_text(
        prompt_path.read_text(encoding="utf-8").replace(
            "{{ clause_text }}", "{{ missing_slot }}", 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(PromptPlaceholderError, match="not supplied"):
        loader.load("stage1_extract", "v1")


def test_both_prompts_declare_output_shape(tmp_path: Path) -> None:
    shipped_prompts = verify_prompt_manifest()
    loader = _copy_prompts(tmp_path)
    prompts = loader.verify()

    assert {prompt.name for prompt in shipped_prompts} == {"stage1_extract", "stage7_memo"}
    assert {prompt.name for prompt in prompts} == {"stage1_extract", "stage7_memo"}
    assert all(prompt.output_shape == "json" for prompt in prompts)
    assert all(prompt.placeholders for prompt in prompts)
