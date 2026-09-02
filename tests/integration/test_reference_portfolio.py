"""Integration checks for the deterministic evaluation reference portfolio."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models import Portfolio
from covenant_radar.domain.ratios.library import LIBRARY
from evaluation.reference_portfolio import (
    DEFAULT_REFERENCE_CONFIG,
    ReferencePortfolioConfig,
    ReferencePortfolioError,
    generate_reference_portfolio,
    load_reference_portfolio,
)
from evaluation.reference_portfolio.generator import CRILC_MINIMUM_EXPOSURE_CRORE
from evaluation.reference_portfolio.names import is_valid_cin, is_valid_pan

pytestmark = pytest.mark.integration

_CLOCK = FixedClock(datetime(2026, 8, 30, 12, 0, tzinfo=UTC))


def _small_config() -> ReferencePortfolioConfig:
    return ReferencePortfolioConfig(
        seed=17,
        borrower_count=24,
        facility_count=57,
        quarter_count=4,
    )


def test_two_runs_content_identical() -> None:
    first = generate_reference_portfolio(_small_config())
    second = generate_reference_portfolio(_small_config())

    assert first.content_hashes() == second.content_hashes()
    assert first.content_hash == second.content_hash


def test_sizes_match_specification() -> None:
    assert DEFAULT_REFERENCE_CONFIG.borrower_count == 5_000
    assert DEFAULT_REFERENCE_CONFIG.facility_count == 12_000
    assert DEFAULT_REFERENCE_CONFIG.quarter_count == 8
    portfolio = generate_reference_portfolio(
        ReferencePortfolioConfig(seed=19, borrower_count=7, facility_count=11, quarter_count=3)
    )
    assert len(portfolio.borrowers) == 7
    assert len(portfolio.facilities) == 11
    assert len(portfolio.financials) == 21


def test_every_facility_at_or_above_crilc_band() -> None:
    portfolio = generate_reference_portfolio(_small_config())

    assert all(
        facility.sanctioned_limit >= CRILC_MINIMUM_EXPOSURE_CRORE
        for facility in portfolio.facilities
    )
    assert all(
        facility.outstanding <= facility.drawing_power <= facility.sanctioned_limit
        for facility in portfolio.facilities
    )


def test_every_ratio_inside_plausible_band() -> None:
    portfolio = generate_reference_portfolio(_small_config())

    for period in portfolio.financials:
        assert set(period.ratios) == set(LIBRARY)
        for code, value in period.ratios.items():
            definition = LIBRARY[code]
            if definition.plausible_min is not None:
                assert value >= definition.plausible_min, (code, value)
            if definition.plausible_max is not None:
                assert value <= definition.plausible_max, (code, value)


def test_names_at_realistic_lengths() -> None:
    portfolio = generate_reference_portfolio(_small_config())

    assert min(len(borrower.legal_name) for borrower in portfolio.borrowers) >= 20
    assert len({borrower.legal_name for borrower in portfolio.borrowers}) == len(
        portfolio.borrowers
    )
    assert all(is_valid_cin(borrower.cin) for borrower in portfolio.borrowers)
    assert all(is_valid_pan(borrower.pan) for borrower in portfolio.borrowers)


def test_generation_into_non_empty_refused(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reference.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            now = _CLOCK.now()
            portfolio_id = new_id()
            session.add(
                Portfolio(
                    id=portfolio_id,
                    code="CUSTOM",
                    name="Existing Customer Portfolio",
                    parent_id=None,
                    branch_code="CUS",
                    path=f"{portfolio_id.hex}/",
                    created_at=now,
                    updated_at=now,
                    created_by_id=None,
                    updated_by_id=None,
                    request_id="existing-reference",
                    version=1,
                )
            )
            session.commit()
            with pytest.raises(ReferencePortfolioError, match="non-empty"):
                load_reference_portfolio(
                    session,
                    generate_reference_portfolio(_small_config()),
                    clock=_CLOCK,
                    request_id="reference-test",
                )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
