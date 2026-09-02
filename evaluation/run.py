"""Command-line and programmatic entry point for offline evaluation."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Final, TextIO

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError

from evaluation import (
    DEFAULT_CASSETTES_DIR,
    DEFAULT_COMMIT_REFERENCE,
    DEFAULT_EXAMPLES_DIR,
    DEFAULT_RUNS_DIR,
    EvaluationError,
    EvaluationSkip,
    ExampleDiscoveryError,
    ExampleIssue,
)
from evaluation.arms import baseline as baseline_arm
from evaluation.arms import product as product_arm
from evaluation.report import (
    EvaluationReport,
    build_report,
    persist_report,
    render_arm_table,
    render_scoreboard,
)
from evaluation.score import ArmScore, ExampleScore, score_arm, score_example, skipped_example

_MAX_EXAMPLE_BYTES: Final[int] = 1 * 1024 * 1024
_COMMIT_ENVIRONMENT_KEYS: Final[tuple[str, ...]] = (
    "GIT_COMMIT",
    "CI_COMMIT_SHA",
    "GITHUB_SHA",
    "COMMIT_SHA",
)
_COMMIT_MAX_LENGTH: Final[int] = 200
DEFAULT_FLOORS_PATH: Final[Path] = Path(__file__).resolve().with_name("floors.json")
_FLOOR_SCHEMA_VERSION: Final[int] = 1
_FLOOR_MAX_JUSTIFICATION_LENGTH: Final[int] = 2_000
_FLOOR_CATEGORY_MAX_LENGTH: Final[int] = 100
_FLOOR_CATEGORY_PATTERN: Final[str] = r"^[A-Za-z][A-Za-z0-9_.-]*$"


@dataclass(frozen=True, slots=True)
class ExampleFile:
    """A validated example and its source path."""

    path: Path
    body: dict[str, object]

    @property
    def id(self) -> str:
        return str(self.body["id"])

    @property
    def kind(self) -> str:
        return str(self.body["kind"])


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Valid examples plus malformed files that were explicitly skipped."""

    examples: tuple[ExampleFile, ...]
    issues: tuple[ExampleIssue, ...] = ()


class FloorConfigurationError(EvaluationError):
    """The score-floor ledger is missing, malformed, or has been weakened."""


@dataclass(frozen=True, slots=True)
class FloorHistoryEntry:
    """One justified, immutable-in-practice floor decision."""

    floor: Decimal
    justification: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.floor, Decimal)
            or not self.floor.is_finite()
            or not Decimal("0") <= self.floor <= Decimal("1")
        ):
            raise FloorConfigurationError("Floor history entries require a finite Decimal floor.")
        if not isinstance(self.justification, str) or not self.justification.strip():
            raise FloorConfigurationError("Floor history entries require a justification.")
        if len(self.justification) > _FLOOR_MAX_JUSTIFICATION_LENGTH:
            raise FloorConfigurationError("Floor history entry justification is too long.")
        if not isinstance(self.recorded_at, datetime):
            raise FloorConfigurationError("Floor history entries require a timestamp.")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise FloorConfigurationError(
                "Floor history entries require a timezone-aware timestamp."
            )

    def as_mapping(self) -> dict[str, object]:
        return {
            "floor": _floor_text(self.floor),
            "justification": self.justification,
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CategoryFloor:
    """The current floor and its complete recorded history."""

    category: str
    floor: Decimal
    history: tuple[FloorHistoryEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.category, str) or not self.category.strip():
            raise FloorConfigurationError("Category floors require a non-blank category.")
        if not isinstance(self.floor, Decimal):
            raise FloorConfigurationError(f"Floor for {self.category!r} must be a Decimal.")
        if not self.history:
            raise FloorConfigurationError(f"Floor history for {self.category!r} is empty.")
        if self.floor != self.history[-1].floor:
            raise FloorConfigurationError(
                f"Floor for {self.category!r} does not match its latest recorded history entry."
            )

    def as_mapping(self) -> dict[str, object]:
        return {
            "floor": _floor_text(self.floor),
            "history": [entry.as_mapping() for entry in self.history],
        }


@dataclass(frozen=True, slots=True)
class FloorLedger:
    """Validated score floors used by the regression gate."""

    categories: Mapping[str, CategoryFloor]
    schema_version: int = _FLOOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _FLOOR_SCHEMA_VERSION:
            raise FloorConfigurationError(
                f"Unsupported score-floor schema version {self.schema_version!r}."
            )
        object.__setattr__(self, "categories", MappingProxyType(dict(self.categories)))
        if any(
            not isinstance(name, str)
            or not isinstance(category, CategoryFloor)
            or name != category.category
            for name, category in self.categories.items()
        ):
            raise FloorConfigurationError("Floor ledger category keys must match their entries.")

    def floor_for(self, category: str) -> Decimal | None:
        """Return a category floor, or ``None`` when that category is ungated."""

        entry = self.categories.get(category)
        return entry.floor if entry is not None else None

    def as_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "categories": {
                name: category.as_mapping() for name, category in self.categories.items()
            },
        }


