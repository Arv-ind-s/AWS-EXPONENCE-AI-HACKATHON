"""Offline evaluation harness for Covenant Radar.

The evaluation package is deliberately separate from the application runtime.
It owns example discovery, arm orchestration and score persistence, while the
product arm delegates calculations and shape checks to the production domain
and AI-boundary modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
DEFAULT_RUNS_DIR = Path("var") / "evaluation" / "runs"
DEFAULT_CASSETTES_DIR = Path("evaluation") / "cassettes"
DEFAULT_COMMIT_REFERENCE = "unversioned"


class EvaluationError(RuntimeError):
    """Base class for failures in the harness itself or its input set."""


class ExampleDiscoveryError(EvaluationError):
    """The example set cannot be discovered as a coherent collection."""


class EvaluationSkip(EvaluationError):
    """A valid example could not be attempted without weakening the run."""


@dataclass(frozen=True, slots=True)
class ExampleIssue:
    """A malformed example that was named and skipped by a run."""

    path: Path
    reason: str


__all__ = [
    "DEFAULT_CASSETTES_DIR",
    "DEFAULT_COMMIT_REFERENCE",
    "DEFAULT_EXAMPLES_DIR",
    "DEFAULT_RUNS_DIR",
    "EvaluationError",
    "EvaluationSkip",
    "ExampleDiscoveryError",
    "ExampleIssue",
]
