"""Integration coverage for the honest baseline comparison (T-105)."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from pathlib import Path

import pytest

from evaluation import run as evaluation_run
from evaluation.arms import baseline as baseline_arm
from evaluation.arms import product as product_arm

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).parents[2]
_EXAMPLES = _ROOT / "evaluation" / "examples"


def _run(tmp_path: Path, *, only: str | None = None):
    return evaluation_run.run_evaluation(
        both_arms=True,
        only=only,
        examples_dir=_EXAMPLES,
        cassette_path=tmp_path / "cassettes",
        runs_dir=tmp_path / "runs",
        commit_reference="baseline-test-commit",
        stream=io.StringIO(),
    )


def test_both_arms_use_identical_scoring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = evaluation_run.score_example
    calls: list[str] = []

    def spy(example, actual, *, arm):
        calls.append(arm)
        return original(example, actual, arm=arm)

    monkeypatch.setattr(evaluation_run, "score_example", spy)
    result = _run(tmp_path, only="EX-0033")
    product_item = result.record.scores["product"].examples[0]
    baseline_item = result.record.scores["baseline"].examples[0]

    assert calls == ["product", "baseline"]
    assert product_item.example_id == baseline_item.example_id == "EX-0033"
    assert product_item.metric["type"] == baseline_item.metric["type"] == "rubric_floor"


def test_baseline_win_is_prominent_and_gap_is_computed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        product_arm,
        "run_product_example",
        lambda _example, **_kwargs: {"criteria": []},
    )
    monkeypatch.setattr(
        baseline_arm,
        "run_baseline_example",
        lambda example: {"criteria": example["expected"]["criteria"]},
    )

    result = _run(tmp_path, only="EX-0033")
    usefulness = result.record.report.gaps["usefulness"]

    assert usefulness.product == Decimal("0")
    assert usefulness.baseline == Decimal("1")
    assert usefulness.gap == Decimal("-1")
    assert "BASELINE OUTSCORES PRODUCT: usefulness" in result.text


def test_non_applicable_categories_are_not_zero(tmp_path: Path) -> None:
    result = _run(tmp_path, only="EX-0001")

    engine = result.record.report.gaps["engine"]
    assert engine.product == Decimal("1")
    assert engine.baseline is None
    assert engine.gap is None
    assert "engine" in result.text and "N/A" in result.text


def test_report_is_stored_with_commit_reference(tmp_path: Path) -> None:
    result = _run(tmp_path, only="EX-0033")

    assert result.path is not None
    report_path = result.path.with_suffix(".scoreboard.json")
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["commit_reference"] == "baseline-test-commit"
    assert set(report["product"]) >= {"arm", "examples", "categories"}
    assert set(report["baseline"]) >= {"arm", "examples", "categories"}


def test_per_category_gap_computed(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.exit_code == 0
    assert result.record.report is not None
    assert "COVENANT RADAR EVALUATION SCOREBOARD" in result.text
    assert "EX-0021" in result.text
    assert result.record.scores["product"].attempted > 0
    assert result.record.scores["baseline"].attempted > 0
    assert result.record.report.gaps["forecast_dating"].gap == Decimal("0")
