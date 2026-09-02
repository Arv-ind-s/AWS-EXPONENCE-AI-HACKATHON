"""Local sklearn training and inference for the Stage-4 challenger.

Training accepts point-in-time rows prepared by an ingestion/backfill job;
it deliberately does not query live application tables, making leakage review
and offline reproducibility straightforward.
"""

from __future__ import annotations

import hashlib
import json
import pickle
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from covenant_radar.domain.forecast.predictor import (
    FeatureContribution,
    FeatureSnapshot,
    Prediction,
)

_HORIZONS = (30, 60, 90)


@dataclass(frozen=True, slots=True)
class TrainingRow:
    """One leakage-free row, with labels formed after the scoring instant."""

    snapshot: FeatureSnapshot
    labels: Mapping[int, bool]


class SklearnForecastPredictor:
    """Checksum-verified, local multi-horizon artifact adapter."""

    def __init__(self, artifact_path: Path | str) -> None:
        self.path = Path(artifact_path)
        raw = self.path.read_bytes()
        self.checksum = hashlib.sha256(raw).hexdigest()
        payload = pickle.loads(raw)
        self.version = str(payload["version"])
        self.features = tuple(payload["features"])
        self.models = payload["models"]

    def predict(self, snapshot: FeatureSnapshot, *, horizon_days: int) -> Prediction:
        if horizon_days not in self.models:
            raise ValueError(f"Artifact does not support the {horizon_days}-day horizon.")
        missing = set(self.features) - set(snapshot.values)
        if missing:
            raise ValueError("Required ML features are missing: " + ", ".join(sorted(missing)))
        import numpy as np

        model = self.models[horizon_days]
        vector = np.array([[float(snapshot.values[name]) for name in self.features]])
        value = Decimal(str(model.predict_proba(vector)[0][1]))
        contributions = _contributions(model, vector[0], self.features)
        return Prediction(value, self.version, self.checksum, contributions)


def train_candidates(
    rows: Iterable[TrainingRow], *, artifact_directory: Path | str, version: str
) -> tuple[Path, Path]:
    """Train calibrated linear and gradient-boosted local challengers.

    Rows must already be chronologically split by the caller.  The generated
    artifacts are immutable content-addressed files and contain no identifiers
    or raw source records.
    """

    values = tuple(rows)
    if len(values) < 10:
        raise ValueError("At least ten labelled point-in-time rows are required for training.")
    features = tuple(sorted(values[0].snapshot.values))
    if any(tuple(sorted(row.snapshot.values)) != features for row in values):
        raise ValueError("Training rows must have the identical allow-listed feature schema.")
    import numpy as np
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    matrix = np.array([[float(row.snapshot.values[name]) for name in features] for row in values])
    constructors = {
        "logistic": lambda: LogisticRegression(max_iter=1000, class_weight="balanced"),
        "gradient_boosted": lambda: HistGradientBoostingClassifier(random_state=0),
    }
    output = Path(artifact_directory)
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for family, constructor in constructors.items():
        models: dict[int, Any] = {}
        for horizon in _HORIZONS:
            labels = np.array([int(row.labels[horizon]) for row in values])
            if len(set(labels)) < 2:
                raise ValueError(f"The {horizon}-day labels must contain both outcome classes.")
            classifier = constructor()  # type: ignore[no-untyped-call]
            models[horizon] = CalibratedClassifierCV(classifier, method="sigmoid", cv=3).fit(
                matrix, labels
            )
        payload = {
            "version": f"{version}:{family}",
            "family": family,
            "features": features,
            "models": models,
        }
        raw = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        checksum = hashlib.sha256(raw).hexdigest()
        path = output / f"stage4-{family}-{checksum[:12]}.pkl"
        path.write_bytes(raw)
        path.with_suffix(".manifest.json").write_text(
            json.dumps(
                {
                    "version": payload["version"],
                    "family": family,
                    "features": features,
                    "checksum": checksum,
                    "horizons": _HORIZONS,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        written.append(path)
    return written[0], written[1]


def _contributions(
    model: object, vector: Any, features: tuple[str, ...]
) -> tuple[FeatureContribution, ...]:
    """Use exact linear contributions where available; otherwise disclose none."""
    estimator = getattr(model, "calibrated_classifiers_", [None])[0]
    base = getattr(estimator, "estimator", None)
    coefficients = getattr(base, "coef_", None)
    if coefficients is None:
        return ()
    return tuple(
        FeatureContribution(name, Decimal(str(float(coefficient) * float(value))))
        for name, coefficient, value in zip(features, coefficients[0], vector, strict=True)
    )
