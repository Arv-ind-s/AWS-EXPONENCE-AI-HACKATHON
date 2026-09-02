"""Unit tests for the domain model tables (`T-009`): `plan.md §5.4`-`§5.9`
copied exactly, the `covenant_version` immutability trigger, the
`audit_event` hash-chain trigger, and the two other uniqueness rules the
task's `Behaviour` line names — `signal_event.content_hash` and
`forecast`'s per-run-covenant-horizon uniqueness.

Every test runs against a real in-memory SQLite database — the same
technique `tests/unit/test_model_borrower.py` (`T-008`) established — so
this file stays fast and network-free; the schema, including both engines'
triggers, is proven again against a real PostgreSQL instance once
`tests/integration` exercises these models.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent, ConfigVersion, ThresholdSnapshot, TraceRow
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantException,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
    CovenantWaiver,
    RatioDefinition,
)
from covenant_radar.db.models.document import Document, DocumentPage, DocumentSpan
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastDriver,
    ForecastPath,
    ForecastRun,
    Intervention,
    Simulation,
    TriageEntry,
)
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.operations import (
    Connector,
    ConnectorRun,
    DriftObservation,
    EntityMatch,
    EvaluationRun,
    FeedSource,
    JobRun,
    ModelCall,
    ModelRegistration,
    RetentionPurgeLog,
)
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import (
    CertificateRequest,
    EvidenceItem,
    EvidenceTransition,
    SignalEvent,
)
from covenant_radar.db.models.workflow import (
    ActionTaken,
    Case,
    CaseComment,
    CaseEvent,
    Disposition,
    Memo,
    MemoExport,
    Notification,
    NotificationPreference,
    OverrideRecord,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MODEL_TABLES = [
    Portfolio.__table__,
    AppUser.__table__,
    Borrower.__table__,
    Facility.__table__,
    Document.__table__,
    DocumentPage.__table__,
    DocumentSpan.__table__,
    Covenant.__table__,
    CovenantVersion.__table__,
    CovenantException.__table__,
    CovenantWaiver.__table__,
    CovenantTest.__table__,
    CovenantSchedule.__table__,
    RatioDefinition.__table__,
    SignalEvent.__table__,
    EvidenceItem.__table__,
    EvidenceTransition.__table__,
    CertificateRequest.__table__,
    ThresholdSnapshot.__table__,
    JobRun.__table__,
    ForecastRun.__table__,
    Forecast.__table__,
    ForecastPath.__table__,
    ForecastDriver.__table__,
    Intervention.__table__,
    Simulation.__table__,
    TriageEntry.__table__,
    Case.__table__,
    CaseEvent.__table__,
    CaseComment.__table__,
    ActionTaken.__table__,
    Memo.__table__,
    MemoExport.__table__,
    OverrideRecord.__table__,
    Disposition.__table__,
    Notification.__table__,
    NotificationPreference.__table__,
    AuditEvent.__table__,
    TraceRow.__table__,
    ConfigVersion.__table__,
    ModelCall.__table__,
    ModelRegistration.__table__,
    DriftObservation.__table__,
    Connector.__table__,
    ConnectorRun.__table__,
    FeedSource.__table__,
    EntityMatch.__table__,
    RetentionPurgeLog.__table__,
    EvaluationRun.__table__,
]

# `StandardColumns` (`db/base.py`) carried by every table, plus the
# foreign-keyed overrides `identity.UserAttributedColumns` adds on top —
# every T-009 table mixes both in, so every table's column set includes
# these six (`tests/unit/test_model_borrower.py`'s own baseline).
_STANDARD_COLUMNS = {
    "id",
    "created_at",
    "updated_at",
    "created_by_id",
    "updated_by_id",
    "request_id",
}

# `plan.md §5.4`-`§5.9`'s "Key fields" per table, copied exactly, minus any
# field a table's own docstring notes is already supplied by a mixin
# (`request_id` from `StandardColumns`, `created_by_id` from
# `UserAttributedColumns`) rather than redeclared.
_PLAN_FIELDS: dict[str, set[str]] = {
    "document": {
        "borrower_id",
        "facility_id",
        "doc_type",
        "filename",
        "content_hash",
        "byte_size",
        "mime_type",
        "storage_key",
        "uploaded_by_id",
        "scan_result",
        "page_count",
        "extraction_state",
        "ocr_applied",
        "retention_class",
        "purge_after",
    },
    "document_page": {
        "document_id",
        "page_number",
        "text",
        "ocr_confidence",
        "needs_review",
        "width",
        "height",
    },
    "document_span": {
        "document_id",
        "page_number",
        "start_offset",
        "end_offset",
        "bbox",
        "text",
        "span_type",
    },
    "covenant": {"reference", "facility_id", "name", "covenant_class", "is_active"},
    "covenant_version": {
        "covenant_id",
        "version_no",
        "definition_ref",
        "custom_formula",
        "threshold",
        "direction",
        "unit",
        "frequency",
        "test_basis",
        "effective_from",
        "effective_to",
        "warning_headroom_pct",
        "cure_days",
        "grace_days",
        "source_document_id",
        "source_span_id",
        "status",
        "tested_at_least_once",
        "registered_by_id",
        "approved_by_id",
    },
    "covenant_exception": {
        "covenant_version_id",
        "from_period",
        "to_period",
        "relaxed_threshold",
        "reason",
        "document_id",
        "approved_by_id",
    },
    "covenant_waiver": {
        "covenant_id",
        "from_date",
        "to_date",
        "scope",
        "reason",
        "document_id",
        "requested_by_id",
        "approved_by_id",
        "state",
    },
    "covenant_test": {
        "covenant_version_id",
        "period_id",
        "as_of_date",
        "value",
        "threshold_used",
        "headroom_pct",
        "verdict",
        "exception_id",
        "waiver_id",
        "cure_ends_on",
        "inputs",
        "not_computable_reason",
        "computed_at",
        "job_run_id",
    },
    "covenant_schedule": {
        "covenant_version_id",
        "due_date",
        "state",
        "test_id",
        "certificate_id",
    },
    "ratio_definition": {
        "code",
        "name",
        "formula_text",
        "required_lines",
        "unit",
        "plausible_min",
        "plausible_max",
        "direction_hint",
        "taxonomy_version",
    },
    "signal_event": {
        "borrower_id",
        "facility_id",
        "event_date",
        "family",
        "event_type",
        "magnitude",
        "unit",
        "payload",
        "source_id",
        "content_hash",
        "is_late",
        "ingested_at",
    },
    "evidence_item": {
        "borrower_id",
        "facility_id",
        "family",
        "evidence_type",
        "first_seen",
        "last_seen",
        "persistence_days",
        "event_count_window",
        "materiality_pct",
        "decay_factor",
        "state",
        "counts_toward_pressure",
        "superseded_by_id",
        "supersedes_id",
        "source_event_ids",
        "last_scored_at",
    },
    "evidence_transition": {
        "evidence_id",
        "from_state",
        "to_state",
        "occurred_on",
        "rule",
        "threshold_snapshot_id",
    },
    "certificate_request": {
        "covenant_schedule_id",
        "borrower_id",
        "due_date",
        "state",
        "requested_at",
        "received_at",
        "document_id",
        "reviewed_by_id",
        "rejection_reason",
    },
    "forecast_run": {
        "as_of_date",
        "job_run_id",
        "threshold_snapshot_id",
        "model_version",
        "started_at",
        "finished_at",
        "covenant_count",
        "state",
    },
    "forecast": {
        "run_id",
        "covenant_version_id",
        "horizon_days",
        "probability",
        "probability_source",
        "fallback_reason",
        "confidence",
        "below_confidence_floor",
        "projected_cross_date",
        "direction",
        "formula_inputs",
        "data_as_of",
        "staleness_days",
    },
    "forecast_path": {
        "run_id",
        "covenant_version_id",
        "day_offset",
        "projected_value",
        "headroom_pct",
    },
    "forecast_driver": {"forecast_id", "name", "share", "evidence_id", "is_other"},
    # `created_by_id` is `plan.md §5.7`'s own listed field for `simulation`,
    # already supplied by `UserAttributedColumns` (`forecast.py`'s
    # `Simulation` docstring), so it is not repeated here on top of
    # `_STANDARD_COLUMNS`, which already includes it.
    "simulation": {
        "forecast_id",
        "intervention_id",
        "parameters",
        "assumptions",
        "projected_cross_date",
        "probability",
        "delta_days",
        "delta_probability",
    },
    "intervention": {
        "code",
        "role_tag",
        "text",
        "effect_model",
        "effect_parameters",
        "applicable_covenant_classes",
        "requires_approval",
        "is_active",
        "retired_at",
    },
    "triage_entry": {
        "run_id",
        "borrower_id",
        "worst_covenant_version_id",
        "worst_horizon",
        "probability",
        "confidence",
        "exposure",
        "urgency",
        "band",
        "sma_band",
        "what_changed",
        "rank",
    },
    "case": {
        "reference",
        "borrower_id",
        "opened_from_run_id",
        "state",
        "band_at_open",
        "assignee_id",
        "due_at",
        "sla_hours",
        "closed_at",
        "closure_reason",
        "closure_note",
    },
    "case_event": {"case_id", "event_type", "actor_id", "payload", "occurred_at"},
    "case_comment": {"case_id", "author_id", "body", "mentions"},
    "action_taken": {
        "case_id",
        "intervention_id",
        "free_text",
        "taken_at",
        "actor_id",
        "outcome",
    },
    "memo": {
        "borrower_id",
        "run_id",
        "case_id",
        "template_version",
        "prompt_version",
        "provider",
        "model_version",
        "slots",
        "drafted_text",
        "actions",
        "simulations",
        "check_verdict",
        "generated_by_id",
    },
    "memo_export": {
        "memo_id",
        "format",
        "storage_key",
        "integrity_hash",
        "exported_at",
        "exported_by_id",
    },
    "override_record": {
        "subject_type",
        "subject_id",
        "stage",
        "shown",
        "user_action",
        "user_value",
        "reason",
        "prompt_version",
        "model_version",
        "threshold_snapshot_id",
        "actor_id",
    },
    "disposition": {
        "subject_type",
        "subject_id",
        "outcome",
        "reason_code",
        "note",
        "actor_id",
    },
    "notification": {
        "recipient_id",
        "channel",
        "template",
        "subject_type",
        "subject_id",
        "payload",
        "state",
        "scheduled_for",
        "sent_at",
        "attempts",
        "last_error",
        "dead_lettered_at",
    },
    "notification_preference": {
        "user_id",
        "template",
        "channel",
        "enabled",
        "quiet_hours_start",
        "quiet_hours_end",
        "digest_frequency",
    },
    # `request_id` is `plan.md §5.9`'s own listed field for `audit_event`
    # and `trace_row`, already supplied by `StandardColumns` and therefore
    # already in `_STANDARD_COLUMNS` — not repeated here.
    "audit_event": {
        "sequence",
        "occurred_at",
        "actor_id",
        "actor_label",
        "event_type",
        "subject_type",
        "subject_id",
        "payload",
        "threshold_snapshot_id",
        "prev_hash",
        "hash",
    },
    "trace_row": {
        "subject_type",
        "subject_id",
        "stage",
        "decider",
        "inputs",
        "outputs",
        "rule_or_prompt_version",
        "thresholds_compared",
        "confidence",
        "sources",
        "occurred_at",
    },
    "threshold_snapshot": {
        "values",
        "source",
        "effective_from",
        "proposed_by_id",
        "approved_by_id",
        "note",
    },
    "config_version": {"values_redacted", "applied_at", "applied_by_id", "checksum"},
    # `request_id` is `plan.md §5.9`'s own listed field for `model_call`,
    # already supplied by `StandardColumns` and already in
    # `_STANDARD_COLUMNS` — not repeated here.
    "model_call": {
        "stage",
        "provider",
        "model_version",
        "prompt_version",
        "tokens_in",
        "tokens_out",
        "latency_ms",
        "cost",
        "currency",
        "check_verdict",
        "retry_count",
        "refusal_reason",
        "from_cassette",
    },
    "model_registration": {
        "component",
        "provider",
        "model_id",
        "prompt_version",
        "purpose",
        "owner_id",
        "evaluation_run_id",
        "approved_by_id",
        "approved_at",
        "state",
    },
    "drift_observation": {
        "component",
        "metric",
        "window_start",
        "window_end",
        "value",
        "baseline",
        "breached",
    },
    "job_run": {
        "job_name",
        "run_id",
        "trigger",
        "started_at",
        "finished_at",
        "state",
        "attempt",
        "error",
        "metrics",
    },
    "connector": {"name", "connector_type", "config", "is_active"},
    "connector_run": {
        "connector_id",
        "started_at",
        "finished_at",
        "state",
        "record_count",
        "reconciled_total",
        "lag_seconds",
        "reject_count",
    },
    "feed_source": {"name", "feed_type", "config", "is_active"},
    "entity_match": {
        "feed_source_id",
        "candidate_subject_type",
        "candidate_subject_id",
        "external_reference",
        "confidence",
        "decision",
        "is_negative",
        "decided_by_id",
        "decided_at",
    },
    "retention_purge_log": {
        "entity",
        "criteria",
        "purged_count",
        "executed_at",
        "executed_by",
    },
    "evaluation_run": {"commit_sha", "arm", "scores", "passed", "executed_at"},
}

# Tables carrying `VersionedColumns` — the user-editable entities
# (`plan.md §5`'s convention). Every other table here is either ingested,
# append-only, or written once by a job/engine and never edited by a
# person, matching each model's own docstring.
_VERSIONED_TABLES = {
    "document",
    "covenant",
    "covenant_version",
    "covenant_exception",
    "covenant_waiver",
    "covenant_schedule",
    "ratio_definition",
    "evidence_item",
    "certificate_request",
    "intervention",
    "case",
    "case_comment",
    "memo",
    "threshold_snapshot",
    "config_version",
    "model_registration",
    "connector",
    "feed_source",
    "notification_preference",
}

_MODELS_BY_TABLE = {table.name: table for table in _MODEL_TABLES}


def _sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _now() -> datetime:
    return datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)


def _request_id(suffix: str) -> str:
    return f"rq-{suffix:0>16}"


def _seed_covenant_version(session: Session) -> CovenantVersion:
    """Build the full parent chain a `CovenantVersion` needs — portfolio,
    borrower, facility, covenant, app_user — and return a persisted,
    not-yet-tested version."""
    portfolio = Portfolio.create(
        code="PF1", name="Portfolio 1", created_at=_now(), updated_at=_now(),
        request_id=_request_id("1"),
    )
    session.add(portfolio)
    session.flush()

    borrower = Borrower(
        reference="B-000001",
        legal_name="Acme Pvt Ltd",
        portfolio_id=portfolio.id,
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("2"),
    )
    session.add(borrower)
    session.flush()

    facility = Facility(
        reference="F-000001-01",
        borrower_id=borrower.id,
        facility_type="cash_credit",
        sanctioned_limit=Decimal("5000000"),
        currency="INR",
        sanction_date=date(2024, 1, 1),
        effective_from=date(2024, 1, 1),
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("3"),
    )
    session.add(facility)
    session.flush()

    covenant = Covenant(
        reference="CV-000001",
        facility_id=facility.id,
        name="DSCR",
        covenant_class="financial",
        is_active=True,
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("4"),
    )
    session.add(covenant)
    session.flush()

    user = AppUser(
        username="maker",
        email="maker@example.com",
        full_name="Maker",
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("5"),
    )
    session.add(user)
    session.flush()

    version = CovenantVersion(
        covenant_id=covenant.id,
        version_no=1,
        threshold=Decimal("1.25"),
        direction="min",
        unit="ratio",
        frequency="quarterly",
        test_basis="standalone",
        effective_from=date(2024, 1, 1),
        status="live",
        tested_at_least_once=False,
        registered_by_id=user.id,
        created_at=_now(),
        updated_at=_now(),
        request_id=_request_id("6"),
    )
    session.add(version)
    session.commit()
    return version


def test_all_tables_and_columns_match_plan() -> None:
    for table_name, plan_fields in _PLAN_FIELDS.items():
        table = _MODELS_BY_TABLE[table_name]
        expected = set(_STANDARD_COLUMNS) | plan_fields
        if table_name in _VERSIONED_TABLES:
            expected.add("version")

        actual = {column.name for column in table.columns}
        assert actual == expected, (
            f"{table_name}: expected {sorted(expected)}, got {sorted(actual)}"
        )


def test_tested_covenant_version_update_refused() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        version = _seed_covenant_version(session)

        # Not yet tested: any column is freely editable.
        version.threshold = Decimal("1.30")
        session.commit()

        version.tested_at_least_once = True
        session.commit()

        # Now frozen: a protected column can no longer change.
        version.threshold = Decimal("1.50")
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()


def test_status_and_effective_to_still_updatable() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        version = _seed_covenant_version(session)
        version.tested_at_least_once = True
        session.commit()

        version.status = "superseded"
        version.effective_to = date(2025, 6, 30)
        session.commit()

        session.expire_all()
        reloaded = session.get(CovenantVersion, version.id)
        assert reloaded is not None
        assert reloaded.status == "superseded"
        assert reloaded.effective_to == date(2025, 6, 30)
        assert reloaded.threshold == Decimal("1.25000000")


def test_signal_content_hash_unique() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        portfolio = Portfolio.create(
            code="PF1", name="Portfolio 1", created_at=_now(), updated_at=_now(),
            request_id=_request_id("1"),
        )
        session.add(portfolio)
        session.flush()

        borrower = Borrower(
            reference="B-000001",
            legal_name="Acme Pvt Ltd",
            portfolio_id=portfolio.id,
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("2"),
        )
        session.add(borrower)
        session.flush()

        session.add(
            SignalEvent(
                borrower_id=borrower.id,
                event_date=date(2026, 1, 1),
                family="payment",
                event_type="dpd_breach",
                payload={"days_past_due": 45},
                content_hash="hash-shared",
                ingested_at=_now(),
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("3"),
            )
        )
        session.commit()

        session.add(
            SignalEvent(
                borrower_id=borrower.id,
                event_date=date(2026, 1, 2),
                family="payment",
                event_type="dpd_breach",
                payload={"days_past_due": 46},
                content_hash="hash-shared",
                ingested_at=_now(),
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("4"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_forecast_unique_per_run_covenant_horizon() -> None:
    engine = _sqlite_engine()
    with Session(engine) as session:
        version = _seed_covenant_version(session)

        run = ForecastRun(
            as_of_date=date(2026, 1, 15),
            started_at=_now(),
            state="complete",
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("7"),
        )
        session.add(run)
        session.flush()

        session.add(
            Forecast(
                run_id=run.id,
                covenant_version_id=version.id,
                horizon_days=90,
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("8"),
            )
        )
        session.commit()

        session.add(
            Forecast(
                run_id=run.id,
                covenant_version_id=version.id,
                horizon_days=90,
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("9"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_audit_chain_rejects_wrong_prev_hash() -> None:
    engine = _sqlite_engine()
    subject_id = new_id()
    with Session(engine) as session:
        first = AuditEvent(
            sequence=1,
            occurred_at=_now(),
            event_type="covenant.version.registered",
            subject_type="covenant_version",
            subject_id=subject_id,
            payload={"foo": "bar"},
            prev_hash=None,
            hash="hash-1",
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("2"),
        )
        session.add(first)
        session.commit()

        # Wrong prev_hash: refused, so the chain cannot be started wrong.
        wrong = AuditEvent(
            sequence=2,
            occurred_at=_now(),
            event_type="covenant.version.approved",
            subject_type="covenant_version",
            subject_id=first.subject_id,
            payload={"foo": "baz"},
            prev_hash="not-the-real-previous-hash",
            hash="hash-2",
            created_at=_now(),
            updated_at=_now(),
            request_id=_request_id("3"),
        )
        session.add(wrong)
        with pytest.raises(IntegrityError, match="prev_hash"):
            session.commit()
        session.rollback()

        # The correctly chained row is accepted.
        session.add(
            AuditEvent(
                sequence=2,
                occurred_at=_now(),
                event_type="covenant.version.approved",
                subject_type="covenant_version",
                subject_id=first.subject_id,
                payload={"foo": "baz"},
                prev_hash="hash-1",
                hash="hash-2",
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("4"),
            )
        )
        session.commit()

        # A non-monotonic sequence is refused even with the right prev_hash.
        session.add(
            AuditEvent(
                sequence=2,
                occurred_at=_now(),
                event_type="covenant.version.approved",
                subject_type="covenant_version",
                subject_id=first.subject_id,
                payload={"foo": "qux"},
                prev_hash="hash-2",
                hash="hash-3",
                created_at=_now(),
                updated_at=_now(),
                request_id=_request_id("5"),
            )
        )
        with pytest.raises(IntegrityError, match="sequence"):
            session.commit()


_UPDATE_OR_DELETE_AUDIT_EVENT = re.compile(
    r"(?i)\b(update\s+audit_event\b"
    r"|delete\s+from\s+audit_event\b"
    r"|AuditEvent\s*\)[^\n]{0,80}\.(update|delete)\("
    r"|session\.delete\([^\n]{0,80}AuditEvent)"
)


def test_no_update_or_delete_sql_against_audit_event_in_source() -> None:
    offending: list[str] = []
    for path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _UPDATE_OR_DELETE_AUDIT_EVENT.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            offending.append(f"{path.relative_to(_REPO_ROOT)}:{line_number}: {match.group(0)!r}")

    assert offending == [], "UPDATE/DELETE against audit_event found:\n" + "\n".join(offending)
