"""Integration checks for the T-025 statement import vertical slice.

Self-contained fixture style copied from `tests/integration/test_signal_
ingestion.py`: its own `sqlite:///:memory:` engine and `Base.metadata.
create_all`, not the PostgreSQL-only `tests/integration/conftest.py`
fixtures, so this suite runs without an external database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import ValidationError
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, Portfolio
from covenant_radar.db.models.statements import (
    FieldProvenance,
    FinancialPeriod,
    ImportBatch,
    ImportMapping,
    QuarantineRow,
    StatementLineValue,
)
from covenant_radar.db.scoping import Scope
from covenant_radar.ingestion.statements.validate import ColumnMismatchError
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.statements import StatementImportService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statements"

_MAPPING_SPEC = {
    "borrower_key_column": "borrower_key",
    "fy_label_column": "fy_label",
    "period_type_column": "period_type",
    "period_start_column": "period_start",
    "period_end_column": "period_end",
    "is_audited_column": "is_audited",
    "unit": "lakh",
    "currency": "INR",
    "sign": "as_reported",
    "columns": {
        "revenue_lakh": "revenue",
        "total_assets_lakh": "total_assets",
        "tangible_net_worth_lakh": "tangible_net_worth",
    },
    "totals_row": {"column": "borrower_key", "value": "TOTAL"},
}


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del actor, request_id
        self.events.append((event_type, subject, payload))
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.user = AppUser(
            id=uuid4(),
            username="statement-import",
            email="statement-import@example.com",
            full_name="Statement Import",
            auth_source="local",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t025-test-0001",
        )
        self.portfolio = Portfolio.create(
            code="STATEMENTS",
            name="Statement Portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t025-test-0002",
        )
        self.borrower_one = Borrower(
            id=uuid4(),
            reference="B-ST-001",
            legal_name="Statement Test Borrower One",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t025-test-0003",
        )
        self.borrower_two = Borrower(
            id=uuid4(),
            reference="B-ST-002",
            legal_name="Statement Test Borrower Two",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t025-test-0004",
        )
        self.session.add_all([self.user, self.portfolio, self.borrower_one, self.borrower_two])
        self.session.flush()

        self.mapping_csv = self._mapping("bank_extract_csv", "csv", _MAPPING_SPEC)
        self.mapping_xlsx = self._mapping("bank_extract_xlsx", "xlsx", _MAPPING_SPEC)
        self.mapping_json = self._mapping("bank_extract_json", "json", _MAPPING_SPEC)
        self.session.flush()

        self.principal = Principal.user(self.user.id, (Permission.INGEST_DATA,))
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.service = StatementImportService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t025-test-0005",
        )

    def _mapping(self, name: str, source_type: str, spec: dict[str, object]) -> ImportMapping:
        mapping = ImportMapping(
            id=uuid4(),
            name=name,
            source_type=source_type,
            version=1,
            spec=spec,
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t025-test-mapping",
        )
        self.session.add(mapping)
        return mapping

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def import_fixture(self, filename: str, *, source_type: str, mapping_name: str) -> object:
        content = (_FIXTURES / filename).read_bytes()
        return self.service.import_statements(
            self.principal,
            source_type=source_type,
            content=content,
            mapping_name=mapping_name,
            scope=self.scope,
        )

    def periods(self) -> list[FinancialPeriod]:
        return list(self.session.scalars(select(FinancialPeriod)))

    def line_values(self, period_id: UUID) -> dict[str, StatementLineValue]:
        rows = self.session.scalars(
            select(StatementLineValue).where(StatementLineValue.period_id == period_id)
        )
        return {row.line_code: row for row in rows}


def test_clean_extract_loads_and_reconciles() -> None:
    fixture = _Fixture()
    try:
        report = fixture.import_fixture(
            "clean_extract.csv", source_type="csv", mapping_name="bank_extract_csv"
        )
        fixture.session.commit()

        assert report.received == 3
        assert report.accepted == 2
        assert report.quarantined == 0
        assert report.totals_rows == 1
        assert report.reconciled is True
        assert report.discrepancies == ()

        periods = fixture.periods()
        assert len(periods) == 2
        by_borrower = {period.borrower_id: period for period in periods}
        one = by_borrower[fixture.borrower_one.id]
        assert one.fy_label == "FY26Q4"
        assert one.period_type == "annual"
        assert one.is_audited is True

        lines = fixture.line_values(one.id)
        assert lines["revenue"].value == Decimal("100.00000000")
        assert lines["revenue"].unit == "crore"
        assert lines["revenue"].currency == "INR"
    finally:
        fixture.close()


def test_reimport_is_idempotent() -> None:
    fixture = _Fixture()
    try:
        first = fixture.import_fixture(
            "clean_extract.csv", source_type="csv", mapping_name="bank_extract_csv"
        )
        fixture.session.commit()
        second = fixture.import_fixture(
            "clean_extract.csv", source_type="csv", mapping_name="bank_extract_csv"
        )
        fixture.session.commit()

        assert first.batch_id == second.batch_id
        assert first == second
        assert fixture.session.scalar(select(func.count(ImportBatch.id))) == 1
        assert fixture.session.scalar(select(func.count(FinancialPeriod.id))) == 2
    finally:
        fixture.close()


def test_bad_row_quarantined_rest_load() -> None:
    fixture = _Fixture()
    try:
        report = fixture.import_fixture(
            "bad_row.csv", source_type="csv", mapping_name="bank_extract_csv"
        )
        fixture.session.commit()

        assert report.accepted == 1
        assert report.quarantined == 1
        assert report.quarantine[0].rule_failed == "line_normalisation_failed"
        assert fixture.session.scalar(select(func.count(FinancialPeriod.id))) == 1
        assert fixture.session.scalar(select(func.count(QuarantineRow.id))) == 1
        quarantine_row = fixture.session.scalars(select(QuarantineRow)).one()
        assert quarantine_row.rule_failed == "line_normalisation_failed"
        assert quarantine_row.raw["revenue_lakh"] == "not-a-number"
    finally:
        fixture.close()


def test_column_mismatch_refused_before_write() -> None:
    fixture = _Fixture()
    try:
        with pytest.raises(ColumnMismatchError):
            fixture.import_fixture(
                "column_mismatch.csv",
                source_type="csv",
                mapping_name="bank_extract_csv",
            )
        fixture.session.rollback()

        assert fixture.session.scalar(select(func.count(ImportBatch.id))) == 0
        assert fixture.session.scalar(select(func.count(FinancialPeriod.id))) == 0
    finally:
        fixture.close()


def test_unit_and_currency_conversion_recorded() -> None:
    fixture = _Fixture()
    try:
        fixture.import_fixture(
            "clean_extract.csv", source_type="csv", mapping_name="bank_extract_csv"
        )
        fixture.session.commit()

        period = fixture.session.scalars(
            select(FinancialPeriod).where(FinancialPeriod.borrower_id == fixture.borrower_one.id)
        ).one()
        lines = fixture.line_values(period.id)
        assert lines["total_assets"].value == Decimal("500.00000000")
        assert lines["total_assets"].currency == "INR"
        assert lines["total_assets"].unit == "crore"

        provenance = fixture.session.get(FieldProvenance, lines["total_assets"].provenance_id)
        assert provenance is not None
        assert "unit=lakh->crore" in provenance.transform_note
        assert "currency=INR" in provenance.transform_note
        assert "sign=as_reported" in provenance.transform_note
    finally:
        fixture.close()


def test_unknown_borrower_key_quarantined() -> None:
    fixture = _Fixture()
    try:
        report = fixture.import_fixture(
            "unknown_borrower.csv", source_type="csv", mapping_name="bank_extract_csv"
        )
        fixture.session.commit()

        assert report.accepted == 1
        assert report.quarantined == 1
        assert report.quarantine[0].rule_failed == "unknown_borrower"
        assert fixture.session.scalar(select(func.count(Borrower.id))) == 2
    finally:
        fixture.close()


def test_totals_row_used_for_reconciliation_not_loaded() -> None:
    fixture = _Fixture()
    try:
        report = fixture.import_fixture(
            "clean_extract.csv", source_type="csv", mapping_name="bank_extract_csv"
        )
        fixture.session.commit()

        assert report.totals_rows == 1
        fy_labels = {period.fy_label for period in fixture.periods()}
        assert fy_labels == {"FY26Q4"}
        assert all(
            period.borrower_id in {fixture.borrower_one.id, fixture.borrower_two.id}
            for period in fixture.periods()
        )
        assert report.reconciled is True
    finally:
        fixture.close()


def test_report_counts_match_actuals() -> None:
    fixture = _Fixture()
    try:
        report = fixture.import_fixture(
            "bad_row.csv", source_type="csv", mapping_name="bank_extract_csv"
        )
        fixture.session.commit()

        assert report.received == report.accepted + report.quarantined + report.totals_rows
        assert fixture.session.scalar(select(func.count(FinancialPeriod.id))) == report.accepted
        assert fixture.session.scalar(select(func.count(QuarantineRow.id))) == report.quarantined
        batch = fixture.session.get(ImportBatch, report.batch_id)
        assert batch is not None
        assert batch.accepted_count == report.accepted
        assert batch.quarantined_count == report.quarantined
        assert batch.row_count == report.received
    finally:
        fixture.close()


def test_xlsx_reader_loads_the_same_shape_as_csv() -> None:
    fixture = _Fixture()
    try:
        report = fixture.import_fixture(
            "clean_extract.xlsx", source_type="xlsx", mapping_name="bank_extract_xlsx"
        )
        fixture.session.commit()

        assert report.accepted == 2
        assert report.reconciled is True
    finally:
        fixture.close()


def test_json_reader_loads_the_same_shape_as_csv() -> None:
    fixture = _Fixture()
    try:
        report = fixture.import_fixture(
            "clean_extract.json", source_type="json", mapping_name="bank_extract_json"
        )
        fixture.session.commit()

        assert report.accepted == 2
        assert report.reconciled is True
    finally:
        fixture.close()


def test_unknown_mapping_name_is_refused_before_write() -> None:
    fixture = _Fixture()
    try:
        with pytest.raises(ValidationError):
            fixture.import_fixture(
                "clean_extract.csv", source_type="csv", mapping_name="does-not-exist"
            )
        fixture.session.rollback()
        assert fixture.session.scalar(select(func.count(ImportBatch.id))) == 0
    finally:
        fixture.close()
