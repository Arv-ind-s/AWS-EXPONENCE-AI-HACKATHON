from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import FeatureContribution, FeatureSnapshot, Prediction


def test_feature_snapshot_is_non_identifying_and_content_addressed() -> None:
    snapshot = FeatureSnapshot({"headroom": Decimal("0.2"), "slope": Decimal("-0.01")})

    assert len(snapshot.content_hash) == 64
    assert snapshot.content_hash == FeatureSnapshot(
        {"slope": Decimal("-0.01"), "headroom": Decimal("0.2")}
    ).content_hash
    with pytest.raises(ValueError, match="non-identifying"):
        FeatureSnapshot({"borrower_id": Decimal("1")})


def test_prediction_enforces_calibrated_probability_bounds() -> None:
    prediction = Prediction(
        Decimal("0.7"), "stage4:v1", "a" * 64, (FeatureContribution("headroom", Decimal("0.1")),)
    )
    assert prediction.probability == Decimal("0.7")
    with pytest.raises(ValueError, match="between zero and one"):
        Prediction(Decimal("1.1"), "stage4:v1", "a" * 64)
