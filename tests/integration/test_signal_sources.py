"""Integration checks for the T-043 signal source seam."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, Facility, Portfolio, SignalEvent
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.signals import SignalEvent as DomainSignalEvent
from covenant_radar.ingestion.signals.api_source import ApiSignalSource
from covenant_radar.ingestion.signals.file_source import FileSignalSource
from covenant_radar.ingestion.signals.framework import SignalIngestionFramework
from covenant_radar.ingestion.signals.sources import (
    SignalSourceConfigurationError,
    SignalSourceError,
    SignalSourceRegistry,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.ingestion import SignalIngestionService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_CSV_MAPPING = {
    "borrower_id": "borrower",
    "facility_id": "facility",
    "event_date": "observed_on",
    "family": "family",
    "event_type": "event_type",
    "magnitude": "magnitude",
    "unit": "unit",
    "payload": {"days_past_due": "days_past_due", "is_adverse": "is_adverse"},
}


class _Audit:
    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del event_type, subject, payload, actor, request_id
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = AppUser(
            id=uuid4(),
            username="signal-source",
            email="signal-source@example.com",
            full_name="Signal Source",
            auth_source="local",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-source-test-0001",
        )
        self.portfolio = Portfolio.create(
            code="SOURCES",
            name="Signal Source Portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-source-test-0002",
        )
        self.borrower = Borrower(
            id=uuid4(),
            reference="B-SOURCE-001",
            legal_name="Source Test Borrower",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-source-test-0003",
        )
        self.facility = Facility(
            id=uuid4(),
            reference="F-SOURCE-001",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("100"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-source-test-0004",
        )
        self.session.add_all([self.user, self.portfolio, self.borrower, self.facility])
        self.session.flush()
        self.principal = Principal.user(self.user.id, (Permission.INGEST_DATA,))
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.service = SignalIngestionService(
            self.session,
            audit=_Audit(),
            clock=FixedClock(_NOW),
            request_id="rq-source-test-0005",
        )

    def event(self) -> dict[str, object]:
        return {
            "borrower_id": self.borrower.id,
            "facility_id": self.facility.id,
            "event_date": date(2026, 1, 1),
            "family": "payment",
            "event_type": "payment_delay",
            "magnitude": Decimal("3"),
            "unit": "days",
            "payload": {"days_past_due": 3, "is_adverse": True},
        }

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def _file_source(
    event: dict[str, object], *, source_reference: str, source_id: UUID
) -> FileSignalSource:
    csv_content = (
        "borrower,facility,observed_on,family,event_type,magnitude,unit,days_past_due,is_adverse\n"
        f"{event['borrower_id']},{event['facility_id']},{event['event_date']},"
        f"{event['family']},{event['event_type']},{event['magnitude']},{event['unit']},"
        f"{event['payload']['days_past_due']},{str(event['payload']['is_adverse']).lower()}\n"
    )
    return FileSignalSource(
        csv_content,
        _CSV_MAPPING,
        source_reference=source_reference,
        source_id=source_id,
        file_format="csv",
    )


def test_file_source_yields_validated_events() -> None:
    borrower_id = uuid4()
    facility_id = uuid4()
    source_id = uuid4()
    content = (
        "borrower,facility,observed_on,family,event_type,magnitude,unit,days_past_due,is_adverse\n"
        f"{borrower_id},{facility_id},2026-01-01,payment,payment_delay,3,days,3,true\n"
        f"{borrower_id},{facility_id},2026-01-02,unknown,unknown,3,days,3,true\n"
    )

    source = FileSignalSource(
        content,
        _CSV_MAPPING,
        source_reference="bank-file-1",
        source_id=source_id,
        file_format="csv",
    )

    events = tuple(source.iter_events())

    assert len(events) == 2
    assert isinstance(events[0], DomainSignalEvent)
    assert events[0].borrower_id == borrower_id
    assert events[0].facility_id == facility_id
    assert events[0].source_id == source_id
    assert events[0].payload == {"days_past_due": 3, "is_adverse": True}
    prepared = SignalIngestionFramework().prepare(events)
    assert len(prepared.prepared) == 1
    assert len(prepared.quarantined) == 1


def test_api_source_matches_file_source_shape() -> None:
    source_id = uuid4()
    event = {
        "borrower_id": uuid4(),
        "facility_id": uuid4(),
        "event_date": date(2026, 1, 1),
        "family": "payment",
        "event_type": "payment_delay",
        "magnitude": Decimal("3"),
        "unit": "days",
        "payload": {"days_past_due": 3, "is_adverse": True},
    }
    file_row = {
        "borrower": str(event["borrower_id"]),
        "facility": str(event["facility_id"]),
        "observed_on": "2026-01-01",
        "family": "payment",
        "event_type": "payment_delay",
        "magnitude": 3,
        "unit": "days",
        "days_past_due": 3,
        "is_adverse": True,
    }
    file_event = tuple(
        FileSignalSource(
            json.dumps([file_row]),
            _CSV_MAPPING,
            source_reference="bank-file-2",
            source_id=source_id,
            file_format="json",
        ).iter_events()
    )[0]
    api_event = tuple(
        ApiSignalSource(
            {"events": [event]},
            source_reference="api-2",
            source_id=source_id,
        ).iter_events()
    )[0]

    assert api_event == file_event
    assert api_event.hash == file_event.hash
    assert api_event.source_id == source_id


def test_source_error_commits_nothing() -> None:
    fixture = _Fixture()
    source_id = uuid4()
    source_identifier = source_id

    class FailingSource:
        source_reference = "mid-stream-bank-feed"
        source_id = source_identifier

        def iter_events(self) -> Iterator[dict[str, object]]:
            yield fixture.event()
            raise RuntimeError("connection closed")

    try:
        registry = SignalSourceRegistry([FailingSource()])
        with pytest.raises(SignalSourceError, match="mid-stream-bank-feed"):
            registry.ingest(fixture.service.ingest, fixture.principal, scope=fixture.scope)
        assert fixture.session.scalar(select(func.count(SignalEvent.id))) == 0
    finally:
        fixture.close()


def test_two_sources_same_event_deduplicated() -> None:
    fixture = _Fixture()
    try:
        event = fixture.event()
        first = _file_source(event, source_reference="bank-file-3", source_id=uuid4())
        second = ApiSignalSource({"events": [event]}, source_reference="api-3", source_id=uuid4())
        report = SignalSourceRegistry([first, second]).ingest(
            fixture.service.ingest,
            fixture.principal,
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert (report.inserted, report.duplicates, report.rejected) == (1, 1, 0)
        assert fixture.session.scalar(select(func.count(SignalEvent.id))) == 1
    finally:
        fixture.close()


def test_unmapped_source_refused() -> None:
    with pytest.raises(SignalSourceConfigurationError, match="mapping is required"):
        FileSignalSource(path="source-that-must-not-be-opened.csv")
