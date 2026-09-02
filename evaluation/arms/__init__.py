"""Evaluation arms.

The product and baseline arms expose the same small protocol: accept one
validated example and return JSON-shaped facts for the shared scorer.
"""

from __future__ import annotations

from evaluation.arms.baseline import run_baseline_example
from evaluation.arms.product import run_product_example

__all__ = ["run_baseline_example", "run_product_example"]
