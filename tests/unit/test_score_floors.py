"""Regression-gate coverage for T-106 score floors."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from evaluation import run as evaluation_run
from evaluation.arms import product as product_arm
from evaluation.run import (
    FloorConfigurationError,
    check_score_floors,
    load_floors,
    main,
    raise_floor,
)
from evaluation.score import ArmScore, score_arm, score_example

pytestmark = pytest.mark.unit

_EXAMPLES = Path(__file__).resolve().parents[2] / "evaluation" / "examples"


def _write_ledger(path: Path, *, category: str = "engine", floor: str = "0.800") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "categories": {
                    category: {
                        "floor": floor,
                        "history": [
                            {
                                "floor": floor,
                                "justification": "Initial test floor.",
                                "recorded_at": "2026-01-01T00:00:00+00:00",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _score(*, category: str = "engine", passed: bool = False) -> ArmScore:
    example = {
        "id": "EX-TEST",
        "kind": category,
        "expected": {"value": "expected"},
        "pass_mark": {"type": "exact"},
    }
    actual = {"value": "expected" if passed else "observed"}
    item = score_example(example, actual, arm="product")
    return score_arm((example,), (item,), arm="product")


def test_below_floor_fails_naming_numbers(tmp_path: Path) -> None:
    floors = tmp_path / "floors.json"
    _write_ledger(floors, floor="0.800")

    failures = check_score_floors(_score(), load_floors(floors))

    assert len(failures) == 1
    assert failures[0].message() == ("FLOOR VIOLATION: category=engine floor=0.800 observed=0")


def test_floors_never_lowered(tmp_path: Path) -> None:
    floors = tmp_path / "floors.json"
    _write_ledger(floors, floor="0.800")
    payload = json.loads(floors.read_text(encoding="utf-8"))
    payload["categories"]["engine"]["floor"] = "0.700"
    floors.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FloorConfigurationError, match="latest recorded history"):
        load_floors(floors)


def test_new_category_without_floor_fails(tmp_path: Path) -> None:
    floors = tmp_path / "floors.json"
    _write_ledger(floors)

    failures = check_score_floors(_score(category="new_category", passed=True), load_floors(floors))

    assert len(failures) == 1
    assert failures[0].reason == "missing_floor"
    assert "category=new_category" in failures[0].message()
    assert "record a floor" in failures[0].message()


def test_raise_requires_justification(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    floors = tmp_path / "floors.json"
    _write_ledger(floors, floor="0.800")

    exit_code = main(["--raise-floor", "engine", "0.900", "--floors", str(floors)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "requires a non-blank --justification" in captured.err
    assert json.loads(floors.read_text(encoding="utf-8"))["categories"]["engine"]["floor"] == (
        "0.800"
    )


def test_raise_floor_records_justification_and_history(tmp_path: Path) -> None:
    floors = tmp_path / "floors.json"
    _write_ledger(floors, floor="0.800")

    raise_floor("engine", "0.900", "Calibrated after an approved evaluation change.", path=floors)
    stored = json.loads(floors.read_text(encoding="utf-8"))

    category = stored["categories"]["engine"]
    assert category["floor"] == "0.900"
    assert len(category["history"]) == 2
    assert category["history"][-1]["justification"] == (
        "Calibrated after an approved evaluation change."
    )


def test_gate_failure_has_nonzero_exit_and_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    floors = tmp_path / "floors.json"
    _write_ledger(floors, floor="1.000")
    monkeypatch.setattr(product_arm, "run_product_example", lambda _example, **_kwargs: {})

    result = evaluation_run.run_evaluation(
        only="EX-0001",
        examples_dir=_EXAMPLES,
        cassette_path=tmp_path / "cassettes",
        runs_dir=tmp_path / "runs",
        commit_reference="floor-test-commit",
        stream=io.StringIO(),
        gate=True,
        floors_path=floors,
    )

    assert result.exit_code == 1
    assert not result.broken
    assert "FLOOR VIOLATION: category=engine floor=1.000 observed=0" in result.text
    assert result.path is not None
    stored = json.loads(result.path.read_text(encoding="utf-8"))
    assert stored["gate_passed"] is False
    assert stored["floor_failures"][0]["category"] == "engine"
