"""Contradiction and supersession rules for the evidence ledger.

Evidence is an append-only interpretation of immutable signal events.  When a
later observation changes that interpretation, this module produces a new
evidence item and marks the previous item as superseded.  It never mutates a
caller-owned value and it never removes an item from a supplied history.

The functions in this module are deliberately independent of SQLAlchemy.  A
scoring job can therefore resolve a whole batch before it starts its database
transaction, and the same rules can be used by a point-in-time reader or an
offline replay.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Final, cast, overload
from uuid import UUID

from covenant_radar.core.ids import new_id
from covenant_radar.domain.signals.evidence import (
    EvidenceFacts,
    EvidenceScore,
    EvidenceTransitionFacts,
    SignalEventFacts,
    to_evidence_facts,
    to_signal_event_facts,
)

SUPERSEDED_STATE: Final[str] = "superseded"
_ACTIVE_STATES: Final[frozenset[str]] = frozenset({"transient", "sustained"})
_RULE_MAX_LENGTH: Final[int] = 100
_FAMILY_MAX_LENGTH: Final[int] = 20
_TYPE_MAX_LENGTH: Final[int] = 50


def _text(value: object, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains a control character.")
    return normalized


@dataclass(frozen=True, slots=True)
class ContradictionRule:
    """One typed contradiction relationship.

    ``superseding_event_type`` is the event type whose observation wins when
    it is later by event date.  The optional polarity fields support the
    existing single-type signal taxonomy: for example, a non-adverse
    ``payment_delay`` observation can resolve an adverse one without making
    the ingestion taxonomy invent a second raw event type.
    """

    family: str
    superseded_event_type: str
    superseding_event_type: str
    rule: str
    superseded_is_adverse: bool | None = None
    superseding_is_adverse: bool | None = None

    def __post_init__(self) -> None:
        family = _text(self.family, "family", _FAMILY_MAX_LENGTH)
        superseded_event_type = _text(
            self.superseded_event_type,
            "superseded_event_type",
            _TYPE_MAX_LENGTH,
        )
        superseding_event_type = _text(
            self.superseding_event_type,
            "superseding_event_type",
            _TYPE_MAX_LENGTH,
        )
        rule = _text(self.rule, "rule", _RULE_MAX_LENGTH)
        for value, name in (
            (self.superseded_is_adverse, "superseded_is_adverse"),
            (self.superseding_is_adverse, "superseding_is_adverse"),
        ):
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be a boolean or None.")
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "superseded_event_type", superseded_event_type)
        object.__setattr__(self, "superseding_event_type", superseding_event_type)
        object.__setattr__(self, "rule", rule)

    @property
    def superseded_type(self) -> str:
        """Compatibility spelling for the predecessor event type."""

        return self.superseded_event_type

    @property
    def superseding_type(self) -> str:
        """Compatibility spelling for the successor event type."""

        return self.superseding_event_type

    def matches(
        self,
        superseded: EvidenceFacts | SignalEventFacts,
        superseding: EvidenceFacts | SignalEventFacts,
    ) -> bool:
        """Return whether this rule applies to the two supplied facts."""

        if superseded.family != self.family or superseding.family != self.family:
            return False
        if _evidence_type(superseded) != self.superseded_event_type:
            return False
        if _evidence_type(superseding) != self.superseding_event_type:
            return False
        return _polarity_matches(superseded, self.superseded_is_adverse) and _polarity_matches(
            superseding, self.superseding_is_adverse
        )

    def reverse(self, *, rule: str | None = None) -> ContradictionRule:
        """Return the same contradiction relationship in the reverse direction.

        The reverse relationship is used only when an event arrives late.  It
        lets the event-date ordering decide which item is current while still
        retaining a stable explanation for the transition.
        """

        reverse_rule = rule or f"{self.rule}.reverse"
        return ContradictionRule(
            family=self.family,
            superseded_event_type=self.superseding_event_type,
            superseding_event_type=self.superseded_event_type,
            rule=reverse_rule,
            superseded_is_adverse=self.superseding_is_adverse,
            superseding_is_adverse=self.superseded_is_adverse,
        )


@dataclass(frozen=True, slots=True)
class SupersessionResult:
    """The two retained states and the transition caused by one revision."""

    superseded: EvidenceScore
    successor: EvidenceScore
    rule: ContradictionRule
    occurred_on: date
    transition: EvidenceTransitionFacts

    @property
    def predecessor(self) -> EvidenceScore:
        """Compatibility spelling for the state that lost precedence."""

        return self.superseded

    @property
    def replacement(self) -> EvidenceScore:
        """Compatibility spelling for the state that now takes precedence."""

        return self.successor

    @property
    def items(self) -> tuple[EvidenceScore, EvidenceScore]:
        """Return both retained items in predecessor/successor order."""

        return self.superseded, self.successor


@dataclass(frozen=True, slots=True)
class SupersessionBatch(Sequence[EvidenceScore]):
    """The complete retained item set after resolving incoming evidence."""

    items: tuple[EvidenceScore, ...]
    revisions: tuple[SupersessionResult, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(item, EvidenceScore) for item in self.items):
            raise TypeError("SupersessionBatch items must be EvidenceScore values.")
        if any(not isinstance(revision, SupersessionResult) for revision in self.revisions):
            raise TypeError("SupersessionBatch revisions must be SupersessionResult values.")

    @property
    def transitions(self) -> tuple[EvidenceTransitionFacts, ...]:
        """Return the append-only transitions produced by this batch."""

        return tuple(revision.transition for revision in self.revisions)

    def __len__(self) -> int:
        return len(self.items)

    @overload
    def __getitem__(self, index: int) -> EvidenceScore: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[EvidenceScore, ...]: ...

    def __getitem__(self, index: int | slice) -> EvidenceScore | tuple[EvidenceScore, ...]:
        return self.items[index]

    def __iter__(self) -> Iterator[EvidenceScore]:
        return iter(self.items)


@dataclass(frozen=True, slots=True)
class _IncomingCandidate:
    score: EvidenceScore
    event: SignalEventFacts | None = None


def _rule_pair(
    family: str,
    first_type: str,
    second_type: str,
    *,
    code: str,
) -> tuple[ContradictionRule, ContradictionRule]:
    return (
        ContradictionRule(family, first_type, second_type, f"{code}.forward"),
        ContradictionRule(family, second_type, first_type, f"{code}.reverse"),
    )


# Rules are data.  Adding a new contradiction is a reviewable data change and
# does not require changing the scoring algorithm.  The first pair is the
# product's canonical example; the remaining pairs cover the same
# deterioration/recovery shape for every signal family.
_DEFAULT_RULE_LIST: tuple[ContradictionRule, ...] = (
    ContradictionRule(
        "account_activity",
        "account_activity_change",
        "account_activity_change",
        "signals.account_activity.adverse_resolved.v1",
        superseded_is_adverse=True,
        superseding_is_adverse=False,
    ),
    *_rule_pair(
        "payment",
        "payment_delay",
        "payment_received",
        code="signals.payment.delay_received.v1",
    ),
    ContradictionRule(
        "payment",
        "payment_delay",
        "payment_delay",
        "signals.payment.adverse_resolved.v1",
        superseded_is_adverse=True,
        superseding_is_adverse=False,
    ),
    *_rule_pair(
        "utilisation",
        "facility_utilisation",
        "utilisation_normalised",
        code="signals.utilisation.high_normal.v1",
    ),
    *_rule_pair(
        "treasury",
        "treasury_outflow",
        "treasury_normalised",
        code="signals.treasury.outflow_normal.v1",
    ),
    *_rule_pair(
        "concentration",
        "concentration_exposure",
        "concentration_reduced",
        code="signals.concentration.high_reduced.v1",
    ),
    *_rule_pair(
        "industry",
        "industry_indicator",
        "industry_recovered",
        code="signals.industry.stress_recovered.v1",
    ),
    *_rule_pair(
        "news",
        "news_event",
        "news_retracted",
        code="signals.news.adverse_retracted.v1",
    ),
)

DEFAULT_CONTRADICTION_RULES: Final[tuple[ContradictionRule, ...]] = _DEFAULT_RULE_LIST
SUPERSESSION_RULES: Final[tuple[ContradictionRule, ...]] = DEFAULT_CONTRADICTION_RULES

# A mapping is useful to configuration and diagnostics callers.  It is a
# defensive, immutable view; callers cannot silently alter process-wide rules.
_RULE_MAP: dict[str, dict[str, tuple[str, ...]]] = {}
for _rule in DEFAULT_CONTRADICTION_RULES:
    _RULE_MAP.setdefault(_rule.family, {}).setdefault(_rule.superseding_event_type, ())
    _RULE_MAP[_rule.family][_rule.superseding_event_type] = tuple(
        (*_RULE_MAP[_rule.family][_rule.superseding_event_type], _rule.superseded_event_type)
    )
CONTRADICTION_RULES: Final[Mapping[str, Mapping[str, tuple[str, ...]]]] = MappingProxyType(
    {family: MappingProxyType(mapping) for family, mapping in _RULE_MAP.items()}
)


def contradiction_rules() -> tuple[ContradictionRule, ...]:
    """Return the default contradiction rules as an immutable tuple."""

    return DEFAULT_CONTRADICTION_RULES


def find_rule(
    superseded: EvidenceFacts | SignalEventFacts,
    superseding: EvidenceFacts | SignalEventFacts,
    rules: Sequence[ContradictionRule] | Mapping[str, object] | None = None,
) -> ContradictionRule | None:
    """Find the first configured rule matching two evidence/event facts."""

    for rule in _normalise_rules(rules):
        if rule.matches(superseded, superseding):
            return rule
    return None


def supersede(
    predecessor: EvidenceFacts | Mapping[str, object] | object,
    successor: EvidenceFacts | Mapping[str, object] | object,
    *,
    occurred_on: date | None = None,
    rule: ContradictionRule | str | None = None,
    id_factory: object = new_id,
) -> SupersessionResult:
    """Link two evidence items without deleting either one.

    The predecessor must be an active, persisted item.  A missing successor
    id is minted through the UUIDv7 ``id_factory`` used by the application;
    this keeps the pure function usable before the successor ORM row exists.
    """

    old = to_evidence_facts(predecessor)
    new = to_evidence_facts(successor)
    if old.id is None:
        raise ValueError("A superseded evidence item must have a persisted id.")
    if old.id == new.id:
        raise ValueError("An evidence item cannot supersede itself.")
    if old.state not in _ACTIVE_STATES:
        raise ValueError("Only transient or sustained evidence can be superseded.")
    if old.superseded_by_id is not None:
        raise ValueError("An evidence item can have only one direct successor.")
    if new.supersedes_id not in {None, old.id}:
        raise ValueError("The successor already supersedes a different evidence item.")
    if new.superseded_by_id is not None:
        raise ValueError("A superseded item cannot become a successor.")
    if new.state not in _ACTIVE_STATES:
        raise ValueError("A supersession successor must be transient or sustained.")

    selected_rule = _coerce_rule(rule, old, new)
    if selected_rule is None:
        raise ValueError(
            f"No contradiction rule matches {old.family!r}/"
            f"{old.evidence_type!r} -> {new.evidence_type!r}."
        )
    supersession_date = _calendar_date(occurred_on or new.first_seen, "occurred_on")
    if supersession_date < old.first_seen:
        raise ValueError("occurred_on cannot precede the superseded item's first_seen date.")
    if supersession_date < new.first_seen:
        raise ValueError("occurred_on cannot precede the successor item's first_seen date.")

    successor_id = new.id
    if successor_id is None:
        if not callable(id_factory):
            raise TypeError("id_factory must be callable.")
        successor_id = id_factory()
        if not isinstance(successor_id, UUID):
            raise TypeError("id_factory must return a UUID.")
    old_score = _as_score(
        old,
        state=SUPERSEDED_STATE,
        counts_toward_pressure=False,
        superseded_by_id=successor_id,
    )
    successor_score = _as_score(new, id=successor_id, supersedes_id=old.id)
    transition = EvidenceTransitionFacts(
        evidence_id=old.id,
        from_state=old.state,
        to_state=SUPERSEDED_STATE,
        occurred_on=supersession_date,
        rule=selected_rule.rule,
    )
    return SupersessionResult(
        superseded=old_score,
        successor=successor_score,
        rule=selected_rule,
        occurred_on=supersession_date,
        transition=transition,
    )


def resolve_supersession(
    existing: Sequence[EvidenceFacts | Mapping[str, object] | object],
    incoming: Sequence[SignalEventFacts | EvidenceFacts | Mapping[str, object] | object],
    *,
    as_of: date | None = None,
    rules: Sequence[ContradictionRule] | Mapping[str, object] | None = None,
    id_factory: object = new_id,
) -> SupersessionBatch:
    """Resolve incoming evidence in event-date order.

    Incoming events are treated as new observations, not as replacements for
    an existing row.  This is what makes a repeated contradiction create a
    fresh item after a chain (A -> B -> C), rather than resurrecting A.  When
    an event arrives out of order, the already-known item wins if its
    observation date is later; the late item is retained but marked
    superseded.  Arrival order never determines the winner.
    """

    current = [_as_score(to_evidence_facts(item)) for item in existing]
    _validate_history_ids(current)
    normalized_rules = _normalise_rules(rules)
    candidates = _incoming_candidates(incoming, id_factory=id_factory, as_of=as_of)
    candidates.sort(key=lambda candidate: _item_sort_key(candidate.score))
    revisions: list[SupersessionResult] = []

    for incoming_candidate in candidates:
        candidate = incoming_candidate.score
        if _already_seen(candidate, current):
            continue
        match = _best_match(incoming_candidate, current, normalized_rules)
        if match is None:
            same_identity = next(
                (
                    index
                    for index, item in enumerate(current)
                    if item.identity == candidate.identity
                    and item.state in _ACTIVE_STATES
                    and item.superseded_by_id is None
                ),
                None,
            )
            if same_identity is None:
                # A normal new item is still part of the retained ledger. The
                # caller may subsequently pass it through persistence/
                # materiality scoring; supersession itself does not discard
                # evidence.
                current.append(candidate)
            else:
                # Evidence identity is stable across observations. Merge a
                # non-contradicting event into its active item so an ordinary
                # observation does not create duplicate ledger identities.
                current[same_identity] = _merge_observation(current[same_identity], candidate)
            continue

        existing_index, existing_item, selected_rule = match
        if candidate.first_seen >= existing_item.last_seen:
            revision = supersede(
                existing_item,
                candidate,
                occurred_on=candidate.first_seen,
                rule=selected_rule,
                id_factory=id_factory,
            )
        else:
            # The event is late.  The existing item's later event date is the
            # point at which it wins, so the late item is not allowed to
            # rewrite the current view.
            reverse_rule = selected_rule.reverse()
            revision = supersede(
                candidate,
                existing_item,
                occurred_on=existing_item.last_seen,
                rule=reverse_rule,
                id_factory=id_factory,
            )
        current[existing_index] = revision.successor
        current.append(revision.superseded)
        revisions.append(revision)

    return SupersessionBatch(
        items=tuple(sorted(current, key=_item_sort_key)),
        revisions=tuple(revisions),
    )


def apply_supersession(
    existing: Sequence[EvidenceFacts | Mapping[str, object] | object],
    incoming: Sequence[SignalEventFacts | EvidenceFacts | Mapping[str, object] | object],
    *,
    as_of: date | None = None,
    rules: Sequence[ContradictionRule] | Mapping[str, object] | None = None,
    id_factory: object = new_id,
) -> list[EvidenceScore]:
    """List-returning adapter around :func:`resolve_supersession`."""

    return list(
        resolve_supersession(
            existing,
            incoming,
            as_of=as_of,
            rules=rules,
            id_factory=id_factory,
        ).items
    )


def point_in_time(
    items: Sequence[EvidenceFacts | Mapping[str, object] | object],
    transitions: Sequence[EvidenceTransitionFacts | Mapping[str, object] | object],
    as_of: date,
) -> list[EvidenceFacts]:
    """Reconstruct the evidence ledger as it existed on ``as_of``.

    An item is visible only after its ``first_seen`` date.  State and
    supersession links are then replayed from the append-only transition trail
    through the requested date.  Links to items that did not yet exist are
    omitted, which is the detail that makes a pre-contradiction read match the
    prior warning exactly.
    """

    scoring_date = _calendar_date(as_of, "as_of")
    facts = [to_evidence_facts(item) for item in items]
    by_id = {item.id: item for item in facts if item.id is not None}
    if len(by_id) != sum(item.id is not None for item in facts):
        raise ValueError("Evidence item ids must be unique for point-in-time reconstruction.")
    trail = [_transition_facts(value) for value in transitions]
    transitions_by_item: dict[UUID, list[EvidenceTransitionFacts]] = {}
    for transition in trail:
        if transition.evidence_id is None:
            raise ValueError("Point-in-time transitions require evidence_id.")
        if transition.evidence_id not in by_id:
            raise ValueError("Point-in-time transition references an unknown evidence item.")
        transitions_by_item.setdefault(transition.evidence_id, []).append(transition)
    for values in transitions_by_item.values():
        # The repository supplies created-at/id ordering for equal dates. A
        # pure replay has no arrival timestamp, so preserve the caller's
        # stable order instead of inventing a lexical order for rules.
        values.sort(key=lambda value: value.occurred_on)

    result: list[EvidenceFacts] = []
    for item in sorted(facts, key=_item_sort_key):
        if item.first_seen > scoring_date:
            continue
        item_transitions = transitions_by_item.get(item.id, ()) if item.id is not None else ()
        state = _state_as_of(item, item_transitions, scoring_date)
        successor_id = _visible_successor(
            item,
            item.superseded_by_id,
            by_id,
            transitions_by_item,
            scoring_date,
        )
        predecessor_id = _visible_predecessor(
            item,
            item.supersedes_id,
            by_id,
            transitions_by_item,
            scoring_date,
        )
        if state != SUPERSEDED_STATE:
            successor_id = None
        if predecessor_id is not None and by_id[predecessor_id].first_seen > scoring_date:
            predecessor_id = None
        result.append(
            EvidenceFacts(
                id=item.id,
                borrower_id=item.borrower_id,
                facility_id=item.facility_id,
                family=item.family,
                evidence_type=item.evidence_type,
                first_seen=item.first_seen,
                last_seen=min(item.last_seen, scoring_date),
                persistence_days=item.persistence_days,
                event_count_window=item.event_count_window,
                materiality_pct=item.materiality_pct,
                decay_factor=item.decay_factor,
                state=state,
                counts_toward_pressure=(item.counts_toward_pressure and state != SUPERSEDED_STATE),
                superseded_by_id=successor_id,
                supersedes_id=predecessor_id,
                source_event_ids=item.source_event_ids,
            )
        )
    return result


def reconstruct_as_of(
    items: Sequence[EvidenceFacts | Mapping[str, object] | object],
    transitions: Sequence[EvidenceTransitionFacts | Mapping[str, object] | object],
    as_of: date,
) -> list[EvidenceFacts]:
    """Descriptive alias for :func:`point_in_time`."""

    return point_in_time(items, transitions, as_of)


def read_as_of(
    items: Sequence[EvidenceFacts | Mapping[str, object] | object],
    transitions: Sequence[EvidenceTransitionFacts | Mapping[str, object] | object],
    as_of: date,
) -> list[EvidenceFacts]:
    """Compatibility alias for point-in-time reads."""

    return point_in_time(items, transitions, as_of)


# Verb-first compatibility names make the stage discoverable alongside
# ``score_persistence`` and ``score_decay`` while keeping one implementation.
SupersessionRule = ContradictionRule
Revision = SupersessionResult
score_supersession = resolve_supersession
resolve_contradictions = resolve_supersession
supersede_evidence = supersede
point_in_time_read = point_in_time
reconstruct_ledger = point_in_time


def _incoming_candidates(
    incoming: Sequence[SignalEventFacts | EvidenceFacts | Mapping[str, object] | object],
    *,
    id_factory: object,
    as_of: date | None,
) -> list[_IncomingCandidate]:
    result: list[_IncomingCandidate] = []
    for value in incoming:
        event: SignalEventFacts | None = None
        if isinstance(value, EvidenceFacts):
            candidate = _as_score(value)
            if candidate.id is None:
                candidate = _as_score(candidate, id=_new_uuid(id_factory))
        else:
            try:
                event = to_signal_event_facts(value)
            except (TypeError, ValueError, KeyError, AttributeError):
                candidate = _as_score(to_evidence_facts(value))
                if candidate.id is None:
                    candidate = _as_score(candidate, id=_new_uuid(id_factory))
            else:
                if as_of is not None and event.event_date > _calendar_date(as_of, "as_of"):
                    continue
                event_id = _event_source_id(event)
                candidate = EvidenceScore(
                    id=_new_uuid(id_factory),
                    borrower_id=event.borrower_id,
                    facility_id=event.facility_id,
                    family=event.family,
                    evidence_type=event.evidence_type or event.event_type or "",
                    first_seen=event.event_date,
                    last_seen=event.event_date,
                    persistence_days=1,
                    event_count_window=1,
                    materiality_pct=None,
                    decay_factor=None,
                    state="transient",
                    counts_toward_pressure=False,
                    source_event_ids=(event_id,),
                )
        result.append(_IncomingCandidate(candidate, event))
    return result


def _best_match(
    incoming: _IncomingCandidate,
    current: Sequence[EvidenceScore],
    rules: Sequence[ContradictionRule],
) -> tuple[int, EvidenceScore, ContradictionRule] | None:
    candidate = incoming.score
    candidate_fact: EvidenceFacts | SignalEventFacts = incoming.event or candidate
    matches: list[tuple[int, EvidenceScore, ContradictionRule]] = []
    for index, item in enumerate(current):
        if item.state not in _ACTIVE_STATES or item.superseded_by_id is not None:
            continue
        if item.borrower_id != candidate.borrower_id or item.facility_id != candidate.facility_id:
            continue
        if item.id is not None and item.id == candidate.id:
            continue
        for rule in rules:
            if rule.matches(item, candidate_fact):
                matches.append((index, item, rule))
    if not matches:
        return None
    return max(matches, key=lambda value: (value[1].last_seen, str(value[1].id)))


def _coerce_rule(
    value: ContradictionRule | str | None,
    predecessor: EvidenceFacts,
    successor: EvidenceFacts,
) -> ContradictionRule | None:
    if value is None:
        return find_rule(predecessor, successor)
    if isinstance(value, ContradictionRule):
        if not value.matches(predecessor, successor):
            raise ValueError("The supplied contradiction rule does not match the two items.")
        return value
    if isinstance(value, str):
        for candidate in DEFAULT_CONTRADICTION_RULES:
            if candidate.rule == value:
                if not candidate.matches(predecessor, successor):
                    raise ValueError("The supplied contradiction rule does not match the items.")
                return candidate
        raise ValueError(f"Unknown contradiction rule {value!r}.")
    raise TypeError("rule must be a ContradictionRule, rule code, or None.")


def _normalise_rules(
    rules: Sequence[ContradictionRule] | Mapping[str, object] | None,
) -> tuple[ContradictionRule, ...]:
    if rules is None:
        return DEFAULT_CONTRADICTION_RULES
    if isinstance(rules, Mapping):
        values: list[ContradictionRule] = []
        for family, successors in rules.items():
            if not isinstance(successors, Mapping):
                raise TypeError("Contradiction rule family values must be mappings.")
            for successor_type, predecessors in successors.items():
                if isinstance(predecessors, str):
                    predecessor_values = (predecessors,)
                elif isinstance(predecessors, Sequence):
                    predecessor_values = tuple(predecessors)
                else:
                    raise TypeError("Contradiction rule predecessors must be text or a sequence.")
                for predecessor_type in predecessor_values:
                    values.append(
                        ContradictionRule(
                            family=str(family),
                            superseded_event_type=str(predecessor_type),
                            superseding_event_type=str(successor_type),
                            rule=(f"signals.{family}.{predecessor_type}.{successor_type}.v1"),
                        )
                    )
        return tuple(values)
    if isinstance(rules, str | bytes | bytearray):
        raise TypeError("rules must be a sequence of ContradictionRule values.")
    result = tuple(rules)
    if any(not isinstance(rule, ContradictionRule) for rule in result):
        raise TypeError("rules must contain only ContradictionRule values.")
    return result


def _as_score(
    item: EvidenceFacts,
    *,
    id: UUID | None = None,
    state: str | None = None,
    counts_toward_pressure: bool | None = None,
    superseded_by_id: UUID | None = None,
    supersedes_id: UUID | None = None,
) -> EvidenceScore:
    return EvidenceScore(
        id=item.id if id is None else id,
        borrower_id=item.borrower_id,
        facility_id=item.facility_id,
        family=item.family,
        evidence_type=item.evidence_type,
        first_seen=item.first_seen,
        last_seen=item.last_seen,
        persistence_days=item.persistence_days,
        event_count_window=item.event_count_window,
        materiality_pct=item.materiality_pct,
        decay_factor=item.decay_factor,
        state=item.state if state is None else state,
        counts_toward_pressure=(
            item.counts_toward_pressure
            if counts_toward_pressure is None
            else counts_toward_pressure
        ),
        superseded_by_id=(item.superseded_by_id if superseded_by_id is None else superseded_by_id),
        supersedes_id=item.supersedes_id if supersedes_id is None else supersedes_id,
        source_event_ids=item.source_event_ids,
    )


def _merge_observation(current: EvidenceScore, observation: EvidenceScore) -> EvidenceScore:
    """Merge one same-identity observation without changing its row id."""

    return EvidenceScore(
        id=current.id,
        borrower_id=current.borrower_id,
        facility_id=current.facility_id,
        family=current.family,
        evidence_type=current.evidence_type,
        first_seen=min(current.first_seen, observation.first_seen),
        last_seen=max(current.last_seen, observation.last_seen),
        persistence_days=current.persistence_days,
        event_count_window=current.event_count_window,
        materiality_pct=current.materiality_pct,
        decay_factor=current.decay_factor,
        state=current.state,
        counts_toward_pressure=current.counts_toward_pressure,
        superseded_by_id=current.superseded_by_id,
        supersedes_id=current.supersedes_id,
        source_event_ids=tuple(
            sorted(set(current.source_event_ids).union(observation.source_event_ids))
        ),
    )


def _transition_facts(
    value: EvidenceTransitionFacts | Mapping[str, object] | object,
) -> EvidenceTransitionFacts:
    if isinstance(value, EvidenceTransitionFacts):
        return value
    get = (
        value.get
        if isinstance(value, Mapping)
        else lambda name, default=None: getattr(value, name, default)
    )
    return EvidenceTransitionFacts(
        evidence_id=cast(UUID | None, get("evidence_id")),
        from_state=cast(str | None, get("from_state")),
        to_state=cast(str, get("to_state")),
        occurred_on=cast(date, get("occurred_on")),
        rule=cast(str, get("rule")),
        threshold_snapshot_id=cast(UUID | None, get("threshold_snapshot_id")),
    )


def _state_as_of(
    item: EvidenceFacts,
    transitions: Sequence[EvidenceTransitionFacts],
    as_of: date,
) -> str:
    applicable = tuple(value for value in transitions if value.occurred_on <= as_of)
    if not applicable:
        future = tuple(value for value in transitions if value.occurred_on > as_of)
        if future and future[0].from_state is not None:
            return future[0].from_state
        return item.state
    state = applicable[0].from_state
    for transition in applicable:
        if transition.from_state is not None and state not in {None, transition.from_state}:
            raise ValueError(
                f"Inconsistent evidence transition history for {item.id}: "
                f"expected {transition.from_state!r}, found {state!r}."
            )
        state = transition.to_state
    if state is None:
        raise ValueError(f"Evidence item {item.id} has no reconstructable state.")
    return state


def _visible_successor(
    item: EvidenceFacts,
    value: UUID | None,
    by_id: Mapping[UUID, EvidenceFacts],
    transitions_by_item: Mapping[UUID, Sequence[EvidenceTransitionFacts]],
    as_of: date,
) -> UUID | None:
    if value is None:
        return None
    target = by_id.get(value)
    if target is None or target.first_seen > as_of:
        return None
    if item.id is None:
        return None
    return (
        value
        if any(
            transition.to_state == SUPERSEDED_STATE and transition.occurred_on <= as_of
            for transition in transitions_by_item.get(item.id, ())
        )
        else None
    )


def _visible_predecessor(
    item: EvidenceFacts,
    value: UUID | None,
    by_id: Mapping[UUID, EvidenceFacts],
    transitions_by_item: Mapping[UUID, Sequence[EvidenceTransitionFacts]],
    as_of: date,
) -> UUID | None:
    if value is None:
        return None
    target = by_id.get(value)
    if target is None or target.first_seen > as_of or target.id is None:
        return None
    return (
        value
        if any(
            transition.to_state == SUPERSEDED_STATE
            and transition.occurred_on <= as_of
            and target.superseded_by_id == item.id
            for transition in transitions_by_item.get(target.id, ())
        )
        else None
    )


def _validate_history_ids(items: Sequence[EvidenceScore]) -> None:
    ids = [item.id for item in items if item.id is not None]
    if len(ids) != len(set(ids)):
        raise ValueError("Evidence item ids must be unique within a supersession batch.")
    for item in items:
        if item.id is not None and item.id in {item.superseded_by_id, item.supersedes_id}:
            raise ValueError("An evidence item cannot link to itself.")


def _already_seen(candidate: EvidenceScore, current: Sequence[EvidenceScore]) -> bool:
    candidate_sources = set(candidate.source_event_ids)
    if not candidate_sources:
        return False
    return any(candidate_sources.intersection(item.source_event_ids) for item in current)


def _item_sort_key(item: EvidenceFacts) -> tuple[str, str, str, date, str, str]:
    return (
        str(item.borrower_id),
        str(item.facility_id) if item.facility_id is not None else "",
        item.family,
        item.first_seen,
        item.evidence_type,
        str(item.id) if item.id is not None else "",
    )


def _evidence_type(value: EvidenceFacts | SignalEventFacts) -> str:
    if isinstance(value, SignalEventFacts):
        return value.evidence_type or value.event_type or ""
    return value.evidence_type


def _polarity_matches(value: EvidenceFacts | SignalEventFacts, expected: bool | None) -> bool:
    if expected is None:
        return True
    if isinstance(value, SignalEventFacts):
        candidate = value.payload.get("is_adverse")
        return candidate is expected
    # Persisted evidence does not duplicate raw-event polarity.  Its type is
    # the authoritative predecessor classification, so a type-only item is a
    # valid match for a polarity-aware rule.
    return True


def _event_source_id(event: SignalEventFacts) -> str:
    if event.event_id is not None:
        return str(event.event_id)
    if event.content_hash is not None:
        return event.content_hash
    return (
        f"derived:{event.borrower_id}:{event.facility_id}:{event.event_date}:"
        f"{event.family}:{event.event_type}:{event.magnitude}"
    )


def _new_uuid(factory: object) -> UUID:
    if not callable(factory):
        raise TypeError("id_factory must be callable.")
    value = factory()
    if not isinstance(value, UUID):
        raise TypeError("id_factory must return a UUID.")
    return value


def _calendar_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a calendar date, not a datetime.")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO calendar date.") from error
    raise TypeError(f"{field_name} must be a calendar date.")


__all__ = [
    "CONTRADICTION_RULES",
    "DEFAULT_CONTRADICTION_RULES",
    "SUPERSESSION_RULES",
    "ContradictionRule",
    "SupersessionBatch",
    "SupersessionRule",
    "SupersessionResult",
    "Revision",
    "apply_supersession",
    "contradiction_rules",
    "find_rule",
    "point_in_time",
    "read_as_of",
    "reconstruct_as_of",
    "resolve_supersession",
    "resolve_contradictions",
    "score_supersession",
    "supersede",
    "supersede_evidence",
    "point_in_time_read",
    "reconstruct_ledger",
]
