"""Offline-only training and local artifact adapters for governed ML."""

from covenant_radar.ml.forecast import SklearnForecastPredictor, train_candidates

__all__ = ["SklearnForecastPredictor", "train_candidates"]
