"""Collect one borrower's persisted facts into stage-7 ``MemoRecords`` (`C-08`).

This module is the missing half of the memo path.  ``MemoAssemblyService``
turns ``MemoRecords`` into the fixed slot map, and ``MemoGenerationService``
drafts and persists from there, but nothing until now read the database to
produce those records — every existing caller built them by hand in a test or
from an evaluation example file.

The rule this module inherits from ``services/memo.py`` is the important one:
**nothing here is calculated.**  Every value is copied from a persisted row,
and every record carries the reference of the row it came from, so the slot
map's provenance stays true all the way back to the database.  Where a fact
was never persisted the record is simply omitted, and memo assembly renders an
explicit absent slot — an absent fact must never arrive as a zero, a guess or
a collection length.

Which row supplies which slot:

===================== ============================================
slot group            source
===================== ============================================
``situation``         ``TriageEntry.what_changed`` for the latest
                      complete run — the persisted statement of
                      what moved this borrower into view.
``covenant_position`` the worst ``Forecast`` (probability,
                      confidence, crossing date, suppression) with
                      the latest ``CovenantTest`` for the same
                      covenant version (value, threshold, headroom)
                      and the ``Covenant`` name.
``drivers``           ``ForecastDriver`` rows for that forecast.
``evidence``          ``EvidenceItem`` rows for the borrower that
                      carry a persisted ``event_count_window``.
``simulations``       ``Simulation`` rows against that forecast,
                      narrowed to ``simulation_ids`` when the
                      caller names them (`C-08`).
``recommendations``   active ``Intervention`` rows applicable to
                      the covenant's class.
===================== ============================================

"Worst" follows the same definition the case-file screen uses: the triage
entry's ``worst_covenant_version_id`` at its ``worst_horizon``.  A memo and
the screen that offered it therefore describe the same covenant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastDriver,
    ForecastRun,
    Intervention,
    Simulation,
    TriageEntry,
)
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.db.models.workflow import Case
from covenant_radar.db.repositories.forecast import ForecastRepository
from covenant_radar.db.scoping import Scope, ownership_path_for
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.memo.slots import MemoRecord, MemoRecords, RecordReference

#: The reason attached to a suppressed probability slot. `Forecast` records
#: the suppression as a boolean, so the human-readable limiting factor is
#: named once, here, rather than invented at each call site.
CONFIDENCE_FLOOR_REASON: Final[str] = (
    "the forecast confidence is below the floor required to publish a probability"
)

_MAX_EVIDENCE_RECORDS: Final[int] = 25
_MAX_DRIVER_RECORDS: Final[int] = 10
_MAX_RECOMMENDATION_RECORDS: Final[int] = 10


@dataclass(frozen=True, slots=True)
class BorrowerMemoFacts:
    """Everything ``MemoGenerationService.generate`` needs for one borrower."""

    records: MemoRecords
    forecast: Forecast | None = None
    run_id: UUID | None = None
    case_id: UUID | None = None

    @property
    def has_forecast(self) -> bool:
        """Whether a forecast was found to ground the covenant position.

        Without one there is no covenant position, no drivers and no
        simulations, which is why the case-file screen only offers the memo
        action when a forecast exists.
        """

        return self.forecast is not None


def collect_memo_records(
    session: Session,
    borrower: Borrower,
    *,
    scope: Scope,
    simulation_ids: Sequence[UUID] | None = None,
) -> BorrowerMemoFacts:
    """Read one borrower's memo-grounding facts within ``scope``.

    Every statement is scoped, so a caller cannot ground a memo in a row it
    is not entitled to read even if it supplies the identifier directly.
    """

    if not is_database_session(session):
        raise TypeError("collect_memo_records requires a SQLAlchemy Session.")
    if not isinstance(borrower, Borrower):
        raise TypeError("collect_memo_records requires a Borrower record.")
    if not isinstance(scope, Scope):
        raise TypeError("collect_memo_records requires a portfolio Scope.")
    requested_simulations = _simulation_ids(simulation_ids)

    run = _latest_complete_run(session, borrower.id, scope)
    triage = _latest_triage_entry(session, borrower.id, run, scope)
    forecast = _worst_forecast(session, triage, run, scope)

    covenant_position: MemoRecord | None = None
    drivers: tuple[MemoRecord, ...] = ()
    simulations: tuple[MemoRecord, ...] = ()
    recommendations: tuple[MemoRecord, ...] = ()
    if forecast is not None:
        covenant, test = _covenant_context(session, forecast, scope)
        covenant_position = _covenant_position_record(forecast, covenant, test)
        drivers = _driver_records(session, forecast, scope)
        simulations = _simulation_records(session, forecast, scope, requested_simulations)
        recommendations = _recommendation_records(session, covenant)

    return BorrowerMemoFacts(
        records=MemoRecords(
            situation=_situation_record(triage),
            covenant_position=covenant_position,
            drivers=drivers,
            evidence=_evidence_records(session, borrower.id, scope),
            simulations=simulations,
            recommendations=recommendations,
        ),
        forecast=forecast,
        run_id=run.id if run is not None else None,
        case_id=_open_case_id(session, borrower.id, scope),
    )


def _situation_record(triage: TriageEntry | None) -> MemoRecord | None:
    """Use the persisted "what changed" statement, or supply nothing.

    A blank ``what_changed`` is not narrated around: assembly turns the
    missing record into an absent slot that says so.
    """

    if triage is None:
        return None
    situation = triage.what_changed
    if not isinstance(situation, str) or not situation.strip():
        return None
    return MemoRecord(
        reference=RecordReference("triage_entry", triage.id),
        values={
            "situation": situation.strip(),
            "band": triage.band,
            "urgency": triage.urgency,
        },
    )


def _covenant_position_record(
    forecast: Forecast,
    covenant: Covenant | None,
    test: CovenantTest | None,
) -> MemoRecord:
    """Combine the forecast with its covenant's latest completed test.

    The reference names the forecast: it is the record that made this
    covenant the memo's subject, and the one an auditor reconstructs from.
    Every key assembly reads is present, carrying ``None`` where the fact was
    not persisted, so a missing value becomes a stated absence rather than a
    hard failure.
    """

    values: dict[str, Any] = {
        "ratio_name": covenant.name if covenant is not None else None,
        "value": test.value if test is not None else None,
        "threshold": test.threshold_used if test is not None else None,
        "headroom": test.headroom_pct if test is not None else None,
        "confidence": forecast.confidence,
        "crossing_date": forecast.projected_cross_date,
    }
    if forecast.below_confidence_floor:
        # Assembly refuses a suppressed record that still carries a
        # probability, which is the point: a suppressed figure must not be
        # recoverable from the memo's own slot map.
        values["probability"] = None
        values["probability_suppressed"] = True
        values["probability_suppression_reason"] = CONFIDENCE_FLOOR_REASON
    else:
        values["probability"] = forecast.probability
        values["probability_suppressed"] = False
    return MemoRecord(reference=RecordReference("forecast", forecast.id), values=values)


def _driver_records(session: Session, forecast: Forecast, scope: Scope) -> tuple[MemoRecord, ...]:
    statement = (
        _scoped_select(ForecastDriver, scope)
        .where(ForecastDriver.forecast_id == forecast.id)
        .order_by(ForecastDriver.share.desc(), ForecastDriver.name, ForecastDriver.id)
        .limit(_MAX_DRIVER_RECORDS)
    )
    return tuple(
        MemoRecord(
            reference=RecordReference("forecast_driver", driver.id),
            values={"name": driver.name, "share": driver.share},
        )
        for driver in session.execute(statement).scalars().all()
        if isinstance(driver.name, str) and driver.name.strip()
    )


def _evidence_records(session: Session, borrower_id: UUID, scope: Scope) -> tuple[MemoRecord, ...]:
    """Take the persisted event count only — never the length of a list.

    ``EvidenceItem.event_count_window`` is nullable, and a row without one
    genuinely has no recorded count.  Such a row is skipped rather than
    counted as one occurrence or as zero.
    """

    statement = (
        _scoped_select(EvidenceItem, scope)
        .where(EvidenceItem.borrower_id == borrower_id)
        .order_by(EvidenceItem.last_seen.desc(), EvidenceItem.family, EvidenceItem.id)
        .limit(_MAX_EVIDENCE_RECORDS)
    )
    records: list[MemoRecord] = []
    for item in session.execute(statement).scalars().all():
        if item.event_count_window is None:
            continue
        records.append(
            MemoRecord(
                reference=RecordReference("evidence_item", item.id),
                values={
                    "citation": f"{item.family}: {item.evidence_type}",
                    "count": item.event_count_window,
                    "state": item.state,
                    "counts_toward_pressure": item.counts_toward_pressure,
                },
            )
        )
    return tuple(records)


def _simulation_records(
    session: Session,
    forecast: Forecast,
    scope: Scope,
    requested: tuple[UUID, ...],
) -> tuple[MemoRecord, ...]:
    """Read simulations against this forecast, narrowed to those requested.

    A simulation whose assumptions were never persisted is skipped: an
    intervention's projected effect is not reportable without the assumptions
    it rests on, and assembly refuses such a record outright.
    """

    statement = (
        _scoped_select(Simulation, scope)
        .where(Simulation.forecast_id == forecast.id)
        .order_by(Simulation.created_at.desc(), Simulation.id)
    )
    if requested:
        statement = statement.where(Simulation.id.in_(requested))

    rows = session.execute(statement).scalars().all()
    intervention_ids = {row.intervention_id for row in rows}
    interventions = _interventions_by_id(session, intervention_ids)

    records: list[MemoRecord] = []
    for row in rows:
        assumptions = row.assumptions
        if not assumptions:
            continue
        intervention = interventions.get(row.intervention_id)
        if intervention is None:
            continue
        records.append(
            MemoRecord(
                reference=RecordReference("simulation", row.id),
                values={
                    "code": intervention.code,
                    "text": intervention.text,
                    "projected_cross_date": row.projected_cross_date,
                    "probability": row.probability,
                    "delta_days": row.delta_days,
                    "delta_probability": row.delta_probability,
                    "assumptions": assumptions,
                },
            )
        )
    return tuple(records)


def _recommendation_records(session: Session, covenant: Covenant | None) -> tuple[MemoRecord, ...]:
    """Offer the active catalogue entries applicable to this covenant class.

    The catalogue is reference data rather than borrower data, so it carries
    no portfolio ownership path and is read unscoped. An entry without a role
    tag is skipped: assembly requires one, because a recommended action that
    names no owning role cannot be acted on.
    """

    statement = select(Intervention).where(Intervention.is_active.is_(True))
    statement = statement.order_by(Intervention.code, Intervention.id)
    records: list[MemoRecord] = []
    for intervention in session.execute(statement).scalars().all():
        if not _applies_to(intervention, covenant):
            continue
        role_tag = intervention.role_tag
        if not isinstance(role_tag, str) or not role_tag.strip():
            continue
        records.append(
            MemoRecord(
                reference=RecordReference("intervention", intervention.id),
                values={
                    "code": intervention.code,
                    "role_tag": role_tag,
                    "text": intervention.text,
                    "requires_approval": intervention.requires_approval,
                },
            )
        )
        if len(records) == _MAX_RECOMMENDATION_RECORDS:
            break
    return tuple(records)


def _applies_to(intervention: Intervention, covenant: Covenant | None) -> bool:
    """An empty class list means the entry applies to every covenant class."""

    classes = intervention.applicable_covenant_classes
    if not classes:
        return True
    if covenant is None:
        return False
    return covenant.covenant_class in set(classes)


def _covenant_context(
    session: Session, forecast: Forecast, scope: Scope
) -> tuple[Covenant | None, CovenantTest | None]:
    covenant = (
        session.execute(
            _scoped_select(Covenant, scope)
            .join(CovenantVersion, CovenantVersion.covenant_id == Covenant.id)
            .where(CovenantVersion.id == forecast.covenant_version_id)
        )
        .scalars()
        .one_or_none()
    )
    test = (
        session.execute(
            _scoped_select(CovenantTest, scope)
            .where(CovenantTest.covenant_version_id == forecast.covenant_version_id)
            .order_by(
                CovenantTest.as_of_date.desc(),
                CovenantTest.computed_at.desc(),
                CovenantTest.id.desc(),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    return covenant, test


def _interventions_by_id(session: Session, intervention_ids: set[UUID]) -> dict[UUID, Intervention]:
    if not intervention_ids:
        return {}
    statement = select(Intervention).where(Intervention.id.in_(intervention_ids))
    return {row.id: row for row in session.execute(statement).scalars().all()}


def _latest_complete_run(
    session: Session,
    borrower_id: UUID,
    scope: Scope,
) -> ForecastRun | None:
    return ForecastRepository(session).latest_complete_run_for_borrower(
        borrower_id,
        scope=scope,
    )


def _latest_triage_entry(
    session: Session,
    borrower_id: UUID,
    run: ForecastRun | None,
    scope: Scope,
) -> TriageEntry | None:
    if run is None:
        return None
    statement = (
        _scoped_select(TriageEntry, scope)
        .where(TriageEntry.run_id == run.id, TriageEntry.borrower_id == borrower_id)
        .order_by(TriageEntry.rank, TriageEntry.id)
        .limit(1)
    )
    return session.execute(statement).scalars().first()


def _worst_forecast(
    session: Session,
    triage: TriageEntry | None,
    run: ForecastRun | None,
    scope: Scope,
) -> Forecast | None:
    if triage is None or run is None:
        return None
    if triage.worst_covenant_version_id is None or triage.worst_horizon is None:
        return None
    statement = _scoped_select(Forecast, scope).where(
        Forecast.run_id == run.id,
        Forecast.covenant_version_id == triage.worst_covenant_version_id,
        Forecast.horizon_days == triage.worst_horizon,
    )
    return session.execute(statement).scalars().first()


def _open_case_id(session: Session, borrower_id: UUID, scope: Scope) -> UUID | None:
    """Link the memo to the borrower's live case when one is open."""

    statement = (
        _scoped_select(Case, scope)
        .where(Case.borrower_id == borrower_id, Case.closed_at.is_(None))
        .order_by(Case.created_at.desc(), Case.id.desc())
        .limit(1)
    )
    case = session.execute(statement).scalars().first()
    return case.id if case is not None else None


def _simulation_ids(values: Sequence[UUID] | None) -> tuple[UUID, ...]:
    if values is None:
        return ()
    identifiers = tuple(values)
    if any(not isinstance(value, UUID) for value in identifiers):
        raise TypeError("simulation_ids must contain UUID values.")
    return tuple(dict.fromkeys(identifiers))


def _scoped_select(model: type[Any], scope: Scope) -> Select[Any]:
    ownership = ownership_path_for(model)
    statement = ownership.apply(select(model))
    return statement.where(scope.predicate(ownership.path_column))


__all__ = [
    "CONFIDENCE_FLOOR_REASON",
    "BorrowerMemoFacts",
    "collect_memo_records",
]
