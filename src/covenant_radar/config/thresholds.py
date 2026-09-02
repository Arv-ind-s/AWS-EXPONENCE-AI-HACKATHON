"""Versioned, approved decision thresholds.

Thresholds are data, not policy hidden in executable branches.  The default
file is parsed and validated once, then the first process that sees no active
snapshot records it as the shipped baseline.  Subsequent changes travel
through the maker-checker persistence port and become effective only when a
different actor approves them.

This module intentionally has no database or framework imports.  The
``ThresholdRepository`` and ``ThresholdAuditWriter`` protocols are the
boundary to the database adapter and the ``C-60`` audit service respectively.
Implementations keep the snapshot, maker-checker state and audit event in the
caller's one transaction; this store never commits on a caller's behalf.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Protocol, cast
from uuid import UUID

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id

DEFAULT_THRESHOLD_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "config" / "thresholds.default.json"
)
THRESHOLD_NAMES: Final[tuple[str, ...]] = tuple(f"T{number}" for number in range(1, 13))
_THRESHOLD_NAME_SET: Final[frozenset[str]] = frozenset(THRESHOLD_NAMES)
_REQUEST_SUBJECT_TYPE: Final[str] = "threshold_change"
_REQUEST_OPERATION: Final[str] = "threshold_change"
_APPROVED_SOURCE: Final[str] = "approved"
_DEFAULT_SOURCE: Final[str] = "default"
_MAX_CONFIG_BYTES: Final[int] = 64 * 1024
_DECIMAL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "act",
        "amber",
        "confidence_floor",
        "headroom_erosion_pct",
        "contribution_share",
        "monthly_budget",
        "ocr_confidence_floor",
        "auto_accept",
        "review_floor",
    }
)
_INTEGER_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "sustained_days",
        "sustained_events",
        "event_window_days",
        "max_output_tokens",
        "calls_per_hour",
        "calls_per_day",
        "bad_shape_retries",
        "act_sla_hours",
        "amber_sla_hours",
        "watch_sla_hours",
    }
)


class ThresholdConfigError(ValidationError):
    """The threshold file or a proposed value violates a known rule."""


@dataclass(frozen=True)
class ThresholdSnapshotRecord:
    """Persistence-neutral representation of an active threshold snapshot."""

    id: UUID
    values: Mapping[str, object]
    source: str
    effective_from: datetime
    version: int
    proposed_by_id: UUID | None = None
    approved_by_id: UUID | None = None
    note: str | None = None


@dataclass(frozen=True)
class ThresholdProposalRecord:
    """Persistence-neutral representation of a maker-checker request."""

    id: UUID
    state: str
    maker_id: UUID
    payload: Mapping[str, object]
    version: int = 1


class ThresholdRepository(Protocol):
    """Database-adapter port required by the threshold store.

    Implementations must perform ``lock_pending_proposal`` with a row lock or
    equivalent optimistic concurrency check.  All methods participate in the
    caller's existing transaction and must not commit independently.
    """

    def get_active_snapshot(self, *, as_of: datetime) -> ThresholdSnapshotRecord | None:
        """Return the latest snapshot effective at ``as_of``."""

    def create_snapshot(
        self,
        *,
        snapshot_id: UUID,
        values: Mapping[str, object],
        source: str,
        effective_from: datetime,
        proposed_by_id: UUID | None,
        approved_by_id: UUID | None,
        note: str | None,
        actor_id: UUID | None,
        request_id: str,
    ) -> ThresholdSnapshotRecord:
        """Persist and return one immutable snapshot."""

    def create_pending_proposal(
        self,
        *,
        proposal_id: UUID,
        maker_id: UUID,
        payload: Mapping[str, object],
        created_at: datetime,
        request_id: str,
    ) -> ThresholdProposalRecord:
        """Persist one pending maker-checker request."""

    def lock_pending_proposal(self, proposal_id: UUID) -> ThresholdProposalRecord | None:
        """Load one proposal for update, or return ``None`` if absent."""

    def mark_proposal_approved(
        self,
        *,
        proposal_id: UUID,
        approver_id: UUID,
        decided_at: datetime,
        request_id: str,
    ) -> None:
        """Transition the locked proposal to approved."""

    def record_audit(
        self,
        *,
        event_type: str,
        subject_type: str,
        subject_id: UUID,
        payload: Mapping[str, object],
        actor: object,
        occurred_at: datetime,
        request_id: str,
    ) -> object:
        """Append an audit event in the current transaction."""


class ThresholdAuditWriter(Protocol):
    """The part of ``C-60`` used by threshold approval."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append an audit event in the current transaction."""


