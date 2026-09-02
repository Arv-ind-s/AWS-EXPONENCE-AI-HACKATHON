"""Integration coverage for the bank-owned intervention catalogue (T-098)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.forecast import Intervention
from covenant_radar.db.seed import SeedLoader
from covenant_radar.domain.interventions.catalogue import CatalogueEntry
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.catalogue import CatalogueService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        _subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, dict(payload)))
        return object()


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalogue.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def actors() -> tuple[Principal, Principal]:
    return (
        Principal.user(new_id(), (Permission.PROPOSE_THRESHOLDS,)),
        Principal.user(new_id(), (Permission.APPROVE_THRESHOLDS,)),
    )


@pytest.fixture
def service(db_session: Session) -> tuple[CatalogueService, _Audit]:
    audit = _Audit()
    return (
        CatalogueService(
            db_session,
            audit=audit,
            clock=FixedClock(_NOW),
            request_id="rq-t098-catalogue",
        ),
        audit,
    )


def _entry(
    code: str = "CREDIT-REDUCE",
    *,
    role_tag: str = "credit",
    text: str = "Review and reduce funded exposure.",
    classes: tuple[str, ...] = ("leverage",),
) -> CatalogueEntry:
    return CatalogueEntry(
        id=code,
        role_tag=role_tag,
        text=text,
        effect_model="level_shift",
        effect_parameters={"amount": "-0.10"},
        assumptions=("The approved exposure reduction is available immediately.",),
        applicable_covenant_classes=classes,
        requires_approval=True,
    )


def _approve(
    service: CatalogueService,
    maker: Principal,
    checker: Principal,
    entry: CatalogueEntry,
) -> CatalogueEntry:
    proposal = service.save(maker, entry)
    assert proposal.pending
    assert proposal.approval_request is not None
    service.decide_approval(checker, proposal.approval_request.id, approved=True, reason="Reviewed")
    return service.get(entry.id)


def test_entry_without_effect_model_refused(service: tuple[CatalogueService, _Audit]) -> None:
    catalogue, _audit = service
    with pytest.raises(ValidationError, match="effect model"):
        catalogue.save(
            Principal.user(new_id(), (Permission.PROPOSE_THRESHOLDS,)),
            {
                "id": "MISSING-EFFECT",
                "role_tag": "credit",
                "text": "An action without a deterministic simulation.",
                "effect_parameters": {"amount": "-1"},
                "assumptions": ("The action is approved.",),
                "applicable_covenant_classes": ("leverage",),
            },
        )


def test_retired_entry_resolves_historically_and_is_excluded_from_new(
    service: tuple[CatalogueService, _Audit],
    actors: tuple[Principal, Principal],
) -> None:
    catalogue, _audit = service
    maker, checker = actors
    current = _approve(catalogue, maker, checker, _entry())

    retirement = catalogue.retire(maker, current.id)
    assert retirement.pending
    assert catalogue.get(current.id).is_active
    assert retirement.approval_request is not None
    catalogue.decide_approval(
        checker, retirement.approval_request.id, approved=True, reason="Retired"
    )

    historical = catalogue.get(current.id)
    assert historical.is_retired
    assert historical.retired_at == _NOW
    assert catalogue.for_recommendation("leverage") == ()
    assert historical.for_simulation("leverage").code == current.id


def test_empty_applicability_refused() -> None:
    with pytest.raises(ValidationError, match="at least one covenant class"):
        _entry(classes=())


def test_invalid_role_tag_refused() -> None:
    with pytest.raises(ValidationError, match="role_tag"):
        _entry(role_tag="collections")


def test_change_routed_through_maker_checker_and_audited(
    service: tuple[CatalogueService, _Audit],
    actors: tuple[Principal, Principal],
    db_session: Session,
) -> None:
    catalogue, audit = service
    maker, checker = actors
    current = _approve(catalogue, maker, checker, _entry())
    changed = _entry(text="Review and reduce the approved funded exposure.")

    proposal = catalogue.save(maker, changed, expected_version=1)
    assert proposal.pending
    row = db_session.scalar(select(Intervention).where(Intervention.code == changed.id))
    assert row is not None
    assert row.text == current.text
    assert proposal.approval_request is not None

    catalogue.decide_approval(
        checker, proposal.approval_request.id, approved=True, reason="Checked"
    )
    db_session.commit()
    assert catalogue.get(changed.id).text == changed.text
    assert [event[0] for event in audit.events] == [
        "maker_checker_submitted",
        "maker_checker_approved",
        "maker_checker_submitted",
        "maker_checker_approved",
    ]


def test_default_set_covers_three_roles(db_session: Session) -> None:
    SeedLoader(db_session, clock=FixedClock(_NOW), request_id="rq-t098-seed").load()
    roles = {row.role_tag for row in db_session.scalars(select(Intervention)).all()}
    assert roles == {"relationship_manager", "credit", "risk"}