@dataclass(frozen=True, slots=True)
class FloorFailure:
    """One actionable score-floor gate failure."""

    category: str
    floor: Decimal | None
    observed: Decimal | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.category, str) or not self.category.strip():
            raise ValueError("FloorFailure.category must be non-blank.")
        if self.reason not in {"below_floor", "missing_floor", "no_observation"}:
            raise ValueError(f"Unknown score-floor failure reason {self.reason!r}.")

    def message(self) -> str:
        if self.reason == "below_floor":
            return (
                f"FLOOR VIOLATION: category={self.category} floor={_floor_text(self.floor)} "
                f"observed={_floor_text(self.observed)}"
            )
        if self.reason == "missing_floor":
            return (
                f"FLOOR MISSING: category={self.category} floor=N/A observed="
                f"{_floor_text(self.observed)}; record a floor before this category is gated"
            )
        return (
            f"FLOOR UNOBSERVED: category={self.category} floor={_floor_text(self.floor)} "
            "observed=N/A; at least one applicable example is required"
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "category": self.category,
            "floor": _floor_text(self.floor),
            "observed": _floor_text(self.observed),
            "reason": self.reason,
            "message": self.message(),
        }


@dataclass(frozen=True, slots=True)
class EvaluationRunRecord:
    """Durable, JSON-shaped metadata for one evaluation invocation."""

    run_id: str
    commit_reference: str
    arm: str
    executed_at: datetime
    scores: Mapping[str, ArmScore]
    report: EvaluationReport | None
    discovery_issues: tuple[ExampleIssue, ...] = ()
    broken_error: str | None = None
    gate: bool = False
    floors_path: str | None = None
    floor_failures: tuple[FloorFailure, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "commit_reference": self.commit_reference,
            "commit_sha": self.commit_reference,
            "arm": self.arm,
            "executed_at": self.executed_at.isoformat(),
            "passed": self.broken_error is None
            and all(score.misses == 0 for score in self.scores.values()),
            "broken_error": self.broken_error,
            "gate": self.gate,
            "floors_path": self.floors_path,
            "gate_passed": self.gate and self.broken_error is None and not self.floor_failures,
            "floor_failures": [failure.as_mapping() for failure in self.floor_failures],
            "discovery_issues": [
                {"path": str(issue.path), "reason": issue.reason} for issue in self.discovery_issues
            ],
            "scores": {name: score.as_mapping() for name, score in self.scores.items()},
            "report": self.report.as_mapping() if self.report is not None else None,
        }


@dataclass(frozen=True, slots=True)
class EvaluationRunResult:
    """The complete outcome returned by :func:`run_evaluation`."""

    record: EvaluationRunRecord
    path: Path | None
    text: str

    @property
    def exit_code(self) -> int:
        if self.record.broken_error is not None:
            return 2
        return 1 if self.record.floor_failures else 0

    @property
    def broken(self) -> bool:
        return self.record.broken_error is not None


def _floor_text(value: Decimal | None) -> str:
    return "N/A" if value is None else format(value, "f")


def _floor_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise FloorConfigurationError(f"{field} must be a finite decimal between 0 and 1.")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (AttributeError, InvalidOperation, TypeError, ValueError) as error:
        raise FloorConfigurationError(
            f"{field} must be a finite decimal between 0 and 1."
        ) from error
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
        raise FloorConfigurationError(f"{field} must be a finite decimal between 0 and 1.")
    return parsed


