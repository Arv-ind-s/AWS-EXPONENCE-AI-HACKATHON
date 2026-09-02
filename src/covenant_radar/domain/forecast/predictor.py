"""Persistence-neutral interfaces for governed statistical Stage-4 models.

The scoring service owns the decision of whether a prediction is usable.  A
predictor is deliberately a small, local port: implementations must never
perform network I/O or receive personal identifiers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FeatureContribution:
    """A signed, human-readable contribution to one statistical prediction."""

    name: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """Point-in-time, non-identifying numerical features used for prediction."""

    values: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        clean: dict[str, Decimal] = {}
        for name, value in self.values.items():
            invalid_name = (
                not isinstance(name, str) or not name or name.lower().endswith(("id", "name"))
            )
            if invalid_name:
                raise ValueError(
                    "Feature snapshots may contain only named non-identifying features."
                )
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"Feature {name!r} must be a finite Decimal.")
            clean[name] = value
        object.__setattr__(self, "values", clean)

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            {key: str(value) for key, value in sorted(self.values.items())}, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Prediction:
    """One calibrated probability from a registered local model artifact."""

    probability: Decimal
    model_version: str
    artifact_checksum: str
    contributions: Sequence[FeatureContribution] = ()

    def __post_init__(self) -> None:
        valid_probability = (
            isinstance(self.probability, Decimal)
            and Decimal("0") <= self.probability <= Decimal("1")
        )
        if not valid_probability:
            raise ValueError("Prediction probability must be a Decimal between zero and one.")
        if not self.model_version.strip() or not self.artifact_checksum.strip():
            raise ValueError("Prediction requires a model version and artifact checksum.")


class ForecastPredictor(Protocol):
    """Local Stage-4 probability adapter selected only after governance approval."""

    def predict(self, snapshot: FeatureSnapshot, *, horizon_days: int) -> Prediction:
        """Return the calibrated probability for one supported horizon."""
