"""Property tests for signed covenant headroom."""

from __future__ import annotations

from decimal import Decimal

from covenant_radar.domain.covenants.headroom import signed_headroom

_THRESHOLDS = tuple(range(1, 21))
_VALUES = tuple(range(-10, 11))


def test_headroom_sign_correct_for_both_directions() -> None:
    for value in _VALUES:
        for threshold in _THRESHOLDS:
            observed = Decimal(value)
            limit = Decimal(threshold)

            min_headroom = signed_headroom(observed, limit, "min")
            max_headroom = signed_headroom(observed, limit, "max")

            assert (min_headroom > 0) is (observed > limit)
            assert (min_headroom < 0) is (observed < limit)
            assert (max_headroom > 0) is (observed < limit)
            assert (max_headroom < 0) is (observed > limit)


def test_headroom_zero_iff_value_equals_threshold() -> None:
    for value in _VALUES:
        for threshold in _THRESHOLDS:
            observed = Decimal(value)
            limit = Decimal(threshold)

            assert (signed_headroom(observed, limit, "min") == 0) is (observed == limit)
            assert (signed_headroom(observed, limit, "max") == 0) is (observed == limit)


def test_headroom_monotonic_in_value() -> None:
    for first in _VALUES:
        for second in _VALUES:
            for threshold in _THRESHOLDS:
                left = Decimal(first)
                right = Decimal(second)
                limit = Decimal(threshold)

                min_left = signed_headroom(left, limit, "min")
                min_right = signed_headroom(right, limit, "min")
                max_left = signed_headroom(left, limit, "max")
                max_right = signed_headroom(right, limit, "max")

                if left <= right:
                    assert min_left <= min_right
                    assert max_left >= max_right
                else:
                    assert min_left >= min_right
                    assert max_left <= max_right
