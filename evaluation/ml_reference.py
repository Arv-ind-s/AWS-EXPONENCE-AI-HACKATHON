"""Reproducible synthetic backtest for the governed Stage-4 ML challengers.

This is intentionally a demonstration harness.  Its report is never a claim
about a customer's portfolio and must not be used to approve a live champion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import UUID

from covenant_radar.domain.forecast import FeatureSnapshot
from covenant_radar.ml.forecast import SklearnForecastPredictor, TrainingRow, train_candidates
from evaluation.reference_portfolio.cohorts import generate_reference_cohorts
from evaluation.reference_portfolio.generator import (
    ReferencePortfolioConfig,
    generate_reference_portfolio,
)

_HORIZONS: Final = (30, 60, 90)
_THRESHOLD: Final = Decimal("85")


@dataclass(frozen=True, slots=True)
class DatedTrainingRow:
    as_of_date: date
    borrower_id: UUID
    row: TrainingRow


def synthetic_rows() -> tuple[DatedTrainingRow, ...]:
    """Build point-in-time utilisation snapshots and derived future labels."""

    portfolio = generate_reference_portfolio(
        ReferencePortfolioConfig(seed=17, borrower_count=96, facility_count=192, quarter_count=4)
    )
    dataset = generate_reference_cohorts(portfolio, authored_cohort_size=24, signal_days=365)
    breach_dates = {item.borrower_id: item.breach_date for item in dataset.labels}
    rows: list[DatedTrainingRow] = []
    for borrower in dataset.portfolio.borrowers:
        events = tuple(dataset.signals.events_for_borrower(borrower.id, family="utilisation"))
        breach_date = breach_dates.get(borrower.id)
        # Seven-day sampling prevents adjacent near-duplicate rows leaking into evaluation.
        for index in range(14, len(events), 7):
            current = events[index]
            prior = events[index - 14]
            slope = (current.magnitude - prior.magnitude) / Decimal("14")
            snapshot = FeatureSnapshot(
                {
                    "current_value": current.magnitude,
                    "threshold": _THRESHOLD,
                    "signed_headroom": (_THRESHOLD - current.magnitude) / _THRESHOLD,
                    "slope": slope,
                    "net_per_day_drift": slope,
                    "evidence_pressure": max(Decimal("0"), slope),
                    "completeness": Decimal("1"),
                    "evidence_support": Decimal("1"),
                    "staleness_days": Decimal("0"),
                    "observation_count": Decimal(index + 1),
                    "direction_max": Decimal("1"),
                }
            )
            labels = {
                horizon: bool(
                    breach_date is not None
                    and current.event_date
                    < breach_date
                    <= current.event_date.fromordinal(current.event_date.toordinal() + horizon)
                )
                for horizon in _HORIZONS
            }
            rows.append(
                DatedTrainingRow(current.event_date, borrower.id, TrainingRow(snapshot, labels))
            )
    return tuple(rows)


def run(output_directory: Path | str = "var/ml-reference") -> dict[str, object]:
    """Train on early dates/borrowers and report a later held-out cohort."""

    rows = synthetic_rows()
    dates = sorted({item.as_of_date for item in rows})
    # The reference outcome window is deliberately late in the generated year.
    # An 85% cutoff leaves positive labels in both temporal partitions; borrower
    # holdout prevents adjacent observations of one entity leaking across them.
    cutoff = dates[int(len(dates) * 0.85)]
    held_out_borrowers = {item.borrower_id for item in rows if item.borrower_id.int % 4 == 0}
    train = tuple(
        item.row
        for item in rows
        if item.as_of_date < cutoff and item.borrower_id not in held_out_borrowers
    )
    test = tuple(
        item.row
        for item in rows
        if item.as_of_date >= cutoff and item.borrower_id in held_out_borrowers
    )
    paths = train_candidates(
        train, artifact_directory=output_directory, version="synthetic-reference-v1"
    )
    report = {
        "dataset": "synthetic_reference_portfolio_only",
        "warning": "Not calibrated for, or promotable to, a customer portfolio.",
        "split": {
            "strategy": "chronological_with_borrower_holdout",
            "cutoff": cutoff.isoformat(),
            "train_rows": len(train),
            "test_rows": len(test),
            "held_out_borrowers": len(held_out_borrowers),
        },
        "models": {path.stem: _metrics(SklearnForecastPredictor(path), test) for path in paths},
    }
    target = Path(output_directory) / "report.json"
    target.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def _metrics(
    predictor: SklearnForecastPredictor, rows: tuple[TrainingRow, ...]
) -> dict[str, object]:
    from sklearn.metrics import (
        average_precision_score,
        brier_score_loss,
        precision_score,
        recall_score,
    )

    result: dict[str, object] = {"artifact_checksum": predictor.checksum, "horizons": {}}
    for horizon in _HORIZONS:
        truth = [int(row.labels[horizon]) for row in rows]
        probabilities = [
            float(predictor.predict(row.snapshot, horizon_days=horizon).probability) for row in rows
        ]
        decisions = [value >= 0.70 for value in probabilities]
        result["horizons"][str(horizon)] = {
            "positive_count": sum(truth),
            "average_precision": round(float(average_precision_score(truth, probabilities)), 6),
            "brier_score": round(float(brier_score_loss(truth, probabilities)), 6),
            "precision_at_0_70": round(
                float(precision_score(truth, decisions, zero_division=0)), 6
            ),
            "recall_at_0_70": round(float(recall_score(truth, decisions, zero_division=0)), 6),
            "false_escalation_rate_at_0_70": round(
                sum(
                    int(predicted and not actual)
                    for predicted, actual in zip(decisions, truth, strict=True)
                )
                / len(truth),
                6,
            ),
        }
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))  # noqa: T201