@dataclass(frozen=True)
class ThresholdProposal:
    """A defensive view of a pending maker-checker threshold change."""

    id: UUID
    state: str
    proposer_id: UUID
    before: Mapping[str, object]
    after: Mapping[str, object]
    note: str | None


class ThresholdStore:
    """Read and govern the active threshold snapshot.

    ``repository`` is optional for configuration-only use such as startup
    validation and offline tests.  Persistence, proposal and approval require
    a repository.  ``session`` is a compatibility spelling for callers that
    already name their injected persistence object a session; it is still a
    ``ThresholdRepository``, never a framework session.
    """

    def __init__(
        self,
        repository: ThresholdRepository | None = None,
        *,
        session: ThresholdRepository | None = None,
        path: Path | str = DEFAULT_THRESHOLD_PATH,
        clock: Clock | None = None,
        audit: ThresholdAuditWriter | None = None,
        request_id: str | None = None,
    ) -> None:
        if repository is not None and session is not None:
            raise TypeError("Pass either repository or session, not both.")
        self._repository = repository or session
        self._path = Path(path)
        self._clock = clock or SystemClock()
        self._audit = audit
        self._request_id = request_id or get_request_id() or new_request_id()
        self._values: dict[str, dict[str, object]] | None = None
        self._snapshot: ThresholdSnapshotRecord | None = None
        self._file_values: dict[str, dict[str, object]] | None = None

        if self._repository is None:
            self._file_values = self._read_file()
            self._values = copy.deepcopy(self._file_values)
            self._snapshot = ThresholdSnapshotRecord(
                id=_snapshot_key(self._values),
                values=copy.deepcopy(self._values),
                source=_DEFAULT_SOURCE,
                effective_from=_utc_now(self._clock),
                version=1,
            )
        else:
            self._load_active_snapshot()

    def get(self, name: str) -> Mapping[str, object]:
        """Return a defensive copy of one threshold from the active snapshot."""
        if name not in _THRESHOLD_NAME_SET:
            raise KeyError(_unknown_threshold_message(name))
        return copy.deepcopy(self._active_values()[name])

    def values(self) -> Mapping[str, Mapping[str, object]]:
        """Return a defensive copy of all active threshold values."""
        return copy.deepcopy(self._active_values())

    def snapshot_id(self) -> UUID:
        """Return the snapshot id that must be stamped on a decision."""
        self._active_values()
        if self._snapshot is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Threshold store has no active snapshot.")
        return self._snapshot.id

    def reload(self) -> Mapping[str, Mapping[str, object]]:
        """Validate the file and return it without bypassing approval.

        A database-backed active snapshot remains in force after a valid file
        reload; an operator must propose and approve the candidate.  A
        malformed reload raises while leaving the active snapshot and the
        last-good file cache untouched.
        """
        candidate = self._read_file()
        self._file_values = copy.deepcopy(candidate)
        if self._repository is None:
            self._values = copy.deepcopy(candidate)
            self._snapshot = ThresholdSnapshotRecord(
                id=_snapshot_key(candidate),
                values=copy.deepcopy(candidate),
                source=_DEFAULT_SOURCE,
                effective_from=_utc_now(self._clock),
                version=1,
            )
        return copy.deepcopy(candidate)

    def propose(
        self,
        values: Mapping[str, object],
        actor: object,
        *,
        note: str | None = None,
    ) -> ThresholdProposal:
        """Create a pending threshold change in the caller's transaction."""
        repository = self._require_repository("propose")
        actor_id = _actor_id(actor)
        before = copy.deepcopy(self._active_values())
        after = _merge_proposal(before, values)
        _validate_thresholds(after)
        validated_note = _validated_note(note) if note is not None else None
        now = _utc_now(self._clock)
        proposal_id = new_id()
        payload: dict[str, object] = {
            "before": _json_safe(before),
            "after": _json_safe(after),
            "base_snapshot_id": str(self.snapshot_id()),
        }
        if validated_note is not None:
            payload["note"] = validated_note
        proposal = repository.create_pending_proposal(
            proposal_id=proposal_id,
            maker_id=actor_id,
            payload=payload,
            created_at=now,
            request_id=self._request_id,
        )
        return ThresholdProposal(
            id=proposal.id,
            state=proposal.state,
            proposer_id=proposal.maker_id,
            before=copy.deepcopy(before),
            after=copy.deepcopy(after),
            note=validated_note,
        )

    def approve(self, proposal_id: UUID | str, actor: object) -> ThresholdSnapshotRecord:
        """Approve one pending proposal and activate an immutable snapshot."""
        repository = self._require_repository("approve")
        actor_id = _actor_id(actor)
        parsed_id = _parse_uuid(proposal_id, "proposal id")
        proposal = repository.lock_pending_proposal(parsed_id)
        if proposal is None:
            raise NotFound(f"Threshold proposal {parsed_id} was not found.")
        if proposal.state != "pending":
            raise Conflict(
                f"Threshold proposal {parsed_id} is {proposal.state}, not pending; "
                "only pending proposals can be approved."
            )
        if proposal.maker_id == actor_id:
            raise Conflict(
                f"Threshold proposal {parsed_id} cannot be approved by its proposer; "
                "the distinct-actor rule requires a different approver."
            )

        active = self._load_active_snapshot(force=True)
        payload = _proposal_payload(proposal.payload, parsed_id)
        if payload["base_snapshot_id"] != str(active.id):
            raise Conflict(
                f"Threshold proposal {parsed_id} is stale: active snapshot changed from "
                f"{payload['base_snapshot_id']} to {active.id}."
            )
        before = cast(dict[str, dict[str, object]], payload["before"])
        after = cast(dict[str, dict[str, object]], payload["after"])
        now = _utc_now(self._clock)
        snapshot = repository.create_snapshot(
            snapshot_id=new_id(),
            values=cast(dict[str, object], _json_safe(after)),
            source=_APPROVED_SOURCE,
            effective_from=now,
            proposed_by_id=proposal.maker_id,
            approved_by_id=actor_id,
            note=cast(str | None, payload.get("note")),
            actor_id=actor_id,
            request_id=self._request_id,
        )
        repository.mark_proposal_approved(
            proposal_id=parsed_id,
            approver_id=actor_id,
            decided_at=now,
            request_id=self._request_id,
        )
        audit_payload: dict[str, object] = {
            "proposal_id": str(parsed_id),
            "before": _json_safe(before),
            "after": _json_safe(after),
            "proposer_id": str(proposal.maker_id),
            "approver_id": str(actor_id),
        }
        self._record_approval_audit(repository, snapshot, actor, audit_payload, now)
        self._snapshot = snapshot
        self._values = _from_storage(snapshot.values)
        return snapshot

    def _active_values(self) -> dict[str, dict[str, object]]:
        if self._values is None:
            self._load_active_snapshot()
        if self._values is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Threshold store has not been initialized.")
        return self._values

    def _load_active_snapshot(self, *, force: bool = False) -> ThresholdSnapshotRecord:
        if self._snapshot is not None and not force:
            return self._snapshot
        if self._repository is None:  # pragma: no cover - caller invariant
            raise RuntimeError("A repository is required for persisted snapshots.")
        snapshot = self._repository.get_active_snapshot(as_of=_utc_now(self._clock))
        if snapshot is None:
            values = self._read_file()
            now = _utc_now(self._clock)
            snapshot = self._repository.create_snapshot(
                snapshot_id=new_id(),
                values=cast(dict[str, object], _json_safe(values)),
                source=_DEFAULT_SOURCE,
                effective_from=now,
                proposed_by_id=None,
                approved_by_id=None,
                note="Packaged threshold defaults.",
                actor_id=None,
                request_id=self._request_id,
            )
        self._snapshot = snapshot
        self._values = _from_storage(snapshot.values)
        return snapshot

    def _record_approval_audit(
        self,
        repository: ThresholdRepository,
        snapshot: ThresholdSnapshotRecord,
        actor: object,
        payload: Mapping[str, object],
        occurred_at: datetime,
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                "threshold_change_approved",
                ("threshold_snapshot", snapshot.id),
                payload,
                actor=actor,
                request_id=self._request_id,
            )
            return
        repository.record_audit(
            event_type="threshold_change_approved",
            subject_type="threshold_snapshot",
            subject_id=snapshot.id,
            payload=payload,
            actor=actor,
            occurred_at=occurred_at,
            request_id=self._request_id,
        )

    def _require_repository(self, operation: str) -> ThresholdRepository:
        if self._repository is None:
            raise RuntimeError(f"A threshold repository is required to {operation} thresholds.")
        return self._repository

    def _read_file(self) -> dict[str, dict[str, object]]:
        try:
            size = self._path.stat().st_size
        except OSError as error:
            raise ThresholdConfigError(
                f"Threshold configuration cannot be read at {self._path}: {error}.",
                field="thresholds.file",
            ) from error
        if size > _MAX_CONFIG_BYTES:
            raise ThresholdConfigError(
                f"Threshold configuration exceeds {_MAX_CONFIG_BYTES} bytes at {self._path}.",
                field="thresholds.file",
            )
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ThresholdConfigError(
                f"Threshold configuration cannot be read at {self._path}: {error}.",
                field="thresholds.file",
            ) from error
        try:
            raw = json.loads(
                text,
                parse_int=Decimal,
                parse_float=Decimal,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except json.JSONDecodeError as error:
            raise ThresholdConfigError(
                f"Malformed threshold configuration at {self._path}, line {error.lineno}, "
                f"column {error.colno}: {error.msg}.",
                field="thresholds.file",
            ) from error
        except ValueError as error:
            raise ThresholdConfigError(
                f"Malformed threshold configuration at {self._path}: {error}.",
                field="thresholds.file",
            ) from error
        values = _normalise_file(raw)
        _validate_thresholds(values)
        return values


def get(
    name: str,
    *,
    store: ThresholdStore | None = None,
) -> Mapping[str, object]:
    """Convenience accessor backed by a process-local file store."""
    return (store or ThresholdStore()).get(name)


def snapshot_id(*, store: ThresholdStore | None = None) -> UUID:
    """Convenience accessor for the process-local snapshot identifier."""
    return (store or ThresholdStore()).snapshot_id()


def scan_threshold_literals(source_root: Path | str) -> tuple[str, ...]:
    """Find executable threshold-valued literals outside this module.

    The scan is AST-based, so values in comments and docstrings are harmless.
    Integer values are reported only when their assignment is threshold-shaped;
    common integers used for storage boundaries or UUID plumbing are not policy
    and must not create gate noise.  The result is deterministic for a gate.
    """
    root = Path(source_root)
    threshold_values = _numeric_threshold_values(
        _normalise_file(_read_json_file(DEFAULT_THRESHOLD_PATH))
    )
    findings: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            findings.append(f"{path}: unable to scan source: {error}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
                continue
            if not isinstance(node.value, int | float):
                continue
            try:
                decimal_value = Decimal(str(node.value))
            except InvalidOperation:
                continue
            if (
                decimal_value in threshold_values
                and not _is_length_literal(tree, node)
                and _is_threshold_literal(tree, node, decimal_value)
            ):
                findings.append(f"{path}:{node.lineno}: threshold literal {node.value!r}")
    return tuple(sorted(findings))


def assert_no_threshold_literals(source_root: Path | str) -> None:
    """Raise when executable source contains a threshold literal."""
    findings = scan_threshold_literals(source_root)
    if findings:
        raise ThresholdConfigError(
            "Threshold literals found outside the threshold store: " + "; ".join(findings),
            field="thresholds.source_scan",
        )


def _normalise_file(raw: object) -> dict[str, dict[str, object]]:
    if not isinstance(raw, Mapping):
        raise ThresholdConfigError(
            "Threshold configuration must be a JSON object containing T1 through T12.",
            field="thresholds",
        )
    actual_names = {str(key) for key in raw}
    expected_names = set(THRESHOLD_NAMES)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ThresholdConfigError(
            "Threshold configuration must contain exactly T1 through T12 ("
            + "; ".join(details)
            + ").",
            field="thresholds",
        )

    values: dict[str, dict[str, object]] = {}
    for name in THRESHOLD_NAMES:
        item = raw[name]
        if not isinstance(item, Mapping):
            raise ThresholdConfigError(
                f"Threshold {name} must be an object.", field=f"thresholds.{name}"
            )
        values[name] = {str(key): _normalise_scalar(str(key), value) for key, value in item.items()}
    return values


def _normalise_scalar(field: str, value: object) -> object:
    if field in _DECIMAL_FIELDS:
        if value is None and field == "monthly_budget":
            return None
        return _decimal(value, field)
    if field in _INTEGER_FIELDS:
        if isinstance(value, bool):
            raise ThresholdConfigError(f"{field} must be an integer, not a boolean.", field=field)
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise ThresholdConfigError(f"{field} must be an integer.", field=field) from error
        if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
            raise ThresholdConfigError(f"{field} must be an integer.", field=field)
        return int(decimal_value)
    if field == "deadline_ist":
        if not isinstance(value, str):
            raise ThresholdConfigError(
                "deadline_ist must be a string in HH:MM format.", field=field
            )
        return value
    raise ThresholdConfigError(f"Unknown field {field!r} in threshold configuration.", field=field)


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ThresholdConfigError(f"{field} must be a finite decimal.", field=field)
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ThresholdConfigError(f"{field} must be a finite decimal.", field=field) from error
    if not result.is_finite():
        raise ThresholdConfigError(f"{field} must be a finite decimal.", field=field)
    return result


def _validate_thresholds(values: Mapping[str, Mapping[str, object]]) -> None:
    if set(values) != set(THRESHOLD_NAMES):
        raise ThresholdConfigError(
            "Threshold set must contain exactly T1 through T12.", field="thresholds"
        )

    _require_keys(values["T1"], "T1", "act", "amber")
    act = _as_decimal(values["T1"]["act"], "T1.act")
    amber = _as_decimal(values["T1"]["amber"], "T1.amber")
    _between_zero_and_one(act, "T1.act")
    _between_zero_and_one(amber, "T1.amber")
    if amber > act:
        raise ThresholdConfigError("T1 invariant: amber must not exceed act.", field="T1.amber")

    _require_keys(values["T2"], "T2", "confidence_floor")
    confidence_floor = _as_decimal(values["T2"]["confidence_floor"], "T2.confidence_floor")
    _between_zero_and_one(confidence_floor, "T2.confidence_floor")

    _require_positive_integer(values["T3"], "T3", "sustained_days")
    _require_positive_integer(values["T3"], "T3", "sustained_events")
    _require_positive_integer(values["T3"], "T3", "event_window_days")
    sustained_days = cast(int, values["T3"]["sustained_days"])
    event_window_days = cast(int, values["T3"]["event_window_days"])
    if sustained_days > event_window_days:
        raise ThresholdConfigError(
            "T3 invariant: sustained_days must not exceed event_window_days.",
            field="T3.sustained_days",
        )

    _require_fraction(values["T4"], "T4", "headroom_erosion_pct")
    _require_fraction(values["T5"], "T5", "contribution_share")
    _require_positive_integer(values["T6"], "T6", "max_output_tokens")

    _require_positive_integer(values["T7"], "T7", "calls_per_hour")
    _require_positive_integer(values["T7"], "T7", "calls_per_day")
    calls_per_hour = cast(int, values["T7"]["calls_per_hour"])
    calls_per_day = cast(int, values["T7"]["calls_per_day"])
    if calls_per_hour > calls_per_day:
        raise ThresholdConfigError(
            "T7 invariant: calls_per_hour must not exceed calls_per_day.",
            field="T7.calls_per_hour",
        )
    monthly_budget = values["T7"].get("monthly_budget")
    if monthly_budget is not None and _as_decimal(monthly_budget, "T7.monthly_budget") <= 0:
        raise ThresholdConfigError(
            "T7 invariant: monthly_budget must be positive when configured.",
            field="T7.monthly_budget",
        )

    _require_nonnegative_integer(values["T8"], "T8", "bad_shape_retries")
    _require_fraction(values["T9"], "T9", "ocr_confidence_floor")

    _require_keys(values["T10"], "T10", "auto_accept", "review_floor")
    auto_accept = _as_decimal(values["T10"]["auto_accept"], "T10.auto_accept")
    review_floor = _as_decimal(values["T10"]["review_floor"], "T10.review_floor")
    _between_zero_and_one(auto_accept, "T10.auto_accept")
    _between_zero_and_one(review_floor, "T10.review_floor")
    if review_floor > auto_accept:
        raise ThresholdConfigError(
            "T10 invariant: review_floor must not exceed auto_accept.",
            field="T10.review_floor",
        )

    _require_positive_integer(values["T11"], "T11", "act_sla_hours")
    _require_positive_integer(values["T11"], "T11", "amber_sla_hours")
    _require_positive_integer(values["T11"], "T11", "watch_sla_hours")
    act_sla_hours = cast(int, values["T11"]["act_sla_hours"])
    amber_sla_hours = cast(int, values["T11"]["amber_sla_hours"])
    watch_sla_hours = cast(int, values["T11"]["watch_sla_hours"])
    if not act_sla_hours <= amber_sla_hours <= watch_sla_hours:
        raise ThresholdConfigError(
            "T11 invariant: SLA hours must be ordered act <= amber <= watch.", field="T11"
        )

    _require_keys(values["T12"], "T12", "deadline_ist")
    deadline = values["T12"]["deadline_ist"]
    if (
        not isinstance(deadline, str)
        or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", deadline) is None
    ):
        raise ThresholdConfigError(
            "T12 invariant: deadline_ist must use 24-hour HH:MM format.",
            field="T12.deadline_ist",
        )


def _require_keys(values: Mapping[str, object], name: str, *keys: str) -> None:
    expected = set(keys)
    actual = set(values)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ThresholdConfigError(
            f"{name} invariant: fields must be exactly {', '.join(keys)} ("
            + "; ".join(details)
            + ").",
            field=name,
        )


def _require_positive_integer(values: Mapping[str, object], name: str, key: str) -> None:
    _require_integer(values, name, key)
    if cast(int, values[key]) <= 0:
        raise ThresholdConfigError(
            f"{name} invariant: {key} must be positive.", field=f"{name}.{key}"
        )


def _require_nonnegative_integer(values: Mapping[str, object], name: str, key: str) -> None:
    _require_integer(values, name, key)
    if cast(int, values[key]) < 0:
        raise ThresholdConfigError(
            f"{name} invariant: {key} must not be negative.", field=f"{name}.{key}"
        )


def _require_integer(values: Mapping[str, object], name: str, key: str) -> None:
    if key not in values or not isinstance(values[key], int) or isinstance(values[key], bool):
        raise ThresholdConfigError(
            f"{name} invariant: {key} must be an integer.", field=f"{name}.{key}"
        )


def _require_fraction(values: Mapping[str, object], name: str, key: str) -> None:
    _require_keys(values, name, key)
    _between_zero_and_one(_as_decimal(values[key], f"{name}.{key}"), f"{name}.{key}")


def _between_zero_and_one(value: Decimal, field: str) -> None:
    if value < 0 or value > 1:
        raise ThresholdConfigError(
            f"{field} invariant: value must be between 0 and 1 inclusive.", field=field
        )


def _as_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ThresholdConfigError(f"{field} invariant: value must be a decimal.", field=field)
    return value


def _merge_proposal(
    before: Mapping[str, Mapping[str, object]], values: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    if not isinstance(values, Mapping) or not values:
        raise ThresholdConfigError("Threshold proposal must be a non-empty object.", field="values")
    after = cast(dict[str, dict[str, object]], copy.deepcopy(dict(before)))
    for name, raw_value in values.items():
        if name not in _THRESHOLD_NAME_SET:
            raise KeyError(_unknown_threshold_message(str(name)))
        current = after[name]
        if isinstance(raw_value, Mapping):
            patch = dict(raw_value)
        else:
            keys = tuple(current)
            if len(keys) != 1:
                raise ThresholdConfigError(
                    f"{name} proposal must be an object with fields: {', '.join(keys)}.",
                    field=f"values.{name}",
                )
            patch = {keys[0]: raw_value}
        for key, raw in patch.items():
            if key not in current:
                raise ThresholdConfigError(
                    f"Unknown field {key!r} in {name} proposal.",
                    field=f"values.{name}.{key}",
                )
            current[key] = _normalise_scalar(key, raw)
    return after


def _proposal_payload(payload: Mapping[str, object], proposal_id: UUID) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise Conflict(f"Threshold proposal {proposal_id} has an invalid payload.")
    before = payload.get("before")
    after = payload.get("after")
    base_snapshot_id = payload.get("base_snapshot_id")
    if (
        not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or not isinstance(base_snapshot_id, str)
    ):
        raise Conflict(f"Threshold proposal {proposal_id} has an invalid payload shape.")
    try:
        normal_before = _normalise_file(dict(before))
        normal_after = _normalise_file(dict(after))
        _validate_thresholds(normal_before)
        _validate_thresholds(normal_after)
    except ThresholdConfigError as error:
        raise Conflict(f"Threshold proposal {proposal_id} has invalid values: {error}") from error
    result: dict[str, object] = {
        "before": normal_before,
        "after": normal_after,
        "base_snapshot_id": base_snapshot_id,
    }
    if "note" in payload:
        try:
            result["note"] = _validated_note(cast(str, payload["note"]))
        except (ThresholdConfigError, TypeError) as error:
            raise Conflict(f"Threshold proposal {proposal_id} has an invalid note.") from error
    return result


def _validated_note(note: str) -> str:
    if not isinstance(note, str) or not note.strip():
        raise ThresholdConfigError("Threshold proposal note must not be blank.", field="note")
    if len(note) > 2000:
        raise ThresholdConfigError(
            "Threshold proposal note must be at most 2000 characters.", field="note"
        )
    return note.strip()


def _actor_id(actor: object) -> UUID:
    if isinstance(actor, UUID):
        return actor
    actor_value = getattr(actor, "id", None)
    if isinstance(actor_value, UUID):
        return actor_value
    raise TypeError("Threshold actor must be a UUID or an object with a UUID id.")


def _parse_uuid(value: UUID | str, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError) as error:
        raise ValidationError(f"Invalid {label}: {value!r}.", field=label) from error


def _utc_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.tzinfo is None:
        raise ValueError("Threshold clock returned a naive datetime.")
    return value.astimezone(UTC)


def _from_storage(values: Mapping[str, object]) -> dict[str, dict[str, object]]:
    normalised = _normalise_file(values)
    _validate_thresholds(normalised)
    return normalised


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


def _snapshot_key(values: Mapping[str, object]) -> UUID:
    encoded = json.dumps(
        _json_safe(values), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).digest()
    return UUID(bytes=digest[:16], version=5)


def _unknown_threshold_message(name: str) -> str:
    return f"Unknown threshold {name!r}; valid names: {', '.join(THRESHOLD_NAMES)}."


def _read_json_file(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_int=Decimal,
        parse_float=Decimal,
        object_pairs_hook=_reject_duplicate_json_keys,
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"duplicate JSON key {key!r}")
        values[key] = value
    return values


def _numeric_threshold_values(values: Mapping[str, Mapping[str, object]]) -> frozenset[Decimal]:
    numbers: set[Decimal] = set()
    for threshold in values.values():
        for value in threshold.values():
            if isinstance(value, Decimal):
                numbers.add(value)
            elif isinstance(value, int) and not isinstance(value, bool):
                numbers.add(Decimal(value))
    return frozenset(numbers)


def _is_length_literal(tree: ast.AST, node: ast.Constant) -> bool:
    for parent in ast.walk(tree):
        if not isinstance(parent, ast.Assign) or node not in ast.walk(parent.value):
            continue
        for target in parent.targets:
            if isinstance(target, ast.Name) and (
                target.id.endswith("_MAX_LENGTH") or target.id.endswith("_MAX")
            ):
                return True
    return False


def _is_threshold_literal(tree: ast.AST, node: ast.Constant, value: Decimal) -> bool:
    """Return whether a matching number is used as policy rather than plumbing."""
    if value != value.to_integral_value():
        return True
    threshold_words = (
        "THRESHOLD",
        "SUSTAINED",
        "EVENT_WINDOW",
        "OUTPUT_TOKEN",
        "CALLS_PER_",
        "BAD_SHAPE",
        "OCR_CONFIDENCE",
        "AUTO_ACCEPT",
        "REVIEW_FLOOR",
        "SLA_HOURS",
    )
    for parent in ast.walk(tree):
        if not isinstance(parent, ast.Assign) or node not in ast.walk(parent.value):
            continue
        for target in parent.targets:
            if isinstance(target, ast.Name) and any(word in target.id for word in threshold_words):
                return True
            if isinstance(target, ast.Name) and re.fullmatch(
                r"T(?:[1-9]|1[0-2])(?:_[A-Z0-9_]+)?", target.id
            ):
                return True
    return False


__all__ = [
    "DEFAULT_THRESHOLD_PATH",
    "THRESHOLD_NAMES",
    "ThresholdAuditWriter",
    "ThresholdConfigError",
    "ThresholdProposal",
    "ThresholdProposalRecord",
    "ThresholdRepository",
    "ThresholdSnapshotRecord",
    "ThresholdStore",
    "assert_no_threshold_literals",
    "get",
    "scan_threshold_literals",
    "snapshot_id",
]
