"""Integration checks for the T-026 provenance, restatement and quarantine
resolution vertical slice.

Self-contained fixture style copied from `tests/integration/test_statement_
import.py`: its own `sqlite:///:memory:` engine and `Base.metadata.
create_all`, not the PostgreSQL-only `tests/integration/conftest.py`
fixtures, so this suite runs without an external database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, Portfolio
from covenant_radar.db.models.covenant import CovenantTest
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.statements import (
    FieldProvenance,
    FinancialPeriod,
    ImportMapping,
    QuarantineRow,
    StatementLineValue,
)
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.registry import RegistryService
from covenant_radar.services.statements import StatementImportService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

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
    "totals_row": None,
}

_INITIAL_ROW = {
    "borrower_key": "B-RS-001",
    "fy_label": "FY26Q4",
    "period_type": "annual",
    "period_start": "2025-04-01",
    "period_end": "2026-03-31",
    "is_audited": "true",
    "revenue_lakh": "10000",
    "total_assets_lakh": "50000",
    "tangible_net_worth_lakh": "20000",
}
_RESTATED_ROW = {**_INITIAL_ROW, "revenue_lakh": "10500"}
_BAD_ROW = {**_INITIAL_ROW, "revenue_lakh": "not-a-number"}


def _csv(rows: list[dict[str, str]]) -> bytes:
    columns = list(rows[0].keys())
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(row[column]) for column in columns))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


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
            username="statement-steward",
            email="statement-steward@example.com",
            full_name="Statement Steward",
            auth_source="local",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t026-user",
        )
        self.portfolio = Portfolio.create(
            code="RESTATE",
            name="Restatement Portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t026-portfolio",
        )
        self.borrower = Borrower(
            id=uuid4(),
            reference="B-RS-001",
            legal_name="Restatement Test Borrower",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t026-borrower",
        )
        self.facility = Facility(
            id=uuid4(),
            reference="F-RS-001",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t026-facility",
        )
        self.session.add_all([self.user, self.portfolio, self.borrower, self.facility])
        self.session.flush()

        self.mapping = ImportMapping(
            id=uuid4(),
            name="bank_extract_csv",
            source_type="csv",
            version=1,
            spec=_MAPPING_SPEC,
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t026-mapping",
        )
        self.session.add(self.mapping)
        self.session.flush()

        self.principal = Principal.user(
            self.user.id,
            (
                Permission.INGEST_DATA,
                Permission.CORRECT_SOURCE_DATA,
                Permission.RESOLVE_QUARANTINE,
                Permission.VIEW_BORROWER,
                Permission.REGISTER_COVENANT,
                Permission.VIEW_COVENANT,
            ),
        )
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.service = StatementImportService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t026-service",
        )
        self.registry = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t026-registry",
            maker_checker_enabled=False,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def import_initial(self) -> object:
        report = self.service.import_statements(
            self.principal,
            source_type="csv",
            content=_csv([_INITIAL_ROW]),
            mapping_name="bank_extract_csv",
            scope=self.scope,
        )
        self.session.commit()
        return report

    def live_period(self) -> FinancialPeriod:
        return self.session.scalars(
            select(FinancialPeriod).where(
                FinancialPeriod.borrower_id == self.borrower.id,
                FinancialPeriod.superseded_by_id.is_(None),
            )
        ).one()

    def register_covenant(self) -> object:
        terms = CovenantVersionTerms(
            definition_ref="leverage_ratio",
            custom_formula=None,
            threshold=Decimal("2.5"),
            direction="max",
            unit="x",
            frequency="annual",
            test_basis="standalone",
            effective_from=date(2026, 1, 1),
        )
        return self.registry.register(
            self.principal,
            facility_id=self.facility.id,
            reference="CV-RS-001",
            name="Leverage covenant",
            covenant_class="financial",
            terms=terms,
            scope=self.scope,
        )


def test_restatement_creates_version_keeps_old() -> None:
    fixture = _Fixture()
    try:
        fixture.import_initial()
        old_period = fixture.live_period()
        assert old_period.version == 1
        assert old_period.superseded_by_id is None

        result = fixture.service.restate_period(
            fixture.principal,
            source_type="csv",
            content=_csv([_RESTATED_ROW]),
            mapping_name="bank_extract_csv",
            reason="Auditor identified a revenue understatement.",
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert result.previous_version == 1
        assert result.new_version == 2
        assert result.has_dependent_tests is False
        assert result.flagged_tests == ()

        fixture.session.refresh(old_period)
        assert old_period.superseded_by_id == result.new_period_id
        assert old_period.version == 1

        new_period = fixture.session.get(FinancialPeriod, result.new_period_id)
        assert new_period is not None
        assert new_period.version == 2
        assert new_period.superseded_by_id is None
        assert new_period.fy_label == old_period.fy_label

        old_lines = {
            row.line_code: row.value
            for row in fixture.session.scalars(
                select(StatementLineValue).where(StatementLineValue.period_id == old_period.id)
            )
        }
        new_lines = {
            row.line_code: row.value
            for row in fixture.session.scalars(
                select(StatementLineValue).where(StatementLineValue.period_id == new_period.id)
            )
        }
        assert old_lines["revenue"] == Decimal("100.00000000")
        assert new_lines["revenue"] == Decimal("105.00000000")

        assert fixture.session.scalar(select(func.count(FinancialPeriod.id))) == 2
    finally:
        fixture.close()


def test_dependent_tests_flagged_for_recomputation() -> None:
    fixture = _Fixture()
    try:
        fixture.import_initial()
        old_period = fixture.live_period()

        registered = fixture.register_covenant()
        test = CovenantTest(
            id=new_id(),
            covenant_version_id=registered.version.id,
            period_id=old_period.id,
            as_of_date=date(2026, 3, 31),
            value=Decimal("2.1"),
            threshold_used=Decimal("2.5"),
            headroom_pct=Decimal("16"),
            verdict="pass",
            computed_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t026-test",
        )
        fixture.session.add(test)
        fixture.session.flush()

        result = fixture.service.restate_period(
            fixture.principal,
            source_type="csv",
            content=_csv([_RESTATED_ROW]),
            mapping_name="bank_extract_csv",
            reason="Correcting FY26Q4 revenue after audit.",
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert result.has_dependent_tests is True
        assert len(result.flagged_tests) == 1
        flagged = result.flagged_tests[0]
        assert flagged.covenant_test_id == test.id
        assert flagged.covenant_version_id == registered.version.id
        assert flagged.verdict == "pass"

        fixture.session.refresh(test)
        assert test.verdict == "pass"
        assert test.value == Decimal("2.1")
        assert test.computed_at == _NOW
    finally:
        fixture.close()


def test_corrected_quarantine_row_carries_both_provenances() -> None:
    fixture = _Fixture()
    try:
        report = fixture.service.import_statements(
            fixture.principal,
            source_type="csv",
            content=_csv([_BAD_ROW]),
            mapping_name="bank_extract_csv",
            scope=fixture.scope,
        )
        fixture.session.commit()
        assert report.quarantined == 1
        quarantine_row = fixture.session.scalars(select(QuarantineRow)).one()
        assert quarantine_row.rule_failed == "line_normalisation_failed"

        new_period = fixture.service.correct_quarantine_row(
            fixture.principal,
            quarantine_row.id,
            corrected_raw=dict(_INITIAL_ROW),
            reason="Bank re-sent the row with a numeric revenue figure.",
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert new_period.borrower_id == fixture.borrower.id
        assert new_period.version == 1

        lines = {
            row.line_code: row
            for row in fixture.session.scalars(
                select(StatementLineValue).where(StatementLineValue.period_id == new_period.id)
            )
        }
        assert lines["revenue"].value == Decimal("100.00000000")
        provenance = fixture.session.get(FieldProvenance, lines["revenue"].provenance_id)
        assert provenance is not None
        assert provenance.transform_note is not None
        assert str(quarantine_row.id) in provenance.transform_note
        assert "reason=" in provenance.transform_note
        assert quarantine_row.rule_failed in provenance.transform_note

        fixture.session.refresh(quarantine_row)
        assert quarantine_row.resolved_at is not None
        assert quarantine_row.resolved_by_id == fixture.user.id
        assert quarantine_row.resolution is not None
        assert quarantine_row.resolution.startswith("corrected:")
    finally:
        fixture.close()


def test_rejected_row_retained_with_reason() -> None:
    fixture = _Fixture()
    try:
        fixture.service.import_statements(
            fixture.principal,
            source_type="csv",
            content=_csv([_BAD_ROW]),
            mapping_name="bank_extract_csv",
            scope=fixture.scope,
        )
        fixture.session.commit()
        quarantine_row = fixture.session.scalars(select(QuarantineRow)).one()

        returned = fixture.service.reject_quarantine_row(
            fixture.principal,
            quarantine_row.id,
            reason="Confirmed with the bank this row is not applicable.",
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert returned.id == quarantine_row.id
        assert fixture.session.scalar(select(func.count(QuarantineRow.id))) == 1
        fixture.session.refresh(quarantine_row)
        assert quarantine_row.resolved_at is not None
        assert quarantine_row.resolved_by_id == fixture.user.id
        assert (
            quarantine_row.resolution
            == "rejected: Confirmed with the bank this row is not applicable."
        )
        assert fixture.session.scalar(select(func.count(FinancialPeriod.id))) == 0
    finally:
        fixture.close()


def test_any_value_resolves_to_source_row_and_mapping() -> None:
    fixture = _Fixture()
    try:
        fixture.import_initial()
        period = fixture.live_period()
        line = fixture.session.scalars(
            select(StatementLineValue).where(
                StatementLineValue.period_id == period.id,
                StatementLineValue.line_code == "revenue",
            )
        ).one()

        trace = fixture.service.trace_line_value(fixture.principal, line.id, scope=fixture.scope)

        assert trace.source_type == "csv"
        assert trace.mapping_name == "bank_extract_csv"
        assert trace.mapping_version == 1
        assert trace.row_reference == "row_1"
        assert trace.batch_id == period.source_batch_id
    finally:
        fixture.close()


def test_restatement_audited() -> None:
    fixture = _Fixture()
    try:
        fixture.import_initial()
        old_period = fixture.live_period()

        result = fixture.service.restate_period(
            fixture.principal,
            source_type="csv",
            content=_csv([_RESTATED_ROW]),
            mapping_name="bank_extract_csv",
            reason="Auditor identified a revenue understatement.",
            scope=fixture.scope,
        )
        fixture.session.commit()

        matches = [
            payload
            for event_type, _subject, payload in fixture.audit.events
            if event_type == AuditEventType.STATEMENT_PERIOD_RESTATED.value
        ]
        assert len(matches) == 1
        payload = matches[0]
        assert payload["previous_period_id"] == str(old_period.id)
        assert payload["new_period_id"] == str(result.new_period_id)
        assert payload["reason"] == "Auditor identified a revenue understatement."
        assert payload["flagged_covenant_test_ids"] == []
    finally:
        fixture.close()
