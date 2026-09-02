"""Product-owned shape checks for the model boundary.

The stage-1 injection check lives in this module because the provider boundary
must remain separate from the pure domain verification code.  Stage 7 uses
the same boundary for a different guarantee: a model may write connecting
prose, but it may not change a recorded value, select an unapproved action,
or turn advisory text into a directive.

The stage-7 functions are deliberately deterministic and side-effect free.
They accept the already assembled :class:`~covenant_radar.domain.memo.slots.MemoSlotMap`
and an explicit action catalogue; they never load data, calculate a fact, or
call a provider.  This makes the checks safe to run before a memo is stored
or rendered and keeps them usable by offline evaluation.

Stage-1 shape checks: injection-shaped input, combined with the six code
verifications into one verdict (`spec §R-06.c`, `plan.md §8`'s `T-095`).

`domain/intake/verify.py` owns the six code verifications (`spec §R-06`) as
pure domain logic with no knowledge of the model boundary. This module adds
the one check that *is* about that boundary — whether the clause text sent
outbound at stage 1 was itself shaped like an attempt to redirect the model
("ignore the above instructions", "reveal your system prompt", and the
like) — and combines the two into :class:`Stage1VerificationOutcome`.

It lives here, not in ``domain/``, because only this layer may depend on
both: the domain-purity import-linter contract forbids
``covenant_radar.domain`` from ever importing ``covenant_radar.ai``, so the
combination has to happen on this side of that boundary, the same way
`ai/intake.py` already imports `domain.intake.candidates` and
`domain.intake.proposal` to do its own wiring.

A match against the injection patterns below does not prove an attack
succeeded — it proves the *input* was shaped like one, which is exactly the
condition `spec §R-06.c` requires to be refused with a fixed message and
logged as a security event, independent of what the model actually
returned and independent of whether the six code verifications would
otherwise have passed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Final

from covenant_radar.domain.intake.proposal import StageOneProposal
from covenant_radar.domain.intake.verify import (
    VerificationContext,
    VerificationReport,
    verify_proposal,
)
from covenant_radar.domain.memo.slots import MemoSlotMap, SlotState

__all__ = [
    "ADVISORY_DISCLAIMER",
    "CatalogueAction",
    "FIXED_INJECTION_REFUSAL",
    "InjectionScanResult",
    "MAX_MEMO_OUTPUT_TOKENS",
    "SecurityAuditEvent",
    "ShapeCheck",
    "Stage1VerificationOutcome",
    "Stage7ShapeReport",
    "T6_MAX_OUTPUT_TOKENS",
    "check_memo_shapes",
    "check_stage7_shapes",
    "extract_numeric_tokens",
    "normalise_catalogue_actions",
    "scan_directive_language",
    "scan_for_injection",
    "validate_stage7_draft",
    "verify_memo_shapes",
    "verify_stage1_proposal",
    "verify_stage7_draft",
    "verify_stage7_shapes",
]

# T6 in ``spec.md``.  This is also used as the request ceiling by
# ``ai.memo`` so a provider cannot be asked for a response longer than the
# product is prepared to accept.
MAX_MEMO_OUTPUT_TOKENS: Final[int] = 1_200
T6_MAX_OUTPUT_TOKENS: Final[int] = MAX_MEMO_OUTPUT_TOKENS
ADVISORY_DISCLAIMER: Final[str] = "human credit review is required before action"


@dataclass(frozen=True, slots=True)
class CatalogueAction:
    """The minimum immutable action shape needed by a stage-7 check."""

    id: str
    role_tag: str
    text: str

    def __post_init__(self) -> None:
        for name in ("id", "role_tag", "text"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Catalogue action {name} must be non-blank text.")
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError(f"Catalogue action {name} contains a control character.")
            object.__setattr__(self, name, value.strip())
        object.__setattr__(self, "role_tag", _normalise_role_tag(self.role_tag))


@dataclass(frozen=True, slots=True)
class ShapeCheck:
    """One named, explainable stage-7 check result."""

    name: str
    passed: bool
    failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ShapeCheck.name must be non-blank text.")
        if not isinstance(self.passed, bool):
            raise TypeError("ShapeCheck.passed must be boolean.")
        failures = tuple(self.failures)
        if any(not isinstance(item, str) or not item.strip() for item in failures):
            raise ValueError("ShapeCheck.failures must contain non-blank text.")
        if self.passed and failures:
            raise ValueError("A passing ShapeCheck cannot contain failures.")
        if not self.passed and not failures:
            raise ValueError("A failing ShapeCheck must name at least one failure.")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "failures", failures)

    @property
    def ok(self) -> bool:
        """Compatibility alias for callers that use ``ok`` for a verdict."""

        return self.passed

    @property
    def detail(self) -> str | None:
        """Return a concise first failure for UI and retry feedback."""

        return None if self.passed else "; ".join(self.failures)


@dataclass(frozen=True, slots=True)
class Stage7ShapeReport:
    """The four stage-7 checks and their complete failure detail.

    ``grounding`` includes the numeric-token and advisory-language checks.
    Keeping those related checks together gives callers the four stable
    contract-level results while ``directive_failures`` remains available for
    a precise refusal message.
    """

    slots: ShapeCheck
    actions: ShapeCheck
    length: ShapeCheck
    grounding: ShapeCheck
    directive_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        checks = (self.slots, self.actions, self.length, self.grounding)
        if any(not isinstance(check, ShapeCheck) for check in checks):
            raise TypeError("Stage7ShapeReport fields must be ShapeCheck values.")
        directives = tuple(self.directive_failures)
        if any(not isinstance(item, str) or not item.strip() for item in directives):
            raise ValueError("directive_failures must contain non-blank text.")
        object.__setattr__(self, "directive_failures", directives)

    @property
    def checks(self) -> tuple[ShapeCheck, ...]:
        """Return the four checks in stable evaluation order."""

        return (self.slots, self.actions, self.length, self.grounding)

    @property
    def slot_check(self) -> ShapeCheck:
        return self.slots

    @property
    def action_check(self) -> ShapeCheck:
        return self.actions

    @property
    def length_check(self) -> ShapeCheck:
        return self.length

    @property
    def numeric_check(self) -> ShapeCheck:
        return self.grounding

    @property
    def all_passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def passed(self) -> bool:
        """Short alias used by presentation and integration callers."""

        return self.all_passed

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"{check.name}: {failure}"
            for check in self.checks
            if not check.passed
            for failure in check.failures
        )

    @property
    def verdict(self) -> str:
        return "passed" if self.all_passed else "refused"


def normalise_catalogue_actions(
    catalogue: Iterable[object] | Mapping[str, object] | object,
) -> tuple[CatalogueAction, ...]:
    """Normalise a catalogue/service into stable action facts.

    The action catalogue is normally a tuple of domain ``CatalogueEntry``
    values.  The mapping and service forms are supported at the boundary so
    this checker can be used by persistence adapters and offline tests without
    importing a database repository.
    """

    source: object = catalogue
    list_method = getattr(source, "list", None)
    if callable(list_method):
        source = list_method(active_only=True)
    if isinstance(source, Mapping):
        if _looks_like_action(source):
            values: Iterable[object] = (source,)
        else:
            values = tuple(_mapping_action_with_key(key, value) for key, value in source.items())
    elif isinstance(source, str | bytes | bytearray) or not isinstance(source, Iterable):
        raise TypeError("The action catalogue must be an iterable, mapping or catalogue service.")
    else:
        values = source

    actions: list[CatalogueAction] = []
    by_id: dict[str, CatalogueAction] = {}
    for value in values:
        action = _catalogue_action(value)
        previous = by_id.get(action.id)
        if previous is not None and previous != action:
            raise ValueError(f"Action catalogue contains conflicting entries for {action.id!r}.")
        if previous is None:
            by_id[action.id] = action
            actions.append(action)
    return tuple(sorted(actions, key=lambda item: item.id))


def check_stage7_shapes(
    draft: Mapping[str, object] | object,
    slots: MemoSlotMap,
    catalogue: Iterable[object] | Mapping[str, object] | object,
    *,
    require_actions: bool = False,
) -> Stage7ShapeReport:
    """Run every product-owned stage-7 check against one draft.

    The function does not raise for a malformed model payload.  Malformed
    shape is a normal model refusal and is returned through the named checks,
    allowing the caller to feed the exact reason into the one permitted
    regeneration.  Programmer errors in the slot map or catalogue still
    raise because those are application wiring defects, not model output.
    """

    if not isinstance(slots, MemoSlotMap):
        raise TypeError("check_stage7_shapes requires a MemoSlotMap.")
    if not isinstance(require_actions, bool):
        raise TypeError("require_actions must be boolean.")
    actions = normalise_catalogue_actions(catalogue)
    payload, payload_failures = _payload_mapping(draft)
    payload_failures = payload_failures + _reply_shape_failures(
        payload, require_actions=require_actions
    )

    slot_failures = _slot_failures(slots)
    action_failures = list(payload_failures)
    action_failures.extend(_action_failures(payload, slots, actions))

    prose = _prose_text(payload)
    length_failures = _length_failures(prose)
    grounding_failures, directive_failures = _grounding_failures(payload, slots)

    return Stage7ShapeReport(
        slots=_check("slot_resolution", slot_failures),
        actions=_check("catalogue_actions", action_failures),
        length=_check("length", length_failures),
        grounding=_check("grounding", grounding_failures),
        directive_failures=directive_failures,
    )


def verify_stage7_draft(
    draft: Mapping[str, object] | object,
    slots: MemoSlotMap,
    catalogue: Iterable[object] | Mapping[str, object] | object,
    *,
    require_actions: bool = False,
) -> Stage7ShapeReport:
    """Compatibility-facing name for :func:`check_stage7_shapes`."""

    return check_stage7_shapes(draft, slots, catalogue, require_actions=require_actions)


verify_stage7_shapes = verify_stage7_draft
check_memo_shapes = check_stage7_shapes
verify_memo_shapes = check_stage7_shapes


def validate_stage7_draft(
    draft: Mapping[str, object] | object,
    slots: MemoSlotMap,
    catalogue: Iterable[object] | Mapping[str, object] | object,
) -> Stage7ShapeReport:
    """Compatibility-facing validator alias.

    Validation is intentionally report-shaped rather than exception-shaped;
    a failed model response is expected control flow for the retry boundary.
    """

    return check_stage7_shapes(draft, slots, catalogue)


def extract_numeric_tokens(text: str) -> tuple[str, ...]:
    """Extract exact-form numeric/date tokens from prose.

    ISO dates are one token.  Other forms are deliberately not normalised:
    ``1.250`` is different from ``1.25``, and ``2026/10/15`` is different from
    ``2026-10-15``.  That exactness is what prevents the model from rewriting
    a recorded figure while still permitting it to place that figure in a
    sentence.
    """

    if not isinstance(text, str):
        raise TypeError("extract_numeric_tokens requires text.")
    pattern = re.compile(r"(?<!\w)\d{4}-\d{2}-\d{2}(?!\w)|(?<!\w)[+-]?\d+(?:\.\d+)?")
    return tuple(match.group(0) for match in pattern.finditer(text))


def scan_directive_language(text: str) -> tuple[str, ...]:
    """Return directive phrases that are not permitted in advisory prose."""

    if not isinstance(text, str):
        raise TypeError("scan_directive_language requires text.")
    matches: list[str] = []
    for pattern in _DIRECTIVE_PATTERNS:
        for match in pattern.finditer(text):
            phrase = " ".join(match.group(0).split())
            if phrase.casefold() not in {item.casefold() for item in matches}:
                matches.append(phrase)
    return tuple(matches)


_DIRECTIVE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(?:you\s+)?(?:must|shall|need(?:s)?\s+to|should)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:approve|decline|waive|sanction|escalate|execute|implement|block|reject)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:do\s+not|take\s+action|proceed\s+with|act\s+now|immediately)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcredit\s+decision\b", re.IGNORECASE),
)


def _normalise_role_tag(value: object) -> str:
    raw = value.value if isinstance(value, Enum) else value
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Catalogue action role_tag must be non-blank text.")
    normalized = raw.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized == "rm":
        normalized = "relationship_manager"
    if normalized not in {"relationship_manager", "credit", "risk"}:
        raise ValueError(f"Catalogue action role_tag {raw!r} is not permitted.")
    return normalized


def _looks_like_action(value: Mapping[object, object]) -> bool:
    return any(key in value for key in ("id", "code", "intervention_id"))


def _mapping_action_with_key(key: object, value: object) -> object:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Action catalogue ids must be non-blank text.")
    if isinstance(value, Mapping):
        result = dict(value)
        result.setdefault("id", key)
        return result
    return value


def _catalogue_action(value: object) -> CatalogueAction:
    if isinstance(value, CatalogueAction):
        return value
    if isinstance(value, Mapping):
        identifier = value.get("id", value.get("code", value.get("intervention_id")))
        role_tag = value.get("role_tag")
        text = value.get("text", value.get("intervention_text", ""))
    else:
        identifier = getattr(value, "id", getattr(value, "code", None))
        role_tag = getattr(value, "role_tag", None)
        text = getattr(value, "text", getattr(value, "intervention_text", ""))
    if isinstance(identifier, Enum):
        identifier = identifier.value
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Catalogue action must carry a non-blank id.")
    if not isinstance(role_tag, str | Enum):
        raise ValueError(f"Catalogue action {identifier!r} has no role_tag.")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Catalogue action {identifier!r} has no intervention text.")
    return CatalogueAction(identifier, _normalise_role_tag(role_tag), text)


def _payload_mapping(
    draft: Mapping[str, object] | object,
) -> tuple[dict[str, object], tuple[str, ...]]:
    if isinstance(draft, Mapping):
        return dict(draft), ()
    if isinstance(draft, str):
        try:
            parsed = json.loads(draft)
        except json.JSONDecodeError as error:
            return {}, (f"model reply is not valid JSON: {error.msg}",)
        if isinstance(parsed, dict):
            return parsed, ()
        return {}, ("model reply must be one JSON object",)
    as_mapping = getattr(draft, "as_mapping", None)
    if callable(as_mapping):
        value = as_mapping()
        if isinstance(value, Mapping):
            return dict(value), ()
    return {}, ("model reply must be a JSON object",)


def _reply_shape_failures(
    payload: Mapping[str, object], *, require_actions: bool = False
) -> tuple[str, ...]:
    if not payload:
        return ()
    keys = set(payload)
    allowed = {
        "headline",
        "summary",
        "drivers",
        "actions",
        "recommended_actions",
        "recommended_next_step",
        "disclaimer",
    }
    required = {"headline", "summary", "drivers", "recommended_next_step", "disclaimer"}
    failures: list[str] = []
    missing = sorted(required.difference(keys), key=str)
    unexpected = sorted(keys.difference(allowed), key=str)
    if missing:
        failures.append("reply is missing required field(s): " + ", ".join(map(str, missing)))
    if unexpected:
        failures.append("reply has unexpected field(s): " + ", ".join(map(str, unexpected)))
    if "actions" in keys and "recommended_actions" in keys:
        failures.append("reply may contain actions only once")
    if require_actions and "actions" not in keys:
        failures.append("reply is missing required field: actions")
    for field_name in ("headline", "summary", "recommended_next_step", "disclaimer"):
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            failures.append(f"reply field {field_name!r} must be non-blank text")
    raw_drivers = payload.get("drivers")
    if isinstance(raw_drivers, str | bytes | bytearray) or not isinstance(raw_drivers, Sequence):
        failures.append("reply field 'drivers' must be an array of text")
    else:
        for index, value in enumerate(raw_drivers):
            if not isinstance(value, str) or not value.strip():
                failures.append(f"reply field drivers[{index}] must be non-blank text")
    return tuple(failures)


def _slot_failures(slots: MemoSlotMap) -> list[str]:
    failures: list[str] = []
    for slot in slots:
        if slot.state is SlotState.PRESENT and not slot.record_references:
            failures.append(f"slot {slot.name!r} is present without a record reference")
    if not slots.all_resolved:
        failures.append("one or more memo slots are unresolved")
    return failures


def _action_failures(
    payload: Mapping[str, object],
    slots: MemoSlotMap,
    catalogue: Sequence[CatalogueAction],
) -> list[str]:
    failures: list[str] = []
    available = {action.id: action for action in catalogue}
    recommended = _recommended_slot_actions(slots)
    for identifier, role_tag in recommended:
        action = available.get(identifier)
        if action is None:
            failures.append(f"slot action {identifier!r} is not in the catalogue")
        elif action.role_tag != role_tag:
            failures.append(
                f"slot action {identifier!r} has role_tag {role_tag!r}; "
                f"catalogue requires {action.role_tag!r}"
            )

    raw_actions = payload.get("actions", payload.get("recommended_actions", ()))
    if raw_actions is None:
        raw_actions = ()
    if isinstance(raw_actions, Mapping) or isinstance(raw_actions, str | bytes | bytearray):
        failures.append("actions must be an array of id and role_tag objects")
        parsed_actions: tuple[tuple[str, str], ...] = ()
    elif not isinstance(raw_actions, Sequence):
        failures.append("actions must be an array of id and role_tag objects")
        parsed_actions = ()
    else:
        parsed_actions = _parse_action_citations(raw_actions, failures)

    if not parsed_actions and not failures and recommended:
        next_step = payload.get("recommended_next_step")
        if isinstance(next_step, str) and next_step.strip():
            matching = tuple(
                identifier
                for identifier, _role_tag in recommended
                if available.get(identifier) is not None
                and available[identifier].text == next_step.strip()
            )
            if not matching and next_step.strip() != "Not available from the recorded evidence.":
                failures.append(
                    "recommended_next_step must cite a permitted action id or exactly "
                    "copy its catalogue wording"
                )

    for identifier, role_tag in parsed_actions:
        action = available.get(identifier)
        if action is None:
            failures.append(f"action {identifier!r} is not in the catalogue")
        elif action.role_tag != role_tag:
            failures.append(
                f"action {identifier!r} has role_tag {role_tag!r}; "
                f"catalogue requires {action.role_tag!r}"
            )

    expected_drivers = _driver_names(slots)
    raw_drivers = payload.get("drivers")
    driver_values: tuple[object, ...] = ()
    if isinstance(raw_drivers, str | bytes | bytearray) or not isinstance(raw_drivers, Sequence):
        failures.append("drivers must be an array of supplied driver names")
    else:
        driver_values = tuple(raw_drivers)
        if tuple(item for item in driver_values if isinstance(item, str)) != expected_drivers:
            failures.append("drivers must copy the supplied driver names exactly")
    if any(not isinstance(item, str) for item in driver_values):
        failures.append("drivers must contain text names only")

    next_step = payload.get("recommended_next_step")
    expected_texts = _intervention_texts(slots)
    if not isinstance(next_step, str):
        failures.append("recommended_next_step must be text")
    elif expected_texts and next_step.strip() not in expected_texts:
        failures.append("recommended_next_step must copy supplied intervention wording exactly")
    elif not expected_texts and next_step.strip() != "Not available from the recorded evidence.":
        failures.append("recommended_next_step must use the documented absence text")
    return failures


def _parse_action_citations(
    raw_actions: Sequence[object], failures: list[str]
) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for index, value in enumerate(raw_actions):
        if not isinstance(value, Mapping):
            failures.append(f"actions[{index}] must be an object with id and role_tag")
            continue
        identifier = value.get("id", value.get("code"))
        role_tag = value.get("role_tag")
        extra = set(value).difference({"id", "code", "role_tag"})
        if extra:
            failures.append(f"actions[{index}] has unexpected field(s): {', '.join(sorted(extra))}")
        if not isinstance(identifier, str) or not identifier.strip():
            failures.append(f"actions[{index}].id must be non-blank text")
            continue
        try:
            normalized_role = _normalise_role_tag(role_tag)
        except (TypeError, ValueError):
            failures.append(f"actions[{index}].role_tag is invalid")
            continue
        citation = (identifier.strip(), normalized_role)
        if citation in parsed:
            failures.append(f"actions contains duplicate citation {identifier!r}")
        else:
            parsed.append(citation)
    return tuple(parsed)


def _driver_names(slots: MemoSlotMap) -> tuple[str, ...]:
    value = slots["drivers"].value
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            name = item.get("name")
            if isinstance(name, str):
                result.append(name)
        elif isinstance(item, str):
            result.append(item)
    return tuple(result)


def _recommended_slot_actions(slots: MemoSlotMap) -> tuple[tuple[str, str], ...]:
    value = slots["recommended_interventions"].value
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("code", item.get("id"))
        role_tag = item.get("role_tag")
        if isinstance(identifier, str) and isinstance(role_tag, str):
            try:
                result.append((identifier, _normalise_role_tag(role_tag)))
            except ValueError:
                continue
    return tuple(result)


def _intervention_texts(slots: MemoSlotMap) -> tuple[str, ...]:
    value = slots["intervention_text"].value
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _prose_text(payload: Mapping[str, object]) -> str:
    values: list[str] = []
    for key in ("headline", "summary", "recommended_next_step", "disclaimer"):
        value = payload.get(key)
        if isinstance(value, str):
            values.append(value)
    drivers = payload.get("drivers")
    if isinstance(drivers, Sequence) and not isinstance(drivers, str | bytes | bytearray):
        values.extend(item for item in drivers if isinstance(item, str))
    return "\n".join(values)


def _length_failures(prose: str) -> list[str]:
    token_count = _count_output_tokens(prose)
    if token_count > MAX_MEMO_OUTPUT_TOKENS:
        return [
            f"memo output contains approximately {token_count} tokens; "
            f"T6 allows at most {MAX_MEMO_OUTPUT_TOKENS}"
        ]
    return []


def _count_output_tokens(text: str) -> int:
    # No provider-specific tokenizer is available in the product dependency
    # set.  This conservative lexical count is deterministic and errs toward
    # refusal at the configured ceiling rather than allowing an overlong memo.
    return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def _grounding_failures(
    payload: Mapping[str, object], slots: MemoSlotMap
) -> tuple[list[str], tuple[str, ...]]:
    failures: list[str] = []
    prose = _prose_text(payload)
    allowed = set(_slot_numeric_tokens(slots))
    actual = extract_numeric_tokens(prose)
    fabricated = tuple(dict.fromkeys(token for token in actual if token not in allowed))
    if fabricated:
        failures.append("numeric token(s) are not present in a slot: " + ", ".join(fabricated))

    ratio_name = slots["ratio_name"].value
    if isinstance(ratio_name, str) and ratio_name != "Not available from the recorded evidence.":
        headline = payload.get("headline")
        if not isinstance(headline, str) or ratio_name not in headline:
            failures.append("headline must name the supplied covenant exactly")

    directives = scan_directive_language(
        "\n".join(
            value
            for key in ("headline", "summary", "disclaimer")
            if isinstance(value := payload.get(key), str)
        )
    )
    if directives:
        failures.append("directive language is not permitted: " + ", ".join(directives))
    disclaimer = payload.get("disclaimer")
    if not isinstance(disclaimer, str) or disclaimer.strip().casefold() != ADVISORY_DISCLAIMER:
        failures.append("disclaimer must be the fixed advisory disclaimer")
    return failures, directives


def _slot_numeric_tokens(slots: MemoSlotMap) -> tuple[str, ...]:
    tokens: list[str] = []
    for slot in slots:
        value = slot.value
        candidates: tuple[str, ...]
        if isinstance(value, Decimal):
            candidates = (format(value, "f"),)
        elif isinstance(value, datetime | date):
            candidates = (value.isoformat(),)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            candidates = (str(value),)
        else:
            candidates = _nested_text_values(value)
        for candidate in candidates:
            for token in extract_numeric_tokens(candidate):
                if token not in tokens:
                    tokens.append(token)
    return tuple(tokens)


def _nested_text_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        result: list[str] = []
        for child in value.values():
            result.extend(_nested_text_values(child))
        return tuple(result)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        result = []
        for child in value:
            result.extend(_nested_text_values(child))
        return tuple(result)
    if isinstance(value, Decimal):
        return (format(value, "f"),)
    if isinstance(value, datetime | date):
        return (value.isoformat(),)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return (str(value),)
    return ()


def _check(name: str, failures: Iterable[str]) -> ShapeCheck:
    normalized = tuple(failure for failure in failures if failure)
    return ShapeCheck(name=name, passed=not normalized, failures=normalized)


#: The one fixed refusal shown to a reviewer and recorded for a clause whose
#: text was injection-shaped. Deliberately generic: it never echoes which
#: pattern matched or repeats any of the submitted text back, so it gives an
#: attacker probing the filter nothing to calibrate against.
FIXED_INJECTION_REFUSAL: Final[str] = (
    "This clause could not be processed automatically and has been referred for manual review."
)

_MAX_EXCERPT_LENGTH: Final[int] = 240

#: Deterministic, regex-only detection — no model call, so an attempt is
#: caught before the reply, not inferred from it. Each label is stable and
#: becomes part of a `SecurityAuditEvent`'s `matched_patterns`, the same way
#: `domain/intake/candidates.py` labels its own detection rules.
_INJECTION_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "ignore_instructions",
        re.compile(
            r"\bignore\s+(?:all|any|the)?\s*(?:previous|prior|above|earlier)\s+instructions?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_instructions",
        re.compile(
            r"\bdisregard\s+(?:all|any|the)?\s*(?:previous|prior|above|earlier)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "forget_instructions",
        re.compile(r"\bforget\s+(?:everything|all)\s+(?:above|before|prior)\b", re.IGNORECASE),
    ),
    ("new_instructions", re.compile(r"\bnew\s+instructions?\s*:", re.IGNORECASE)),
    (
        "reveal_system_prompt",
        re.compile(
            r"\b(?:reveal|show|print|output)\s+(?:your|the)\s+"
            r"(?:system\s+prompt|hidden\s+instructions|instructions)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_override",
        re.compile(
            r"\byou\s+are\s+now\b|\bact\s+as\s+if\s+you\s+(?:are|were)\b",
            re.IGNORECASE,
        ),
    ),
    ("developer_mode", re.compile(r"\b(?:developer|jailbreak|dan)\s+mode\b", re.IGNORECASE)),
    (
        "do_not_verify",
        re.compile(r"\bdo\s+not\s+(?:verify|check|validate)\s+this\b", re.IGNORECASE),
    ),
    (
        "prompt_leak_request",
        re.compile(r"\bwhat\s+(?:is|are)\s+your\s+(?:instructions|rules)\b", re.IGNORECASE),
    ),
)


@dataclass(frozen=True, slots=True)
class InjectionScanResult:
    """Whether *text* matched a prompt-injection pattern, and which ones."""

    detected: bool
    matched_patterns: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "matched_patterns", tuple(self.matched_patterns))
        if self.detected and not self.matched_patterns:
            raise ValueError(
                "A detected InjectionScanResult must name at least one matched pattern."
            )
        if not self.detected and self.matched_patterns:
            raise ValueError("An undetected InjectionScanResult must not carry matched patterns.")


def scan_for_injection(text: str) -> InjectionScanResult:
    """Scan *text* for prompt-injection-shaped language.

    Deterministic and side-effect free: no model call, no I/O. Applied to
    the clause text a candidate carries — the same text `ai/intake.py`
    sends outbound at stage 1 — independent of whatever the model replies.
    """
    if not isinstance(text, str):
        raise TypeError("scan_for_injection requires text.")
    matched = tuple(label for label, pattern in _INJECTION_PATTERNS if pattern.search(text))
    return InjectionScanResult(detected=bool(matched), matched_patterns=matched)


@dataclass(frozen=True, slots=True)
class SecurityAuditEvent:
    """A security-event-shaped record of one injection attempt, ready for a
    caller to hand to the append-only audit chain (`C-60`, `audit/record.py`
    — a later task this module deliberately does not import: the
    audit-write-boundary import-linter contract confines that write path to
    `covenant_radar.audit.store` alone).

    ``excerpt`` is bounded and whitespace-collapsed rather than the full
    clause text, so a persisted event stays a manageable, reviewable size
    regardless of how long the offending clause was.
    """

    event_type: str
    detail: str
    matched_patterns: tuple[str, ...]
    excerpt: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "matched_patterns", tuple(self.matched_patterns))
        if not self.matched_patterns:
            raise ValueError("SecurityAuditEvent.matched_patterns must not be empty.")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("SecurityAuditEvent.event_type must be non-empty text.")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("SecurityAuditEvent.detail must be non-empty text.")
        if not isinstance(self.excerpt, str) or not self.excerpt.strip():
            raise ValueError("SecurityAuditEvent.excerpt must be non-empty text.")
        if len(self.excerpt) > _MAX_EXCERPT_LENGTH:
            raise ValueError(
                f"SecurityAuditEvent.excerpt exceeds {_MAX_EXCERPT_LENGTH} characters."
            )


@dataclass(frozen=True, slots=True)
class Stage1VerificationOutcome:
    """The complete stage-1 verdict: the six code verifications plus the
    injection-shaped-input check, combined the way a caller actually needs
    to decide whether a proposal may ever be shown for confirmation.

    ``all_passed`` is ``False`` whenever either half refuses: a proposal
    that passed all six checks is still refused if its source text was
    injection-shaped, and a proposal is never silently confirmable just
    because the six checks happened to pass despite the attempt.
    """

    verification: VerificationReport
    injection_detected: bool
    security_event: SecurityAuditEvent | None
    refusal_message: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.verification, VerificationReport):
            raise TypeError("Stage1VerificationOutcome.verification must be a VerificationReport.")
        if not isinstance(self.injection_detected, bool):
            raise TypeError("Stage1VerificationOutcome.injection_detected must be a boolean.")
        if self.injection_detected:
            if self.security_event is None or self.refusal_message is None:
                raise ValueError(
                    "A detected Stage1VerificationOutcome must carry a security_event and "
                    "a refusal_message."
                )
        else:
            if self.security_event is not None or self.refusal_message is not None:
                raise ValueError(
                    "An undetected Stage1VerificationOutcome must not carry a security_event "
                    "or a refusal_message."
                )

    @property
    def all_passed(self) -> bool:
        """Whether the proposal may be confirmed: all six checks passed and
        its source text was never injection-shaped."""
        return self.verification.all_passed and not self.injection_detected

    @property
    def failed_checks(self) -> tuple[str, ...]:
        """The machine-readable names of every failed code verification —
        `C-06`'s ``failed_checks[]``. Silent on the injection refusal by
        design: that refusal is reported through ``refusal_message``
        instead, deliberately never naming which detection pattern fired."""
        return self.verification.failed_checks


def verify_stage1_proposal(
    proposal: StageOneProposal, context: VerificationContext
) -> Stage1VerificationOutcome:
    """Run the six code verifications and the injection-shaped-input scan
    against one stage-1 proposal, and combine them into one verdict.

    Never raises for a malformed or injection-shaped proposal — every such
    case is a normal, named refusal, not a caller error.
    """
    if not isinstance(proposal, StageOneProposal):
        raise TypeError("verify_stage1_proposal requires a StageOneProposal.")

    verification = verify_proposal(proposal, context)
    scan = scan_for_injection(proposal.candidate.text)
    if not scan.detected:
        return Stage1VerificationOutcome(
            verification=verification,
            injection_detected=False,
            security_event=None,
            refusal_message=None,
        )

    security_event = SecurityAuditEvent(
        event_type="intake.injection_attempt",
        detail="The submitted clause text matched a prompt-injection pattern and was refused.",
        matched_patterns=scan.matched_patterns,
        excerpt=_bounded_excerpt(proposal.candidate.text),
    )
    return Stage1VerificationOutcome(
        verification=verification,
        injection_detected=True,
        security_event=security_event,
        refusal_message=FIXED_INJECTION_REFUSAL,
    )


def _bounded_excerpt(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= _MAX_EXCERPT_LENGTH:
        return cleaned
    return cleaned[: _MAX_EXCERPT_LENGTH - 1].rstrip() + "…"