def _floor_category(value: object) -> str:
    if not isinstance(value, str):
        raise FloorConfigurationError("Floor category names must be text.")
    category = value.strip()
    if (
        not category
        or len(category) > _FLOOR_CATEGORY_MAX_LENGTH
        or re.fullmatch(_FLOOR_CATEGORY_PATTERN, category) is None
    ):
        raise FloorConfigurationError(
            "Floor category names must be safe identifiers of at most "
            f"{_FLOOR_CATEGORY_MAX_LENGTH} characters."
        )
    return category


def _floor_justification(value: object, field: str = "justification") -> str:
    if not isinstance(value, str) or not value.strip():
        raise FloorConfigurationError(f"{field} must be a non-blank string.")
    justification = value.strip()
    if len(justification) > _FLOOR_MAX_JUSTIFICATION_LENGTH:
        raise FloorConfigurationError(
            f"{field} exceeds {_FLOOR_MAX_JUSTIFICATION_LENGTH} characters."
        )
    return justification


def _floor_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FloorConfigurationError(f"{field} must be a timezone-aware ISO timestamp.")
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise FloorConfigurationError(f"{field} must be a timezone-aware ISO timestamp.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise FloorConfigurationError(f"{field} must be a timezone-aware ISO timestamp.")
    return timestamp


def _floor_history(category: str, raw_history: object) -> tuple[FloorHistoryEntry, ...]:
    if not isinstance(raw_history, Sequence) or isinstance(raw_history, str | bytes | bytearray):
        raise FloorConfigurationError(f"Floor history for {category!r} must be an array.")
    if not raw_history:
        raise FloorConfigurationError(f"Floor history for {category!r} must not be empty.")
    entries: list[FloorHistoryEntry] = []
    for index, raw_entry in enumerate(raw_history):
        if not isinstance(raw_entry, Mapping):
            raise FloorConfigurationError(
                f"Floor history entry {category!r}[{index}] must be an object."
            )
        entry_floor = _floor_decimal(raw_entry.get("floor"), f"{category!r} history floor")
        entry = FloorHistoryEntry(
            floor=entry_floor,
            justification=_floor_justification(
                raw_entry.get("justification"), f"{category!r} history justification"
            ),
            recorded_at=_floor_timestamp(
                raw_entry.get("recorded_at"), f"{category!r} history recorded_at"
            ),
        )
        if entries and entry.floor < entries[-1].floor:
            raise FloorConfigurationError(
                f"Floor for {category!r} was lowered in its recorded history "
                f"({entry.floor} after {entries[-1].floor})."
            )
        if entries and entry.recorded_at < entries[-1].recorded_at:
            raise FloorConfigurationError(
                f"Floor history timestamps for {category!r} are not chronological."
            )
        entries.append(entry)
    return tuple(entries)


def load_floors(path: Path | str = DEFAULT_FLOORS_PATH) -> FloorLedger:
    """Load and validate the versioned, monotonic score-floor ledger."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FloorConfigurationError(
            f"Score-floor ledger could not be loaded: {source}."
        ) from error
    if not isinstance(payload, Mapping):
        raise FloorConfigurationError("Score-floor ledger root must be an object.")
    if payload.get("schema_version") != _FLOOR_SCHEMA_VERSION:
        raise FloorConfigurationError(
            f"Score-floor ledger schema_version must be {_FLOOR_SCHEMA_VERSION}."
        )
    raw_categories = payload.get("categories")
    if not isinstance(raw_categories, Mapping) or not raw_categories:
        raise FloorConfigurationError("Score-floor ledger must contain categories.")
    categories: dict[str, CategoryFloor] = {}
    for raw_category, raw_value in raw_categories.items():
        category = _floor_category(raw_category)
        if category in categories:
            raise FloorConfigurationError(f"Duplicate score-floor category {category!r}.")
        if not isinstance(raw_value, Mapping):
            raise FloorConfigurationError(f"Floor entry for {category!r} must be an object.")
        history = _floor_history(category, raw_value.get("history"))
        current_floor = _floor_decimal(raw_value.get("floor"), f"floor for {category!r}")
        if current_floor != history[-1].floor:
            raise FloorConfigurationError(
                f"Floor for {category!r} does not match its latest recorded history entry; "
                "floor changes must be recorded with a justification."
            )
        categories[category] = CategoryFloor(category, current_floor, history)
    return FloorLedger(categories=categories, schema_version=_FLOOR_SCHEMA_VERSION)


def check_score_floors(scores: ArmScore, ledger: FloorLedger) -> tuple[FloorFailure, ...]:
    """Return every floor violation for one arm, without short-circuiting."""

    if not isinstance(scores, ArmScore):
        raise TypeError("check_score_floors requires an ArmScore.")
    if not isinstance(ledger, FloorLedger):
        raise TypeError("check_score_floors requires a FloorLedger.")
    failures: list[FloorFailure] = []
    for category in sorted(scores.categories):
        category_score = scores.categories[category]
        floor = ledger.floor_for(category)
        if floor is None:
            failures.append(
                FloorFailure(
                    category=category,
                    floor=None,
                    observed=category_score.score,
                    reason="missing_floor",
                )
            )
        elif category_score.score is None:
            failures.append(
                FloorFailure(
                    category=category,
                    floor=floor,
                    observed=None,
                    reason="no_observation",
                )
            )
        elif category_score.score < floor:
            failures.append(
                FloorFailure(
                    category=category,
                    floor=floor,
                    observed=category_score.score,
                    reason="below_floor",
                )
            )
    return tuple(failures)


def _floor_gate_text(
    scores: ArmScore,
    ledger: FloorLedger | None,
    failures: Sequence[FloorFailure],
    error: str | None,
) -> str:
    if error is not None:
        return f"REGRESSION GATE BROKEN: {error}"
    if ledger is None:  # pragma: no cover - defensive invariant
        return "REGRESSION GATE BROKEN: no score-floor ledger was loaded."
    lines = ["REGRESSION GATE", "CATEGORY             FLOOR     OBSERVED  STATUS", "-" * 52]
    failed_by_category = {failure.category: failure for failure in failures}
    for category in sorted(scores.categories):
        category_score = scores.categories[category]
        floor = ledger.floor_for(category)
        failure = failed_by_category.get(category)
        status = "FAIL" if failure is not None else "PASS"
        lines.append(
            f"{category:<20}{_floor_text(floor):>9}  "
            f"{_floor_text(category_score.score):>9}  {status}"
        )
    if failures:
        lines.append("REGRESSION GATE FAILED")
        lines.extend(failure.message() for failure in failures)
    else:
        lines.append("REGRESSION GATE PASSED")
    return "\n".join(lines)


def raise_floor(
    category: str,
    floor: object,
    justification: str,
    *,
    path: Path | str = DEFAULT_FLOORS_PATH,
    recorded_at: datetime | None = None,
) -> FloorLedger:
    """Raise one floor atomically, requiring an auditable justification."""

    name = _floor_category(category)
    new_floor = _floor_decimal(floor, f"floor for {name!r}")
    reason = _floor_justification(justification)
    timestamp = recorded_at or datetime.now(UTC)
    if not isinstance(timestamp, datetime):
        raise FloorConfigurationError("recorded_at must be a datetime.")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise FloorConfigurationError("recorded_at must be timezone-aware.")
    ledger = load_floors(path)
    existing = ledger.categories.get(name)
    if existing is not None and new_floor <= existing.floor:
        raise FloorConfigurationError(
            f"New floor for {name!r} must be greater than its current floor "
            f"{_floor_text(existing.floor)}; floors never move downward."
        )
    if existing is not None and timestamp < existing.history[-1].recorded_at:
        raise FloorConfigurationError(
            f"recorded_at for {name!r} cannot precede its latest floor history entry."
        )
    history = (
        (
            *existing.history,
            FloorHistoryEntry(new_floor, reason, timestamp),
        )
        if existing is not None
        else (FloorHistoryEntry(new_floor, reason, timestamp),)
    )
    categories = dict(ledger.categories)
    categories[name] = CategoryFloor(name, new_floor, history)
    updated = FloorLedger(categories=categories, schema_version=ledger.schema_version)
    _atomic_json_write(updated.as_mapping(), Path(path))
    return updated


def _schema(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaValidationError) as error:
        raise ExampleDiscoveryError(f"Evaluation schema could not be loaded: {path}.") from error
    if not isinstance(payload, dict):
        raise ExampleDiscoveryError("Evaluation schema must be a JSON object.")
    return payload


def _safe_example_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return path.is_file() and not path.is_symlink()


def load_examples(directory: Path | str = DEFAULT_EXAMPLES_DIR) -> DiscoveryResult:
    """Load valid examples and retain malformed files as named issues.

    Duplicate ids are a broken example set rather than an example-level miss:
    allowing either duplicate to score would make a result depend on file
    ordering and would undermine the versioned dataset contract.
    """

    root = Path(directory)
    schema_path = root / "_schema.json"
    schema = _schema(schema_path)
    validator = Draft202012Validator(schema)
    if not root.is_dir():
        raise ExampleDiscoveryError(f"Evaluation examples directory was not found: {root}.")

    examples: list[ExampleFile] = []
    issues: list[ExampleIssue] = []
    ids: dict[str, Path] = {}
    for path in sorted(root.glob("EX-*.json")):
        if not _safe_example_path(path, root):
            issues.append(ExampleIssue(path, "file is not a regular in-directory example file"))
            continue
        try:
            if path.stat().st_size > _MAX_EXAMPLE_BYTES:
                raise ValueError(f"file exceeds the {_MAX_EXAMPLE_BYTES}-byte limit")
            body = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(body, dict):
                raise ValueError("example root must be a JSON object")
            errors = sorted(validator.iter_errors(body), key=lambda item: list(item.path))
            if errors:
                error = errors[0]
                location = "/".join(str(part) for part in error.path) or "<root>"
                raise ValueError(f"schema violation at {location}: {error.message}")
            example_id = body.get("id")
            if not isinstance(example_id, str):
                raise ValueError("example id must be text")
            previous = ids.get(example_id)
            if previous is not None:
                raise ExampleDiscoveryError(
                    f"Duplicate example id {example_id!r} in {previous} and {path}."
                )
            ids[example_id] = path
            examples.append(ExampleFile(path=path, body=body))
        except ExampleDiscoveryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            issues.append(ExampleIssue(path, str(error)))
    if not examples and not issues:
        raise ExampleDiscoveryError(f"No EX-*.json examples found under {root}.")
    return DiscoveryResult(examples=tuple(examples), issues=tuple(issues))


def discover_examples(directory: Path | str = DEFAULT_EXAMPLES_DIR) -> tuple[ExampleFile, ...]:
    """Return the valid examples, while preserving the simple T-103 API."""

    return load_examples(directory).examples


def resolve_commit_reference() -> str:
    """Resolve a release reference without requiring Git on the host."""

    for key in _COMMIT_ENVIRONMENT_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value[:_COMMIT_MAX_LENGTH]
    try:
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return DEFAULT_COMMIT_REFERENCE
    value = completed.stdout.strip()
    return (
        value[:_COMMIT_MAX_LENGTH]
        if completed.returncode == 0 and value
        else DEFAULT_COMMIT_REFERENCE
    )


def _atomic_json_write(payload: object, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


def persist_run_record(
    record: EvaluationRunRecord, directory: Path | str = DEFAULT_RUNS_DIR
) -> Path:
    """Atomically retain one run record under the generated evaluation area."""

    destination = Path(directory) / f"{record.run_id}.json"
    return _atomic_json_write(record.as_mapping(), destination)


def _write(stream: TextIO, text: str) -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")


def _run_arm_example(
    example: ExampleFile,
    arm: str,
    *,
    cassette_path: Path,
    client: object | None,
) -> Mapping[str, object]:
    if arm == "product":
        if client is not None and not hasattr(client, "call"):
            raise TypeError("The product model client must expose call().")
        return product_arm.run_product_example(
            example.body,
            cassette_path=cassette_path,
            client=client,
        )
    return baseline_arm.run_baseline_example(example.body)


def _arm_table_or_scoreboard(
    scores: Mapping[str, ArmScore], report: EvaluationReport | None
) -> str:
    if report is not None:
        return render_scoreboard(report)
    return render_arm_table(scores["product"])


def run_evaluation(
    *,
    both_arms: bool = False,
    only: str | None = None,
    examples_dir: Path | str = DEFAULT_EXAMPLES_DIR,
    cassette_path: Path | str = DEFAULT_CASSETTES_DIR,
    runs_dir: Path | str = DEFAULT_RUNS_DIR,
    commit_reference: str | None = None,
    client: object | None = None,
    stream: TextIO | None = None,
    gate: bool = False,
    floors_path: Path | str = DEFAULT_FLOORS_PATH,
) -> EvaluationRunResult:
    """Run the requested arms, optionally enforcing the product score floors."""

    if not isinstance(both_arms, bool):
        raise TypeError("both_arms must be boolean.")
    if not isinstance(gate, bool):
        raise TypeError("gate must be boolean.")
    if only is not None and (not isinstance(only, str) or not only.strip()):
        raise ValueError("only must be a non-blank example id when provided.")
    discovery = load_examples(examples_dir)
    selected = discovery.examples
    if only is not None:
        selected = tuple(example for example in selected if example.id == only)
        if not selected:
            raise ExampleDiscoveryError(f"No valid example named {only!r} was found.")
    arms = ("product", "baseline") if both_arms else ("product",)
    cassette = Path(cassette_path)
    scores_by_arm: dict[str, tuple[ExampleScore, ...]] = {}
    broken_error: str | None = None

    for arm in arms:
        collected_scores: list[ExampleScore] = []
        for example in selected:
            declared_arms = example.body.get("arms")
            if not isinstance(declared_arms, Sequence) or arm not in declared_arms:
                continue
            try:
                actual = _run_arm_example(
                    example,
                    arm,
                    cassette_path=cassette,
                    client=client,
                )
                if not isinstance(actual, Mapping):
                    raise TypeError("an evaluation arm must return a mapping")
                collected_scores.append(score_example(example.body, actual, arm=arm))
            except EvaluationSkip as error:
                collected_scores.append(skipped_example(example.body, arm=arm, reason=str(error)))
            except (EvaluationError, KeyError, TypeError, ValueError, ArithmeticError) as error:
                broken_error = f"{arm} arm failed on {example.id}: {error}"
                break
            except Exception as error:  # pragma: no cover - defensive harness boundary
                broken_error = f"{arm} arm failed on {example.id}: {error.__class__.__name__}"
                break
        scores_by_arm[arm] = tuple(collected_scores)
        if broken_error is not None:
            break

    example_bodies = tuple(example.body for example in selected)
    arm_scores = {
        arm: score_arm(example_bodies, scores_by_arm.get(arm, ()), arm=arm) for arm in arms
    }
    report = (
        build_report(
            arm_scores["product"], arm_scores["baseline"], commit_reference=commit_reference
        )
        if both_arms
        else None
    )
    resolved_commit = commit_reference or resolve_commit_reference()
    if report is not None and report.commit_reference != resolved_commit:
        report = build_report(
            arm_scores["product"], arm_scores["baseline"], commit_reference=resolved_commit
        )
    floor_ledger: FloorLedger | None = None
    floor_failures: tuple[FloorFailure, ...] = ()
    floor_error: str | None = None
    if gate and broken_error is None:
        try:
            floor_ledger = load_floors(floors_path)
            floor_failures = check_score_floors(arm_scores["product"], floor_ledger)
        except (EvaluationError, TypeError, ValueError) as error:
            floor_error = str(error)
            broken_error = f"score-floor gate could not run: {floor_error}"
    record = EvaluationRunRecord(
        run_id=str(uuid.uuid4()),
        commit_reference=resolved_commit,
        arm="both" if both_arms else "product",
        executed_at=datetime.now(UTC),
        scores=arm_scores,
        report=report,
        discovery_issues=discovery.issues,
        broken_error=broken_error,
        gate=gate,
        floors_path=str(floors_path) if gate else None,
        floor_failures=floor_failures,
    )
    path: Path | None = None
    try:
        path = persist_run_record(record, runs_dir)
        if report is not None:
            persist_report(report, path.with_suffix(".scoreboard.json"))
    except OSError as error:
        record = EvaluationRunRecord(
            run_id=record.run_id,
            commit_reference=record.commit_reference,
            arm=record.arm,
            executed_at=record.executed_at,
            scores=record.scores,
            report=record.report,
            discovery_issues=record.discovery_issues,
            broken_error=f"could not store evaluation run: {error}",
            gate=record.gate,
            floors_path=record.floors_path,
            floor_failures=record.floor_failures,
        )
        try:
            path = persist_run_record(record, runs_dir)
        except OSError:
            path = None
    text = _arm_table_or_scoreboard(arm_scores, report)
    if discovery.issues:
        issue_lines = "\n".join(
            f"SKIP {issue.path.name} — {issue.reason}" for issue in discovery.issues
        )
        text = f"{issue_lines}\n{text}"
    if record.broken_error is not None:
        text = f"BROKEN evaluation run — {record.broken_error}\n{text}"
    if gate:
        text = f"{text}\n\n" + _floor_gate_text(
            arm_scores["product"], floor_ledger, floor_failures, floor_error
        )
    if stream is not None:
        _write(stream, text)
        if path is not None:
            _write(stream, f"STORED {path}")
    return EvaluationRunResult(record=record, path=path, text=text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the offline Covenant Radar evaluation harness"
    )
    parser.add_argument("--both-arms", action="store_true", help="run product and baseline arms")
    parser.add_argument("--only", metavar="EX-####", help="run one example by stable id")
    parser.add_argument("--examples-dir", type=Path, default=DEFAULT_EXAMPLES_DIR)
    parser.add_argument("--cassettes", type=Path, default=DEFAULT_CASSETTES_DIR)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument(
        "--floors",
        "--floors-file",
        dest="floors_path",
        type=Path,
        default=DEFAULT_FLOORS_PATH,
        help="Versioned score-floor ledger used by --gate or --raise-floor",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="fail with exit code 1 when a product category is below its recorded floor",
    )
    parser.add_argument(
        "--raise-floor",
        nargs=2,
        metavar=("CATEGORY", "SCORE"),
        help="raise one category floor (requires --justification)",
    )
    parser.add_argument(
        "--justification",
        help="auditable reason for a --raise-floor change",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.raise_floor is not None:
        if args.gate or args.both_arms or args.only is not None:
            _write(
                sys.stderr,
                "--raise-floor cannot be combined with --gate, --both-arms, or --only.",
            )
            return 2
        if args.justification is None or not args.justification.strip():
            _write(sys.stderr, "--raise-floor requires a non-blank --justification.")
            return 2
        category, floor = args.raise_floor
        try:
            updated = raise_floor(
                category,
                floor,
                args.justification,
                path=args.floors_path,
            )
        except (EvaluationError, TypeError, ValueError) as error:
            _write(sys.stderr, f"Could not raise score floor: {error}")
            return 2
        normalized_category = category.strip()
        _write(
            sys.stdout,
            f"RAISED FLOOR {normalized_category} TO "
            f"{_floor_text(updated.categories[normalized_category].floor)} "
            f"IN {args.floors_path}",
        )
        return 0
    if args.justification is not None:
        _write(sys.stderr, "--justification is only valid with --raise-floor.")
        return 2
    try:
        result = run_evaluation(
            both_arms=args.both_arms,
            only=args.only,
            examples_dir=args.examples_dir,
            cassette_path=args.cassettes,
            runs_dir=args.runs_dir,
            stream=sys.stdout,
            gate=args.gate,
            floors_path=args.floors_path,
        )
    except EvaluationError as error:
        _write(sys.stderr, f"Evaluation run failed: {error}")
        return 2
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


run = run_evaluation
RunRecord = EvaluationRunRecord


__all__ = [
    "CategoryFloor",
    "DEFAULT_FLOORS_PATH",
    "DiscoveryResult",
    "EvaluationRunRecord",
    "EvaluationRunResult",
    "ExampleFile",
    "FloorConfigurationError",
    "FloorFailure",
    "FloorHistoryEntry",
    "FloorLedger",
    "RunRecord",
    "build_parser",
    "check_score_floors",
    "discover_examples",
    "load_floors",
    "load_examples",
    "main",
    "persist_run_record",
    "raise_floor",
    "resolve_commit_reference",
    "run",
    "run_evaluation",
]
