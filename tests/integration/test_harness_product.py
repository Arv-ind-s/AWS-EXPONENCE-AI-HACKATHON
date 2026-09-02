"""Integration coverage for the offline product evaluation harness (T-104)."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from evaluation import EvaluationSkip
from evaluation import run as evaluation_run
from evaluation.arms import product as product_arm

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).parents[2]
_EXAMPLES = _ROOT / "evaluation" / "examples"


def _run(
    tmp_path: Path,
    *,
    only: str | None = None,
    examples_dir: Path = _EXAMPLES,
):
    return evaluation_run.run_evaluation(
        only=only,
        examples_dir=examples_dir,
        cassette_path=tmp_path / "cassettes",
        runs_dir=tmp_path / "runs",
        commit_reference="test-commit",
        stream=io.StringIO(),
    )


def test_all_examples_scored(tmp_path: Path) -> None:
    result = _run(tmp_path)
    product = result.record.scores["product"]

    assert result.exit_code == 0
    assert len(product.examples) == 34
    assert product.attempted == 31
    assert product.skipped == 3
    assert product.passed == 31
    assert product.misses == 0
    assert "ARM product" in result.text


def test_malformed_example_is_named_skipped_and_counted(tmp_path: Path) -> None:
    examples = tmp_path / "examples"
    examples.mkdir()
    shutil.copy2(_EXAMPLES / "_schema.json", examples / "_schema.json")
    shutil.copy2(_EXAMPLES / "EX-0001.json", examples / "EX-0001.json")
    (examples / "EX-9999.json").write_text("{not valid json", encoding="utf-8")

    result = _run(tmp_path, examples_dir=examples)

    assert result.exit_code == 0
    assert len(result.record.discovery_issues) == 1
    assert result.record.discovery_issues[0].path.name == "EX-9999.json"
    assert "SKIP EX-9999.json" in result.text
    assert result.record.scores["product"].attempted == 1


def test_unbuilt_stage_skips_only_its_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = product_arm.run_product_example

    def skip_extraction(example: dict[str, object], **kwargs: object):
        if example.get("kind") == "extraction":
            raise EvaluationSkip("extraction stage is not built")
        return original(example, **kwargs)

    monkeypatch.setattr(product_arm, "run_product_example", skip_extraction)
    result = _run(tmp_path)
    product = result.record.scores["product"]

    skipped_ids = {item.example_id for item in product.examples if item.status == "skipped"}
    assert result.exit_code == 0
    assert {"EX-0025", "EX-0026", "EX-0027", "EX-0028"} <= skipped_ids
    assert product.attempted > 0
    assert product.skipped == 4


def test_cassette_miss_skips_without_network(tmp_path: Path) -> None:
    result = _run(tmp_path, only="EX-0025")
    item = result.record.scores["product"].examples[0]

    assert result.exit_code == 0
    assert item.status == "skipped"
    assert item.reason is not None
    assert "cassette miss" in item.reason
    assert "SKIP" in result.text


def test_score_miss_is_printed_but_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        product_arm,
        "run_product_example",
        lambda _example, **_kwargs: {"computable": False},
    )

    result = _run(tmp_path, only="EX-0001")

    assert result.exit_code == 0
    assert result.record.scores["product"].misses == 1
    assert "MISS" in result.text
    assert result.path is not None and result.path.exists()


def test_harness_failure_has_a_distinct_nonzero_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_example, **_kwargs):
        raise RuntimeError("unexpected product failure")

    monkeypatch.setattr(product_arm, "run_product_example", fail)
    result = _run(tmp_path, only="EX-0001")

    assert result.exit_code == 2
    assert result.broken is True
    assert result.record.broken_error is not None
    assert "BROKEN" in result.text


def test_run_record_carries_the_commit_reference(tmp_path: Path) -> None:
    result = _run(tmp_path, only="EX-0001")

    assert result.path is not None
    stored = json.loads(result.path.read_text(encoding="utf-8"))
    assert stored["commit_reference"] == "test-commit"
    assert stored["commit_sha"] == "test-commit"
    assert stored["scores"]["product"]["examples"][0]["example_id"] == "EX-0001"
